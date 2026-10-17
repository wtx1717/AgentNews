"""MCP 客户端：连接远端 MCP 服务，发现并校验工具，再受控地调用它们。"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

import httpx2
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError as JsonSchemaValidationError
from mcp import Client as ProtocolClient
from mcp.client.streamable_http import streamable_http_client

from agentnews.config import Settings, get_settings

# 工具名须满足模型 function-calling 的命名约束，否则无法直接暴露给模型。
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class MCPError(RuntimeError):
    """Base class for sanitized MCP failures."""


class MCPConfigurationError(MCPError):
    """Raised when MCP settings are incomplete."""


class MCPTimeoutError(MCPError):
    """Raised when an MCP operation exceeds its timeout."""


class MCPUnavailableError(MCPError):
    """Raised when an MCP operation fails for an unknown upstream reason."""


class MCPToolDiscoveryError(MCPError):
    """Raised when the server publishes invalid or duplicate tool definitions."""


class MCPToolSchemaError(MCPError):
    """Raised when a published input schema is invalid or incompatible."""


class MCPToolArgumentError(MCPError):
    """Raised when tool-call arguments do not satisfy the published schema."""


class MCPToolCallError(MCPError):
    """Raised when an MCP tool reports an execution error."""


@dataclass(frozen=True, slots=True)
class MCPToolDefinition:
    """Validated tool metadata advertised by an MCP server."""

    name: str
    description: str
    input_schema: dict[str, Any]

    @property
    def inputSchema(self) -> dict[str, Any]:
        """Wire-compatible spelling retained for callers inspecting MCP metadata."""
        return self.input_schema


class MCPClient:
    """Async client that discovers and invokes all tools published by an MCP server."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._stack: AsyncExitStack | None = None
        self._client: Any | None = None
        self._tools: dict[str, MCPToolDefinition] = {}
        self._validators: dict[str, Draft202012Validator] = {}

    @property
    def tools(self) -> tuple[MCPToolDefinition, ...]:
        """返回本次连接中已发现并通过校验的工具定义。"""
        return tuple(self._tools.values())

    async def __aenter__(self) -> MCPClient:
        """建立连接、发现工具并预编译校验器，失败时清理已建资源。"""
        try:
            url, api_key = self._settings.require_search()
        except ValueError as exc:
            raise MCPConfigurationError(str(exc)) from None

        timeout = self._settings.mcp_search_timeout
        stack = AsyncExitStack()
        self._stack = stack
        try:
            http_client = await stack.enter_async_context(
                httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {api_key.get_secret_value()}"},
                    timeout=timeout,
                )
            )
            transport = streamable_http_client(url, http_client=http_client)
            self._client = ProtocolClient(transport, read_timeout_seconds=timeout)
            # 连接与工具发现共用一个总超时，避免半开连接无限阻塞。
            async with asyncio.timeout(timeout):
                await stack.enter_async_context(self._client)
                self._tools = await _discover_tools(self._client)
                try:
                    self._validators = {
                        name: _create_validator(definition.input_schema)
                        for name, definition in self._tools.items()
                    }
                except SchemaError:
                    raise MCPToolSchemaError("MCP 工具 schema 无效") from None
        except MCPError:
            await self._reset_failed_enter(stack)
            raise
        except (TimeoutError, httpx2.TimeoutException):
            await self._reset_failed_enter(stack)
            raise MCPTimeoutError("MCP 服务连接或工具发现超时") from None
        except Exception:
            await self._reset_failed_enter(stack)
            raise MCPUnavailableError("MCP 服务暂不可用") from None
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        stack, self._stack = self._stack, None
        self._client = None
        self._tools = {}
        self._validators = {}
        if stack is not None:
            await stack.aclose()

    async def _reset_failed_enter(self, stack: AsyncExitStack) -> None:
        try:
            await stack.aclose()
        except Exception:
            pass
        self._stack = None
        self._client = None
        self._tools = {}
        self._validators = {}

    async def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> str:
        """Validate and invoke the named published tool, returning safe JSON."""
        if self._client is None:
            raise MCPUnavailableError("MCP 客户端未连接")
        if not isinstance(name, str) or name not in self._tools:
            raise MCPToolDiscoveryError("请求了未注册的 MCP 工具")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, Mapping):
            raise MCPToolArgumentError("MCP 工具参数必须是对象")
        try:
            self._validators[name].validate(dict(arguments))
        except JsonSchemaValidationError:
            raise MCPToolArgumentError("MCP 工具参数校验失败") from None
        except Exception:
            raise MCPToolSchemaError("MCP 工具 schema 校验失败") from None

        try:
            async with asyncio.timeout(self._settings.mcp_search_timeout):
                response = await self._client.call_tool(name, dict(arguments))
        except (TimeoutError, httpx2.TimeoutException):
            raise MCPTimeoutError("MCP 工具调用超时") from None
        except Exception:
            raise MCPUnavailableError("MCP 工具调用失败") from None

        # 同时兼容 is_error / isError 两种拼写，显式错误标志一律按失败处理。
        if _read_field(response, "is_error", "isError") is True:
            raise MCPToolCallError("MCP 工具返回错误")
        return _json_result({"ok": True, "result": _serialize_value(response)})


