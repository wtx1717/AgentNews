"""LangChain tool-calling orchestration for AgentNews."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..config import Settings, get_settings
from ..tools import (
    MCPConfigurationError,
    MCPClient,
    MCPError,
    MCPTimeoutError,
    MCPToolArgumentError,
    MCPUnavailableError,
    OnlineSearchResult,
    create_bash_tool,
    create_digest_tool,
    create_send_report_email_tool,
    create_local_file_tools,
)
from ..tools.path_security import ManagedPathResolver
from .models.deepseek import create_deepseek_model, map_deepseek_error
from .prompt_catalog import get_prompt


StopReason = Literal["completed", "max_tool_calls"]


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """One model-requested tool call and the result returned to the model."""

    name: str
    arguments: Mapping[str, Any] | None
    result: str
    call_id: str


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """Stable result returned by one Agent run."""

    text: str
    tool_calls: int
    stop_reason: StopReason
    tool_call_records: tuple[ToolCallRecord, ...] = ()


class ToolRegistrationError(RuntimeError):
    """Raised when model tools cannot be registered without ambiguity."""


class WebSearchInput(BaseModel):
    """Deprecated compatibility schema for explicitly injected search clients."""

    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, strict=True, description="Search query.")
    limit: int = Field(default=5, ge=5, le=20, strict=True, description="Maximum results.")


class AgentRunner:
    """Run a model-driven LangChain tool loop with bounded tool execution."""

    def __init__(
        self,
        model: Any | None = None,
        *,
        settings: Settings | None = None,
        resolver: ManagedPathResolver | None = None,
        mcp_client_factory: Callable[[Settings], Any] | None = None,
        search_client_factory: Callable[[Settings], Any] | None = None,
        bash_approval_handler: Callable[[str, str], Awaitable[str]] | None = None,
    ) -> None:
        """初始化运行器，未显式传入模型时按配置创建 DeepSeek 模型。"""
        self.settings = settings or get_settings()
        self.model = model if model is not None else create_deepseek_model(self.settings)
        self.resolver = resolver
        self.mcp_client_factory = mcp_client_factory or MCPClient
        self.search_client_factory = search_client_factory
        self.bash_approval_handler = bash_approval_handler

    async def ainvoke(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        task_context: str | None = None,
        bash_approval_handler: Callable[[str, str], Awaitable[str]] | None = None,
        digest_handler: Callable[[str, str], Awaitable[str]] | None = None,
        email_handler: Callable[[int | None, bool], Awaitable[str]] | None = None,
    ) -> AgentRunResult:
        """Execute one prompt and its model-selected tools asynchronously.

        ``prompt`` remains the user request. ``system_prompt`` and
        ``task_context`` are separate model messages so task-specific data is
        never silently mixed into an ordinary user request.
        """
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must not be empty")
        if system_prompt is not None and (
            not isinstance(system_prompt, str) or not system_prompt.strip()
        ):
            raise ValueError("system_prompt must not be empty")
        if task_context is not None and not isinstance(task_context, str):
            raise TypeError("task_context must be a string or None")

        resolved_system_prompt = system_prompt or get_prompt("interaction")
        normalized_context = task_context.strip() if task_context else ""

        # 用退出栈统一管理 MCP 与搜索客户端的异步资源释放。
        stack = AsyncExitStack()
        try:
            tools = create_local_file_tools(self.resolver)
            resolved_bash_handler = bash_approval_handler or self.bash_approval_handler
            if resolved_bash_handler is not None:
                tools.append(create_bash_tool(resolved_bash_handler))
            if digest_handler is not None:
                tools.append(create_digest_tool(digest_handler))
            if email_handler is not None:
                tools.append(create_send_report_email_tool(email_handler))
            mcp_configured = (
                self.settings.mcp_search_server_url is not None
                or self.settings.mcp_search_api_key is not None
            )
            if mcp_configured:
                try:
                    self.settings.require_search()
                except ValueError as exc:
                    raise MCPConfigurationError(str(exc)) from None
                try:
                    mcp_client = self.mcp_client_factory(self.settings)
                    mcp_client = await stack.enter_async_context(mcp_client)
                except MCPError:
                    raise
                except (TimeoutError, asyncio.TimeoutError):
                    raise MCPTimeoutError("MCP 服务连接或工具发现超时") from None
                except Exception:
                    raise MCPUnavailableError("MCP 服务暂不可用") from None
                try:
                    definitions = tuple(mcp_client.tools)
                except Exception:
                    raise ToolRegistrationError("MCP 工具发现结果无效") from None
                for definition in definitions:
                    tool_name = getattr(definition, "name", None)
                    description = getattr(definition, "description", None)
                    input_schema = getattr(definition, "input_schema", None)
                    if (
                        not isinstance(tool_name, str)
                        or not tool_name
                        or not isinstance(description, str)
                        or not isinstance(input_schema, Mapping)
                        or input_schema.get("type") != "object"
                    ):
                        raise ToolRegistrationError("MCP 工具定义无效，无法注册")
                    # 用默认参数在定义时绑定工具名，避免闭包共享循环变量。
                    async def invoke_mcp_tool(
                        _tool_name: str = tool_name,
                        **arguments: Any,
                    ) -> str:
                        """把一次工具调用转发给 MCP 客户端。"""
                        return await mcp_client.call_tool(_tool_name, arguments)

                    try:
                        tools.append(
                            StructuredTool(
                                name=tool_name,
                                description=description,
                                args_schema=dict(input_schema),
                                coroutine=invoke_mcp_tool,
                            )
                        )
                    except Exception:
                        raise ToolRegistrationError("MCP 工具定义无法注册到模型") from None
            if self.search_client_factory is not None and not mcp_configured:
                search_client: Any | None = None

                async def run_legacy_search(query: str, limit: int = 5) -> str:
                    """首次调用时才创建旧版搜索客户端，并执行一次查询。"""
                    nonlocal search_client
                    if search_client is None:
                        search_client = self.search_client_factory(self.settings)
                        await stack.enter_async_context(search_client)
                    try:
                        results = await search_client.search(query, limit=limit)
                        return _json_result({"ok": True, "results": [_serialize_legacy_result(item) for item in results]})
                    except Exception:
                        return _json_error("search_error", "在线搜索工具执行失败，请稍后重试。")

                tools.insert(
                    0,
                    StructuredTool.from_function(
                        coroutine=run_legacy_search,
                        name="web_search",
                        description="Deprecated compatibility search tool.",
                        args_schema=WebSearchInput,
                        handle_validation_error=lambda _error: _json_error("invalid_arguments", "工具参数校验失败。"),
                    ),
                )

            tool_map: dict[str, Any] = {}
            for tool in tools:
                name = getattr(tool, "name", None)
                if not isinstance(name, str) or not name:
                    raise ToolRegistrationError("工具注册失败：工具名称无效")
                if name in tool_map:
                    raise ToolRegistrationError(f"工具名称冲突，无法注册：{name}")
                tool_map[name] = tool
            try:
                bound_model = self.model.bind_tools(tools)
            except Exception:
                raise ToolRegistrationError("工具定义无法绑定到模型") from None

            messages: list[Any] = [SystemMessage(content=resolved_system_prompt)]
            if normalized_context:
                messages.append(SystemMessage(content=normalized_context))
            messages.append(HumanMessage(content=prompt.strip()))
            executed = 0
            last_response: Any | None = None
            tool_results: list[str] = []
            tool_call_records: list[ToolCallRecord] = []
            while True:
                try:
                    response = await bound_model.ainvoke(messages)
                except Exception as error:
                    # 统一包装成脱敏异常，避免原始错误带出请求内容。
                    raise map_deepseek_error(error) from None
                last_response = response
                tool_calls = _tool_calls(response)
                if not tool_calls:
                    return AgentRunResult(
                        text=_message_text(response),
                        tool_calls=executed,
                        stop_reason="completed",
                        tool_call_records=tuple(tool_call_records),
                    )

                # 已达调用上限时不再执行新工具，直接安全返回。
                if executed >= self.settings.agent_max_tool_calls:
                    return AgentRunResult(
                        text=_limit_text(response, executed, tool_results),
                        tool_calls=executed,
                        stop_reason="max_tool_calls",
                        tool_call_records=tuple(tool_call_records),
                    )

                messages.append(response)
                for call in tool_calls:
                    if executed >= self.settings.agent_max_tool_calls:
                        return AgentRunResult(
                            text=_limit_text(last_response, executed, tool_results),
                            tool_calls=executed,
                            stop_reason="max_tool_calls",
                            tool_call_records=tuple(tool_call_records),
                        )
                    name = _field(call, "name")
                    call_id = _field(call, "id") or f"tool-call-{executed + 1}"
                    arguments = _field(call, "args")
                    content = await _invoke_tool(tool_map, name, arguments)
                    tool_results.append(content)
                    tool_call_records.append(
                        ToolCallRecord(
                            name=str(name) if isinstance(name, str) else "",
                            arguments=dict(arguments) if isinstance(arguments, Mapping) else None,
                            result=content,
                            call_id=str(call_id),
                        )
                    )
                    messages.append(
                        ToolMessage(
                            content=content,
                            tool_call_id=str(call_id),
                            name=str(name) if isinstance(name, str) else None,
                        )
                    )
                    executed += 1

                if executed >= self.settings.agent_max_tool_calls:
                    return AgentRunResult(
                        text=_limit_text(last_response, executed, tool_results),
                        tool_calls=executed,
                        stop_reason="max_tool_calls",
                        tool_call_records=tuple(tool_call_records),
                    )
        finally:
            try:
                await stack.aclose()
            except Exception:
                # 关闭阶段的异常不应掩盖本次运行的结果。
                pass

    def invoke(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        task_context: str | None = None,
    ) -> AgentRunResult:
        """Synchronous convenience wrapper around :meth:`ainvoke`."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.ainvoke(
                    prompt,
                    system_prompt=system_prompt,
                    task_context=task_context,
                )
            )
        raise RuntimeError("AgentRunner.invoke cannot run inside an active event loop; use ainvoke")


