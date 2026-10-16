from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import inspect
import json
from typing import Any

from ..agent import AgentRunResult, AgentRunner, create_agent_runner
from ..repositories import ChatMessage, RepositoryProtocol, SessionArchivedError


class ChatError(RuntimeError):
    """Base class for ordinary chat failures."""


class ChatSessionNotFoundError(ChatError):
    """The requested chat session does not exist."""


class ChatSessionArchivedError(ChatError):
    """The requested chat session is archived and cannot accept messages."""


class ChatAgentError(ChatError):
    """The agent could not produce a safe answer."""


@dataclass(frozen=True, slots=True)
class ChatResult:
    session_id: str
    answer: str
    tool_calls: int
    report_ids: tuple[int, ...] = ()


class ChatService:
    """Coordinate ordinary chat without reading saved user preferences."""

    def __init__(
        self,
        repository: RepositoryProtocol,
        agent_runner: AgentRunner | Any | None = None,
        *,
        runner_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.repository = repository
        self._agent_runner = agent_runner
        self._runner_factory = runner_factory or create_agent_runner

    def get_agent_runner(self) -> Any:
        if self._agent_runner is None:
            self._agent_runner = self._runner_factory()
        return self._agent_runner

    async def run(
        self,
        message: str,
        session_id: str | None = None,
        *,
        bash_approval_handler: Callable[[str, str], Any] | None = None,
        digest_handler: Callable[[str, str], Any] | None = None,
        run_id: str | None = None,
    ) -> ChatResult:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must not be empty")
        normalized_message = message.strip()

        if session_id is None:
            active_session_id = self.repository.create_session().id
            history: tuple[ChatMessage, ...] = ()
        else:
            session = self.repository.get_session(session_id)
            if session is None:
                raise ChatSessionNotFoundError("会话不存在") from None
            if session.status != "active":
                raise ChatSessionArchivedError("会话已归档，请先恢复") from None
            try:
                history = self.repository.list_messages(session_id)
            except KeyError:
                raise ChatSessionNotFoundError("会话不存在") from None
            active_session_id = session_id

        context = _render_history(history)
        try:
            self.repository.append_message(active_session_id, "user", normalized_message)
        except SessionArchivedError:
            raise ChatSessionArchivedError("会话已归档，请先恢复") from None
        try:
            runner = self.get_agent_runner()
            invoke_kwargs: dict[str, Any] = {"task_context": context or None}
            if bash_approval_handler is not None and _accepts_keyword(
                runner.ainvoke, "bash_approval_handler"
            ):
                invoke_kwargs["bash_approval_handler"] = bash_approval_handler
            if digest_handler is not None and _accepts_keyword(
                runner.ainvoke, "digest_handler"
            ):
                invoke_kwargs["digest_handler"] = digest_handler
            run = await runner.ainvoke(normalized_message, **invoke_kwargs)
            if not isinstance(run, AgentRunResult):
                raise TypeError("invalid agent result")
            answer = run.text.strip()
            if not answer:
                raise ValueError("empty agent answer")
        except Exception as error:
            self.repository.append_message(
                active_session_id,
                "assistant",
                "Agent 暂时无法处理该请求，请稍后重试。",
                {"error_code": "agent_error"},
            )
            if isinstance(error, ChatAgentError):
                raise
            raise ChatAgentError("Agent 暂时无法处理该请求，请稍后重试。") from None

        self.repository.append_message(
            active_session_id,
            "assistant",
            answer,
            {"tool_calls": run.tool_calls, "stop_reason": run.stop_reason},
        )
        return ChatResult(
            session_id=active_session_id,
            answer=answer,
            tool_calls=run.tool_calls,
            report_ids=_extract_report_ids(run),
        )


def _render_history(messages: Sequence[ChatMessage]) -> str:
    lines = []
    for message in messages:
        if message.role not in {"user", "assistant"}:
            continue
        lines.append(f"{message.role}: {message.content}")
    return "\n".join(lines)


def _accepts_keyword(function: Any, name: str) -> bool:
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD or parameter.name == name
        for parameter in parameters
    )


def _extract_report_ids(run: AgentRunResult) -> tuple[int, ...]:
    """Expose successful digest IDs without deciding which tools the model calls."""
    report_ids: list[int] = []
    for record in run.tool_call_records:
        if record.name != "generate_digest":
            continue
        try:
            payload = json.loads(record.result)
            report_id = payload.get("result", {}).get("report_id")
        except (TypeError, json.JSONDecodeError, AttributeError):
            continue
        if isinstance(report_id, int) and not isinstance(report_id, bool) and report_id > 0:
            if report_id not in report_ids:
                report_ids.append(report_id)
    return tuple(report_ids)
