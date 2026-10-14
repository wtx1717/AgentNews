from __future__ import annotations

import asyncio
import json
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from agentnews.config import Settings, get_settings

_TOOL_NAME = "tavily-search"
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
        except (TimeoutError, httpx2.TimeoutException):
            await stack.aclose()
            self._stack = None
            self._client = None
            raise SearchTimeoutError("在线搜索服务连接超时") from None
        except Exception:
            await stack.aclose()
            self._stack = None
            self._client = None
            raise SearchUnavailableError("在线搜索服务暂不可用") from None
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        stack, self._stack = self._stack, None
        self._client = None
        if stack is not None:
            await stack.aclose()

    async def search(self, query: str, limit: int = 5) -> list[OnlineSearchResult]:
        if self._client is None:
            raise RuntimeError("TavilyMCPClient must be used within an async context manager")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must not be empty")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 5 <= limit <= 20:
            raise ValueError("limit must be an integer between 5 and 20")

        try:
            async with asyncio.timeout(self._settings.mcp_search_timeout):
                response = await self._client.call_tool(
                    _TOOL_NAME,
                    {
                        "query": query.strip(),
                        "max_results": limit,
                        "topic": "news",
                        "search_depth": "basic",
                    },
                )
        except (TimeoutError, httpx2.TimeoutException):
            raise SearchTimeoutError("在线搜索请求超时") from None
        except Exception:
            raise SearchUnavailableError("在线搜索服务暂不可用") from None

        if getattr(response, "is_error", getattr(response, "isError", False)):
            raise SearchUnavailableError("在线搜索服务返回错误")
        return _standardize_results(response)[:limit]


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