def create_agent_runner(
    model: Any | None = None,
    *,
    settings: Settings | None = None,
    resolver: ManagedPathResolver | None = None,
    mcp_client_factory: Callable[[Settings], Any] | None = None,
    search_client_factory: Callable[[Settings], Any] | None = None,
    bash_approval_handler: Callable[[str, str], Awaitable[str]] | None = None,
) -> AgentRunner:
    """Create a configured Agent runner."""
    return AgentRunner(
        model,
        settings=settings,
        resolver=resolver,
        mcp_client_factory=mcp_client_factory,
        search_client_factory=search_client_factory,
        bash_approval_handler=bash_approval_handler,
    )


async def _invoke_tool(tool_map: Mapping[str, Any], name: Any, arguments: Any) -> str:
    """调用已注册工具，并把各类失败转成结构化的错误结果。"""
    if not isinstance(name, str) or name not in tool_map:
        return _json_error("unknown_tool", "模型请求了未注册的工具。")
    if not isinstance(arguments, Mapping):
        return _json_error("invalid_arguments", "工具参数必须是对象。")
    try:
        value = await tool_map[name].ainvoke(dict(arguments))
    except ValidationError:
        return _json_error("invalid_arguments", "工具参数校验失败。")
    except MCPToolArgumentError:
        return _json_error("invalid_arguments", "工具参数校验失败。")
    except MCPTimeoutError:
        return _json_error("mcp_timeout", "MCP 工具调用超时，请稍后重试。")
    except MCPError:
        return _json_error("mcp_error", "MCP 工具执行失败，请稍后重试。")
    except Exception:
        return _json_error("tool_error", "工具执行失败，请检查参数或稍后重试。")
    return value if isinstance(value, str) else _json_result(value)


def _tool_calls(response: Any) -> list[Any]:
    calls = getattr(response, "tool_calls", None)
    return list(calls) if isinstance(calls, list) else []


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item if isinstance(item, str) else str(item.get("text", ""))
            for item in content
            if isinstance(item, (str, Mapping))
        )
    return str(content) if content is not None else ""


def _limit_text(message: Any, executed: int, tool_results: list[str]) -> str:
    text = _message_text(message)
    parts = [text] if text else []
    parts.append(f"工具调用达到上限，已安全停止（已执行 {executed} 次）。")
    if tool_results:
        parts.append("已获得的工具结果：\n" + "\n".join(tool_results))
    return "\n\n".join(parts)


def _json_result(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_error(code: str, message: str) -> str:
    return _json_result({"ok": False, "error": {"code": code, "message": message}})


def _serialize_legacy_result(value: Any) -> Any:
    if isinstance(value, OnlineSearchResult):
        return {
            "title": value.title,
            "url": value.url,
            "snippet": value.snippet,
            "published_at": value.published_at,
            "source_site": value.source_site,
            "searched_at": value.searched_at.isoformat(),
        }
    if isinstance(value, Mapping):
        return {str(key): _serialize_legacy_result(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize_legacy_result(item) for item in value]
    return value
