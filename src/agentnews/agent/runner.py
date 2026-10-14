"""LangChain tool-calling orchestration for AgentNews."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..config import Settings, get_settings
from ..tools import TavilyMCPClient, create_local_file_tools
from ..tools.path_security import ManagedPathResolver
from .models.deepseek import create_deepseek_model, map_deepseek_error


StopReason = Literal["completed", "max_tool_calls"]


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """Stable result returned by one Agent run."""

    text: str
    tool_calls: int
    stop_reason: StopReason


class WebSearchInput(BaseModel):
    """Arguments accepted by the application-level web search tool."""

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
        search_client_factory: Callable[[Settings], Any] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.model = model if model is not None else create_deepseek_model(self.settings)
        self.resolver = resolver
        self.search_client_factory = search_client_factory or TavilyMCPClient

    async def ainvoke(self, prompt: str) -> AgentRunResult:
        """Execute one prompt and its model-selected tools asynchronously."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must not be empty")

        search_client: Any | None = None
        search_started = False
        stack = AsyncExitStack()

        async def run_search(query: str, limit: int = 5) -> str:
            nonlocal search_client, search_started
            if not search_started:
                search_client = self.search_client_factory(self.settings)
                await stack.enter_async_context(search_client)
                search_started = True
            try:
                results = await search_client.search(query, limit=limit)
                return _json_result({"ok": True, "results": [_serialize_result(item) for item in results]})
            except Exception:
                return _json_error("search_error", "在线搜索工具执行失败，请稍后重试。")

        search_tool = StructuredTool.from_function(
            coroutine=run_search,
            name="web_search",
            description=(
                "Search the web for current information. Returns normalized results with "
                "titles, URLs, summaries, publication dates and source sites."
            ),
            args_schema=WebSearchInput,
            handle_validation_error=_handle_validation_error,
        )
        tools = [search_tool, *create_local_file_tools(self.resolver)]
        tool_map = {tool.name: tool for tool in tools}
        bound_model = self.model.bind_tools(tools)
        messages: list[Any] = [HumanMessage(content=prompt.strip())]
        executed = 0
        last_response: Any | None = None
        tool_results: list[str] = []

        try:
            while True:
                try:
                    response = await bound_model.ainvoke(messages)
                except Exception as error:
                    raise map_deepseek_error(error) from None
                last_response = response
                tool_calls = _tool_calls(response)
                if not tool_calls:
                    return AgentRunResult(
                        text=_message_text(response),
                        tool_calls=executed,
                        stop_reason="completed",
                    )

                if executed >= self.settings.agent_max_tool_calls:
                    return AgentRunResult(
                        text=_limit_text(response, executed, tool_results),
                        tool_calls=executed,
                        stop_reason="max_tool_calls",
                    )

                messages.append(response)
                for call in tool_calls:
                    if executed >= self.settings.agent_max_tool_calls:
                        return AgentRunResult(
                            text=_limit_text(last_response, executed, tool_results),
                            tool_calls=executed,
                            stop_reason="max_tool_calls",
                        )
                    name = _field(call, "name")
                    call_id = _field(call, "id") or f"tool-call-{executed + 1}"
                    arguments = _field(call, "args")
                    content = await _invoke_tool(tool_map, name, arguments)
                    tool_results.append(content)
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
                    )
        finally:
            try:
                await stack.aclose()
            except Exception:
                pass

    def invoke(self, prompt: str) -> AgentRunResult:
        """Synchronous convenience wrapper around :meth:`ainvoke`."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.ainvoke(prompt))
        raise RuntimeError("AgentRunner.invoke cannot run inside an active event loop; use ainvoke")


def create_agent_runner(
    model: Any | None = None,
    *,
    settings: Settings | None = None,
    resolver: ManagedPathResolver | None = None,
    search_client_factory: Callable[[Settings], Any] | None = None,
) -> AgentRunner:
    """Create a configured Agent runner."""
    return AgentRunner(
        model,
        settings=settings,
        resolver=resolver,
        search_client_factory=search_client_factory,
    )


async def _invoke_tool(tool_map: Mapping[str, Any], name: Any, arguments: Any) -> str:
    if not isinstance(name, str) or name not in tool_map:
        return _json_error("unknown_tool", "模型请求了未注册的工具。")
    if not isinstance(arguments, Mapping):
        return _json_error("invalid_arguments", "工具参数必须是对象。")
    try:
        value = await tool_map[name].ainvoke(dict(arguments))
    except ValidationError:
        return _json_error("invalid_arguments", "工具参数校验失败。")
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


def _handle_validation_error(error: ValidationError) -> str:
    return _json_error("invalid_arguments", "工具参数校验失败。")


def _json_result(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_error(code: str, message: str) -> str:
    return _json_result({"ok": False, "error": {"code": code, "message": message}})


def _serialize_result(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {key: _serialize_result(item) for key, item in asdict(value).items()}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(key): _serialize_result(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize_result(item) for item in value]
    return str(value)
