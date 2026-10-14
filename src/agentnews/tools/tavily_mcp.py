from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from agentnews.config import Settings, get_settings

_TEXT_RESULT_SPLIT = re.compile(r"(?im)(?=^\s*(?:\d+[.)]\s+|Title\s*:))")
_TEXT_FIELD = re.compile(
    r"(?im)^\s*(?:\d+[.)]\s*)?(Title|URL|Content|Snippet|Raw Content|Published Date|Published At)\s*:\s*(.*)$"
)


class SearchError(RuntimeError):
    """Base class for online search failures."""


class SearchConfigurationError(SearchError):
    """Raised when the MCP search service is not configured."""


class SearchTimeoutError(SearchError):
    """Raised when an MCP search operation exceeds its timeout."""


class SearchUnavailableError(SearchError):
    """Raised when the MCP search service cannot complete a request."""


class SearchToolDiscoveryError(SearchError):
    """Raised when the server does not advertise one unique Tavily search tool."""


class SearchToolSchemaError(SearchError):
    """Raised when the advertised search tool cannot represent a news search."""


@dataclass(frozen=True, slots=True)
class OnlineSearchResult:
    title: str
    url: str
    snippet: str
    published_at: str | None
    source_site: str
    searched_at: datetime


class TavilyMCPClient:
    """Async Tavily search adapter backed by its hosted MCP endpoint."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None
        self._tool_name: str | None = None
        self._input_schema: dict[str, Any] | None = None

    async def __aenter__(self) -> TavilyMCPClient:
        try:
            url, api_key = self._settings.require_search()
        except ValueError as exc:
            raise SearchConfigurationError(str(exc)) from None

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
            self._client = Client(transport, read_timeout_seconds=timeout)
            async with asyncio.timeout(timeout):
                await stack.enter_async_context(self._client)
                self._tool_name, self._input_schema = await _discover_search_tool(self._client)
                _validate_search_schema(self._input_schema)
        except SearchError:
            await self._reset_failed_enter(stack)
            raise
        except (TimeoutError, httpx2.TimeoutException):
            await self._reset_failed_enter(stack)
            raise SearchTimeoutError("在线搜索服务初始化或工具发现超时") from None
        except Exception:
            await self._reset_failed_enter(stack)
            raise SearchUnavailableError("在线搜索服务暂不可用") from None
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        stack, self._stack = self._stack, None
        self._client = None
        self._tool_name = None
        self._input_schema = None
        if stack is not None:
            await stack.aclose()

    async def _reset_failed_enter(self, stack: AsyncExitStack) -> None:
        try:
            await stack.aclose()
        except Exception:
            pass
        self._stack = None
        self._client = None
        self._tool_name = None
        self._input_schema = None

    async def search(self, query: str, limit: int = 5) -> list[OnlineSearchResult]:
        if self._client is None:
            raise RuntimeError("TavilyMCPClient must be used within an async context manager")
        if self._tool_name is None or self._input_schema is None:
            raise RuntimeError("Tavily search tool was not discovered for this connection")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must not be empty")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 5 <= limit <= 20:
            raise ValueError("limit must be an integer between 5 and 20")
        arguments = _build_search_arguments(self._input_schema, query.strip(), limit)

        try:
            async with asyncio.timeout(self._settings.mcp_search_timeout):
                response = await self._client.call_tool(
                    self._tool_name,
                    arguments,
                )
        except (TimeoutError, httpx2.TimeoutException):
            raise SearchTimeoutError("在线搜索请求超时") from None
        except Exception:
            raise SearchUnavailableError("在线搜索服务暂不可用") from None

        if getattr(response, "is_error", getattr(response, "isError", False)):
            raise SearchUnavailableError("在线搜索服务返回错误")
        return _standardize_results(response)[:limit]


async def _discover_search_tool(client: Client) -> tuple[str, dict[str, Any]]:
    tools = []
    cursor = None
    seen_cursors: set[str] = set()
    while True:
        page = await client.list_tools(cursor=cursor)
        tools.extend(_read_field(page, "tools") or [])
        cursor = _read_field(page, "next_cursor", "nextCursor")
        if not cursor:
            break
        if cursor in seen_cursors:
            raise SearchToolDiscoveryError("MCP 工具列表分页游标重复，无法完成工具发现")
        seen_cursors.add(cursor)

    candidates = [tool for tool in tools if _is_tavily_search_tool(tool)]
    if not candidates:
        raise SearchToolDiscoveryError("MCP 服务未公布明确的 Tavily 搜索工具（候选数：0）")
    if len(candidates) != 1:
        raise SearchToolDiscoveryError(
            f"MCP 服务公布了多个 Tavily 搜索工具（候选数：{len(candidates)}）"
        )

    name = _read_field(candidates[0], "name")
    if not isinstance(name, str) or not name:
        raise SearchToolDiscoveryError("MCP Tavily 搜索工具缺少有效名称")
    schema = _read_field(candidates[0], "input_schema", "inputSchema")
    if hasattr(schema, "model_dump"):
        schema = schema.model_dump(by_alias=True, exclude_none=True)
    if not isinstance(schema, Mapping):
        raise SearchToolSchemaError("Tavily 搜索工具未公布有效 inputSchema")
    return name, dict(schema)


def _is_tavily_search_tool(tool: Any) -> bool:
    metadata = " ".join(
        value
        for value in (
            _read_field(tool, "name"),
            _read_field(tool, "title"),
            _read_field(tool, "description"),
        )
        if isinstance(value, str)
    )
    expanded = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", metadata)
    tokens = set(re.findall(r"[a-z0-9]+", expanded.casefold()))
    return "tavily" in tokens and "search" in tokens


def _validate_search_schema(schema: Mapping[str, Any]) -> None:
    if schema.get("type") not in (None, "object"):
        raise SearchToolSchemaError("Tavily 搜索工具 inputSchema 不是 object")
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        raise SearchToolSchemaError("Tavily 搜索工具 inputSchema 缺少 properties")

    query_key = _select_schema_key(properties, ("query", "search_query", "q"))
    query_schema = properties.get(query_key) if query_key is not None else None
    if not isinstance(query_schema, Mapping) or not _has_type(query_schema, "string"):
        raise SearchToolSchemaError("Tavily 搜索工具 schema 不支持字符串查询参数")

    topic_schema = properties.get("topic")
    if (
        not isinstance(topic_schema, Mapping)
        or not _has_type(topic_schema, "string")
        or not _schema_accepts(topic_schema, "news")
    ):
        raise SearchToolSchemaError("Tavily 搜索工具 schema 不支持 topic=news")

    limit_key = _select_schema_key(properties, ("max_results", "limit"))
    if limit_key is not None and (
        not isinstance(properties[limit_key], Mapping)
        or not _has_numeric_type(properties[limit_key])
    ):
        raise SearchToolSchemaError("Tavily 搜索工具 schema 的结果数量字段无效")

    depth_schema = properties.get("search_depth")
    depth_supported = (
        isinstance(depth_schema, Mapping)
        and _has_type(depth_schema, "string")
        and _schema_accepts(depth_schema, "basic")
    )
    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        raise SearchToolSchemaError("Tavily 搜索工具 schema 的 required 字段无效")
    if "search_depth" in required and not depth_supported:
        raise SearchToolSchemaError("Tavily 搜索工具 schema 要求不兼容的搜索深度")

    supported = {query_key, "topic"}
    if limit_key is not None:
        supported.add(limit_key)
    if depth_supported:
        supported.add("search_depth")
    if any(item not in supported for item in required):
        raise SearchToolSchemaError("Tavily 搜索工具 schema 要求不支持的参数")


def _build_search_arguments(
    schema: Mapping[str, Any], query: str, limit: int
) -> dict[str, Any]:
    _validate_search_schema(schema)
    properties = schema.get("properties")
    assert isinstance(properties, Mapping)

    query_key = _select_schema_key(properties, ("query", "search_query", "q"))
    assert query_key is not None
    query_schema = properties[query_key]
    assert isinstance(query_schema, Mapping)
    if not _schema_accepts(query_schema, query):
        raise SearchToolSchemaError("查询内容不符合 Tavily 工具 schema")

    arguments: dict[str, Any] = {query_key: query, "topic": "news"}
    limit_key = _select_schema_key(properties, ("max_results", "limit"))
    if limit_key is not None:
        limit_schema = properties[limit_key]
        assert isinstance(limit_schema, Mapping)
        if not _schema_accepts(limit_schema, limit):
            raise SearchToolSchemaError("请求的结果数量不符合 Tavily 工具 schema")
        arguments[limit_key] = limit

    depth_schema = properties.get("search_depth")
    if isinstance(depth_schema, Mapping) and _has_type(depth_schema, "string") and _schema_accepts(depth_schema, "basic"):
        arguments["search_depth"] = "basic"

    return arguments


def _select_schema_key(properties: Mapping[str, Any], choices: tuple[str, ...]) -> str | None:
    return next((name for name in choices if name in properties), None)


def _has_type(field_schema: Mapping[str, Any], expected: str) -> bool:
    declared = field_schema.get("type")
    return declared is None or declared == expected or (
        isinstance(declared, list) and expected in declared
    )


def _has_numeric_type(field_schema: Mapping[str, Any]) -> bool:
    declared = field_schema.get("type")
    if declared is None:
        return True
    if isinstance(declared, str):
        return declared in {"integer", "number"}
    if isinstance(declared, list):
        return any(isinstance(item, str) and item in {"integer", "number"} for item in declared)
    return False


def _schema_accepts(field_schema: Mapping[str, Any], value: Any) -> bool:
    if "const" in field_schema and field_schema["const"] != value:
        return False
    if "enum" in field_schema:
        choices = field_schema["enum"]
        if not isinstance(choices, list) or value not in choices:
            return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        for key, check in (
            ("minimum", lambda bound: value >= bound),
            ("maximum", lambda bound: value <= bound),
            ("exclusiveMinimum", lambda bound: value > bound),
            ("exclusiveMaximum", lambda bound: value < bound),
        ):
            bound = field_schema.get(key)
            if isinstance(bound, (int, float)) and not isinstance(bound, bool) and not check(bound):
                return False
    if isinstance(value, str):
        minimum = field_schema.get("minLength")
        maximum = field_schema.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            return False
        if isinstance(maximum, int) and len(value) > maximum:
            return False
    return True


def _read_field(value: Any, *names: str) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        result = getattr(value, name, None)
        if result is not None:
            return result
    return None


def _standardize_results(response: Any) -> list[OnlineSearchResult]:
    structured = getattr(response, "structured_content", None)
    if structured is None:
        structured = getattr(response, "structuredContent", None)
    records = _records_from_payload(structured)
    if records is None:
        text = "\n".join(
            block.text
            for block in getattr(response, "content", [])
            if getattr(block, "type", None) == "text" and isinstance(getattr(block, "text", None), str)
        )
        records = _records_from_text(text)

    searched_at = datetime.now(UTC)
    results = []
    for record in records:
        result = _normalize_record(record, searched_at)
        if result is not None:
            results.append(result)
    return results


def _records_from_payload(payload: Any) -> list[dict[str, Any]] | None:
    if isinstance(payload, dict):
        records = payload.get("results")
        if records is None and "url" in payload:
            records = [payload]
    elif isinstance(payload, list):
        records = payload
    else:
        return None
    if not isinstance(records, list):
        return None
    return [record for record in records if isinstance(record, dict)]


def _records_from_text(text: str) -> list[dict[str, str]]:
    if not text.strip():
        return []
    try:
        records = _records_from_payload(json.loads(text))
    except json.JSONDecodeError:
        records = None
    if records is not None:
        return records

    parsed = []
    for section in _TEXT_RESULT_SPLIT.split(text):
        fields: dict[str, str] = {}
        current_field: str | None = None
        for line in section.splitlines():
            match = _TEXT_FIELD.match(line)
            if match:
                current_field = match.group(1).lower().replace(" ", "_")
                fields[current_field] = match.group(2).strip()
            elif current_field and line.strip():
                fields[current_field] += f"\n{line.strip()}"
        if fields.get("url"):
            parsed.append(fields)
    return parsed


def _normalize_record(record: dict[str, Any], searched_at: datetime) -> OnlineSearchResult | None:
    title = _as_text(record.get("title"))
    url = _as_text(record.get("url"))
    if not title or not url:
        return None
    parsed_url = urlsplit(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        return None

    snippet = _as_text(record.get("content")) or _as_text(record.get("snippet"))
    snippet = snippet or _as_text(record.get("raw_content"))
    published_at = _as_text(record.get("published_date")) or _as_text(record.get("published_at"))
    return OnlineSearchResult(
        title=title,
        url=url,
        snippet=snippet,
        published_at=published_at or None,
        source_site=parsed_url.hostname,
        searched_at=searched_at,
    )


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return ""