async def _discover_tools(client: Any) -> dict[str, MCPToolDefinition]:
    """分页拉取全部工具定义；工具重名或游标循环直接判为发现失败。"""
    tools: dict[str, MCPToolDefinition] = {}
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        page = await client.list_tools(cursor=cursor)
        page_tools = _read_field(page, "tools")
        if not isinstance(page_tools, list):
            raise MCPToolDiscoveryError("MCP 工具列表格式无效")
        for raw_tool in page_tools:
            definition = _normalize_tool(raw_tool)
            if definition.name in tools:
                raise MCPToolDiscoveryError(f"MCP 工具名称重复：{definition.name}")
            tools[definition.name] = definition
        cursor = _read_field(page, "next_cursor", "nextCursor")
        if cursor is None:
            break
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise MCPToolDiscoveryError("MCP 工具列表分页游标重复或无效")
        seen_cursors.add(cursor)
    return tools


def _normalize_tool(raw_tool: Any) -> MCPToolDefinition:
    """把原始工具条目规范化，并校验其名称与 inputSchema 根类型。"""
    name = _read_field(raw_tool, "name")
    if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
        raise MCPToolDiscoveryError("MCP 工具名称无效或不兼容模型工具接口")

    raw_schema = _read_field(raw_tool, "input_schema", "inputSchema")
    if hasattr(raw_schema, "model_dump"):
        raw_schema = raw_schema.model_dump(by_alias=True, exclude_none=True)
    if not isinstance(raw_schema, Mapping):
        raise MCPToolSchemaError(f"MCP 工具 {name} 的 inputSchema 无效")
    schema = copy.deepcopy(dict(raw_schema))
    if schema.get("type") != "object":
        raise MCPToolSchemaError(f"MCP 工具 {name} 的 inputSchema 根类型必须是 object")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise MCPToolSchemaError(f"MCP 工具 {name} 的 inputSchema 无效") from None

    description = _read_field(raw_tool, "description")
    if description is not None and not isinstance(description, str):
        raise MCPToolDiscoveryError(f"MCP 工具 {name} 的 description 无效")
    return MCPToolDefinition(name, description or "", schema)


def _create_validator(schema: Mapping[str, Any]) -> Draft202012Validator:
    """构造禁用远程 $ref 解析的 schema 校验器。"""
    # A server-provided schema must never trigger an arbitrary network fetch via $ref.
    from referencing import Registry
    from referencing.exceptions import NoSuchResource

    def reject_remote_references(uri: str) -> Any:
        raise NoSuchResource(ref=uri)

    return Draft202012Validator(
        schema,
        registry=Registry(retrieve=reject_remote_references),
    )


def _read_field(value: Any, *names: str) -> Any:
    """按别名依次读取字段，兼容映射与对象属性两种返回形态。"""
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        result = getattr(value, name, None)
        if result is not None:
            return result
    return None


def _serialize_value(value: Any) -> Any:
    """递归把 MCP 返回对象转成可 JSON 序列化的纯数据结构。"""
    if hasattr(value, "model_dump"):
        return _serialize_value(value.model_dump(by_alias=True, exclude_none=True))
    if isinstance(value, Mapping):
        return {str(key): _serialize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "__dict__"):
        return _serialize_value(vars(value))
    return str(value)


def _json_result(value: Any) -> str:
    """序列化为稳定且紧凑的 JSON 文本，便于模型消费。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# Compatibility alias for callers that used the interim descriptive name.
MCPToolsClient = MCPClient
Client = ProtocolClient
