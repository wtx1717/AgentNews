"""Human-in-the-loop Bash runs and report-specific command actions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
from pathlib import Path
import re
import shlex
import time
from typing import Any, Callable, Mapping, TYPE_CHECKING
from uuid import uuid4

from ..repositories import InvalidReportPathError, RepositoryProtocol, ReportStorage
from ..repositories.source_archive_storage import SourceArchiveStorage
from ..tools.bash import BashExecutor
from .report import ReportDeletionError, ReportService

if TYPE_CHECKING:
    from .chat import ChatResult, ChatService


logger = logging.getLogger(__name__)
APPROVAL_TIMEOUT_SECONDS = 5 * 60
TEMP_FILE_MAX_AGE_SECONDS = 60 * 60


class ChatRunBusyError(RuntimeError):
    """Raised when one session already has an active Agent run."""


class BashApprovalExpired(RuntimeError):
    """Raised when a user does not answer a Bash approval in time."""


@dataclass(frozen=True, slots=True)
class BashApproval:
    """一次等待用户确认的 Bash 执行请求。"""
    id: str
    run_id: str
    session_id: str
    command: str
    explanation: str
    created_at: datetime
    expires_at: datetime

    def to_dict(
        self,
        *,
        working_directory: str,
        timeout_seconds: float,
        output_limit_bytes: int,
        interpreter: str | None = None,
    ) -> dict[str, Any]:
        """转成前端展示所需的审批详情字典。"""
        return {
            "id": self.id,
            "run_id": self.run_id,
            "command": self.command,
            "explanation": self.explanation,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "working_directory": working_directory,
            "environment": "继承当前 AgentNews 进程环境变量",
            "interpreter": interpreter,
            "timeout_seconds": timeout_seconds,
            "output_limit_bytes": output_limit_bytes,
            "risk_summary": "批准后可在当前 Windows 用户权限下访问文件、环境变量和网络。",
        }


class BashAudit:
    """Emit structured, redacted audit events to a logger and optional sink."""

    def __init__(self, sink: Callable[[Mapping[str, Any]], None] | None = None) -> None:
        self.sink = sink

    def record(
        self,
        event: str,
        *,
        run_id: str,
        session_id: str,
        approval_id: str | None,
        command: str,
        explanation: str,
        status: str,
        exit_code: int | None = None,
        timed_out: bool = False,
        truncated: bool = False,
        duration_ms: int | None = None,
        error_code: str | None = None,
    ) -> None:
        """记录一条脱敏审计事件，同时写入可选 sink 与日志。"""
        payload: dict[str, Any] = {
            "event": event,
            "run_id": run_id,
            "session_id": session_id,
            "approval_id": approval_id,
            "command": _redact(command),
            "command_hash": hashlib.sha256(command.encode("utf-8")).hexdigest(),
            "explanation": _redact(explanation),
            "status": status,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "truncated": truncated,
        }
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        if error_code is not None:
            payload["error_code"] = error_code
        if self.sink is not None:
            self.sink(dict(payload))
        logger.info("bash_audit %s", json.dumps(payload, ensure_ascii=False, sort_keys=True))


class BashService:
    """Run raw PowerShell or fixed application actions after approval."""

    def __init__(
        self,
        repository: RepositoryProtocol,
        report_service: ReportService,
        report_storage: ReportStorage,
        source_archive_storage: SourceArchiveStorage,
        *,
        temp_root: str | Path,
        working_directory: str | Path,
        executor: BashExecutor | None = None,
        audit: BashAudit | None = None,
    ) -> None:
        self.repository = repository
        self.report_service = report_service
        self.report_storage = report_storage
        self.source_archive_storage = source_archive_storage
        self.temp_root = Path(temp_root).resolve()
        self.executor = executor or BashExecutor(working_directory)
        self.audit = audit or BashAudit()

    @property
    def working_directory(self) -> str:
        return str(self.executor.working_directory)

    @property
    def timeout_seconds(self) -> float:
        return self.executor.timeout_seconds

    @property
    def output_limit_bytes(self) -> int:
        return self.executor.output_limit_bytes

    @property
    def interpreter_label(self) -> str | None:
        """Describe the PowerShell entry point the executor would launch."""
        resolver = getattr(self.executor, "resolve_interpreter", None)
        if not callable(resolver):
            return None
        try:
            interpreter = resolver()
        except Exception:
            return None
        display = getattr(interpreter, "display", None)
        return display if isinstance(display, str) and display else None

    async def execute(
        self,
        command: str,
        explanation: str,
        *,
        run_id: str,
        session_id: str,
        approval_id: str,
    ) -> str:
        """执行已批准的应用程序动作或 PowerShell 命令，并记录审计事件。"""
        started = time.perf_counter()
        # 内置 agentnews 动作直接调用服务层，不经过 shell。
        builtin = _parse_builtin(command)
        if builtin is not None:
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(self._execute_builtin, builtin),
                    timeout=self.timeout_seconds,
                )
            except asyncio.TimeoutError:
                self.audit.record(
                    "execution_finished",
                    run_id=run_id,
                    session_id=session_id,
                    approval_id=approval_id,
                    command=command,
                    explanation=explanation,
                    status="timeout",
                    timed_out=True,
                    duration_ms=_elapsed_ms(started),
                    error_code="timeout",
                )
                return _json_error("timeout", "应用动作执行超时。")
            except Exception:
                self.audit.record(
                    "execution_finished",
                    run_id=run_id,
                    session_id=session_id,
                    approval_id=approval_id,
                    command=command,
                    explanation=explanation,
                    status="failed",
                    duration_ms=_elapsed_ms(started),
                    error_code="application_action_failed",
                )
                return _json_error("application_action_failed", "应用动作执行失败。")
            payload = _json(result)
            self.audit.record(
                "execution_finished",
                run_id=run_id,
                session_id=session_id,
                approval_id=approval_id,
                command=command,
                explanation=explanation,
                status="success" if result.get("ok") else "failed",
                duration_ms=_elapsed_ms(started),
                error_code=(result.get("error") or {}).get("code") if isinstance(result.get("error"), dict) else None,
            )
            return payload

        process_result = await self.executor.execute(command)
        payload = process_result.to_dict()
        error_code = getattr(process_result, "error_code", None)
        self.audit.record(
            "execution_finished",
            run_id=run_id,
            session_id=session_id,
            approval_id=approval_id,
            command=command,
            explanation=explanation,
            status="success" if process_result.ok else "failed",
            exit_code=process_result.exit_code,
            timed_out=process_result.timed_out,
            truncated=process_result.truncated,
            duration_ms=process_result.duration_ms,
            error_code=error_code,
        )
        response: dict[str, Any] = {"ok": process_result.ok, "result": payload}
        if not process_result.ok:
            message = getattr(process_result, "message", None)
            response["error"] = {
                "code": error_code or "bash_failed",
                "message": (
                    message
                    if isinstance(message, str) and message
                    else "命令执行失败，请查看 stderr。"
                ),
            }
        return _json(response)

    def _execute_builtin(self, command: tuple[str, tuple[str, ...]]) -> dict[str, Any]:
        action, args = command
        if action == "reports_list":
            options = _parse_options(args, {"--limit": True, "--offset": True})
            limit = _positive_option(options, "--limit", 20, maximum=100)
            offset = _nonnegative_option(options, "--offset", 0)
            reports = self.repository.list_reports(limit=limit, offset=offset)
            return {
                "ok": True,
                "result": {
                    "items": [
                        {
                            "id": report.id,
                            "date": report.report_date.isoformat(),
                            "title": report.title,
                            "summary": report.summary,
                            "path": report.file_path,
                            "source_count": len(self.repository.get_report_sources(report.id)),
                        }
                        for report in reports
                    ],
                    "limit": limit,
                    "offset": offset,
                },
            }
        if action == "reports_read":
            options = _parse_options(args, {"--id": True})
            report_id = _positive_option(options, "--id", None)
            report = self.repository.get_report(report_id)
            if report is None:
                return _json_error_dict("report_not_found", "日报不存在。")
            try:
                markdown = self.report_storage.read_report(report.file_path)
            except (OSError, UnicodeError, InvalidReportPathError):
                return _json_error_dict("report_content_not_found", "日报正文不存在。")
            return {
                "ok": True,
                "result": {
                    "id": report.id,
                    "date": report.report_date.isoformat(),
                    "title": report.title,
                    "summary": report.summary,
                    "path": report.file_path,
                    "markdown": markdown,
                },
            }
        if action == "reports_search":
            options = _parse_options(args, {"--keyword": True})
            keyword = options.get("--keyword")
            if not keyword or len(keyword) > 256:
                raise ValueError("--keyword must be between 1 and 256 characters")
            return self._search_reports(keyword)
        if action == "reports_delete":
            options = _parse_options(args, {"--id": True})
            report_id = _positive_option(options, "--id", None)
            try:
                deleted = self.report_service.delete_report(report_id)
            except ReportDeletionError:
                return _json_error_dict("report_cleanup_failed", "日报索引已删除，但文件清理失败。")
            if not deleted:
                return _json_error_dict("report_not_found", "日报不存在。")
            return {"ok": True, "result": {"deleted": True, "report_id": report_id}}
        if action == "temp_cleanup":
            return {"ok": True, "result": self._cleanup_temp_files()}
        raise ValueError("unknown application action")

    def _search_reports(self, keyword: str) -> dict[str, Any]:
        normalized = keyword.casefold()
        matches: list[dict[str, Any]] = []
        offset = 0
        page_size = 100
        while True:
            reports = self.repository.list_reports(limit=page_size, offset=offset)
            for report in reports:
                fields = {
                    "title": report.title,
                    "summary": report.summary,
                }
                try:
                    fields["markdown"] = self.report_storage.read_report(report.file_path)
                except (OSError, UnicodeError, InvalidReportPathError):
                    fields["markdown"] = ""
                for field_name, value in fields.items():
                    if normalized in value.casefold():
                        position = value.casefold().find(normalized)
                        matches.append(
                            {
                                "id": report.id,
                                "date": report.report_date.isoformat(),
                                "title": report.title,
                                "field": field_name,
                                "snippet": _snippet(value, position, len(keyword)),
                            }
                        )
                        break
            if len(reports) < page_size:
                break
            offset += page_size
        return {"ok": True, "result": {"keyword": keyword, "matches": matches, "count": len(matches)}}

    def _cleanup_temp_files(self) -> dict[str, Any]:
        self.temp_root.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - TEMP_FILE_MAX_AGE_SECONDS
        removed: list[str] = []
        bytes_removed = 0
        for path in self.temp_root.rglob("*"):
            # 跳过符号链接，避免误删链接指向的临时目录之外的文件。
            if not path.is_file() or path.is_symlink():
                continue
            try:
                stat = path.stat()
                if stat.st_mtime >= cutoff:
                    continue
                relative = path.relative_to(self.temp_root).as_posix()
                path.unlink()
                removed.append(relative)
                bytes_removed += stat.st_size
            except OSError:
                continue
        return {"removed": removed, "count": len(removed), "bytes_removed": bytes_removed}


@dataclass
class _RunRecord:
    id: str
    session_id: str
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_monotonic: float = field(default_factory=time.monotonic)
    finished_monotonic: float | None = None
    status: str = "running"
    task: asyncio.Task[Any] | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    result: ChatResult | None = None
    error: BaseException | None = None
    approval: BashApproval | None = None
    approval_future: asyncio.Future[str] | None = None
    error_code: str | None = None

    def elapsed_ms(self) -> int:
        """返回本轮已运行毫秒数；进入终态后冻结，避免轮询继续累加。"""
        end = (
            self.finished_monotonic
            if self.finished_monotonic is not None
            else time.monotonic()
        )
        return max(0, int((end - self.started_monotonic) * 1000))


class ChatRunManager:
    """Run ChatService tasks while exposing approval checkpoints to HTTP/UI."""

    def __init__(
        self,
        repository: RepositoryProtocol,
        chat_service: ChatService,
        bash_service: BashService,
        *,
        digest_handler: Callable[[str, str], Any] | None = None,
        email_handler: Callable[[int | None, bool], Any] | None = None,
        approval_timeout_seconds: float = APPROVAL_TIMEOUT_SECONDS,
    ) -> None:
        self.repository = repository
        self.chat_service = chat_service
        self.bash_service = bash_service
        self.digest_handler = digest_handler
        self.email_handler = email_handler
        if approval_timeout_seconds <= 0:
            raise ValueError("approval_timeout_seconds must be positive")
        self.approval_timeout_seconds = float(approval_timeout_seconds)
        self._runs: dict[str, _RunRecord] = {}
        self._active_by_session: dict[str, str] = {}

    async def start(self, message: str, session_id: str | None = None) -> _RunRecord:
        """登记并启动一次会话运行，返回可轮询状态的运行记录。"""
        if session_id is None:
            session_id = self.repository.create_session().id
        session = self.repository.get_session(session_id)
        if session is None:
            from .chat import ChatSessionNotFoundError

            raise ChatSessionNotFoundError("会话不存在")
        if session.status != "active":
            from .chat import ChatSessionArchivedError

            raise ChatSessionArchivedError("会话已归档，请先恢复")
        # 同一会话只允许一个运行，避免并发写入打乱消息顺序。
        if session_id in self._active_by_session:
            raise ChatRunBusyError("当前会话已有正在处理的消息")

        record = _RunRecord(id=str(uuid4()), session_id=session_id)
        self._runs[record.id] = record
        self._active_by_session[session_id] = record.id
        record.task = asyncio.create_task(self._run(record, message))
        await record.changed.wait()
        return record

    async def _run(self, record: _RunRecord, message: str) -> None:
        async def bash_handler(command: str, explanation: str) -> str:
            return await self._request_approval(record, command, explanation)

        try:
            run_kwargs: dict[str, Any] = {
                "bash_approval_handler": bash_handler,
                "run_id": record.id,
            }
            if self.digest_handler is not None:
                run_kwargs["digest_handler"] = self.digest_handler
            if self.email_handler is not None:
                run_kwargs["email_handler"] = self.email_handler
            record.result = await self.chat_service.run(
                message,
                record.session_id,
                **run_kwargs,
            )
            if record.status != "expired":
                record.status = "completed"
        except Exception as error:
            if record.status != "expired":
                record.error = error
                record.status = "failed"
                record.error_code = _error_code(error)
        finally:
            self._active_by_session.pop(record.session_id, None)
            if record.finished_monotonic is None:
                record.finished_monotonic = time.monotonic()
            # 唤醒等待中的请求方，避免其一直阻塞。
            record.changed.set()

    async def _request_approval(self, record: _RunRecord, command: str, explanation: str) -> str:
        """挂起运行等待用户审核，超时未答复则判定为过期。"""
        now = datetime.now(UTC)
        approval = BashApproval(
            id=str(uuid4()),
            run_id=record.id,
            session_id=record.session_id,
            command=command,
            explanation=explanation,
            created_at=now,
            expires_at=now + timedelta(seconds=self.approval_timeout_seconds),
        )
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        # 用 Future 把 HTTP 侧的用户决定回传给正在等待的协程。
        record.approval = approval
        record.approval_future = future
        record.status = "pending_approval"
        self.bash_service.audit.record(
            "approval_requested",
            run_id=record.id,
            session_id=record.session_id,
            approval_id=approval.id,
            command=command,
            explanation=explanation,
            status="pending",
        )
        record.changed.set()
        try:
            decision = await asyncio.wait_for(future, timeout=self.approval_timeout_seconds)
        except asyncio.TimeoutError:
            record.status = "expired"
            record.error_code = "approval_expired"
            record.error = BashApprovalExpired("Bash 审核已过期")
            self.bash_service.audit.record(
                "approval_expired",
                run_id=record.id,
                session_id=record.session_id,
                approval_id=approval.id,
                command=command,
                explanation=explanation,
                status="expired",
                error_code="approval_expired",
            )
            record.changed.set()
            raise BashApprovalExpired("Bash 审核已过期") from None
        finally:
            record.approval_future = None

        record.status = "running"
        record.changed.set()
        if decision == "reject":
            self.bash_service.audit.record(
                "approval_rejected",
                run_id=record.id,
                session_id=record.session_id,
                approval_id=approval.id,
                command=command,
                explanation=explanation,
                status="rejected",
            )
            record.approval = None
            return _json_error("bash_denied", "用户拒绝了这次 Bash 执行。")

        self.bash_service.audit.record(
            "approval_approved",
            run_id=record.id,
            session_id=record.session_id,
            approval_id=approval.id,
            command=command,
            explanation=explanation,
            status="approved",
        )
        result = await self.bash_service.execute(
            command,
            explanation,
            run_id=record.id,
            session_id=record.session_id,
            approval_id=approval.id,
        )
        record.approval = None
        return result

    async def decide(self, run_id: str, approval_id: str, decision: str) -> _RunRecord:
        """提交用户对某次 Bash 审核的批准或拒绝。"""
        record = self._runs.get(run_id)
        if record is None:
            raise KeyError("run not found")
        if record.status != "pending_approval" or record.approval is None:
            raise RuntimeError("run is not waiting for approval")
        if record.approval.id != approval_id or record.approval_future is None:
            raise KeyError("approval not found")
        if decision not in {"approve", "reject"}:
            raise ValueError("invalid approval decision")
        if record.approval_future.done():
            raise RuntimeError("approval was already decided")
        record.approval_future.set_result(decision)
        return record

    def snapshot(self, run_id: str) -> dict[str, Any] | None:
        """返回运行状态快照，供 HTTP/UI 轮询展示。"""
        record = self._runs.get(run_id)
        if record is None:
            return None
        payload: dict[str, Any] = {
            "run_id": record.id,
            "session_id": record.session_id,
            "status": record.status,
            "started_at": record.started_at.isoformat(),
            "elapsed_ms": record.elapsed_ms(),
        }
        if record.approval is not None and record.status == "pending_approval":
            payload["approval"] = record.approval.to_dict(
                working_directory=self.bash_service.working_directory,
                timeout_seconds=self.bash_service.timeout_seconds,
                output_limit_bytes=self.bash_service.output_limit_bytes,
                interpreter=self.bash_service.interpreter_label,
            )
        if record.result is not None:
            payload["result"] = {
                "session_id": record.result.session_id,
                "answer": record.result.answer,
                "tool_calls": record.result.tool_calls,
                "report_ids": list(record.result.report_ids),
            }
        if record.error is not None:
            payload["error"] = {
                "code": record.error_code or "agent_error",
                "message": (
                    "Bash 审核已过期。"
                    if record.error_code == "approval_expired"
                    else _safe_error_message(record.error)
                ),
            }
        return payload


def _parse_builtin(command: str) -> tuple[str, tuple[str, ...]] | None:
    try:
        tokens = tuple(shlex.split(command))
    except ValueError:
        return None
    if not tokens or tokens[0].lower() != "agentnews":
        return None
    if len(tokens) >= 3 and tokens[1].lower() == "reports":
        action = tokens[2].lower()
        builtin = {
            "list": ("reports_list", tokens[3:]),
            "read": ("reports_read", tokens[3:]),
            "search": ("reports_search", tokens[3:]),
            "delete": ("reports_delete", tokens[3:]),
        }.get(action)
        if builtin is not None:
            return builtin
        return None
    if len(tokens) == 3 and tokens[1].lower() == "temp" and tokens[2].lower() == "cleanup":
        return "temp_cleanup", ()
    return None


def _parse_options(args: tuple[str, ...], allowed: dict[str, bool]) -> dict[str, str]:
    options: dict[str, str] = {}
    index = 0
    while index < len(args):
        name = args[index]
        if name not in allowed or not allowed[name] or index + 1 >= len(args) or name in options:
            raise ValueError("invalid application action arguments")
        options[name] = args[index + 1]
        index += 2
    return options


def _positive_option(options: dict[str, str], name: str, default: int | None, *, maximum: int | None = None) -> int:
    value = options.get(name)
    if value is None:
        if default is None:
            raise ValueError(f"{name} is required")
        return default
    if not value.isascii() or not value.isdigit() or int(value) < 1:
        raise ValueError(f"{name} must be a positive integer")
    parsed = int(value)
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{name} exceeds the maximum")
    return parsed


def _nonnegative_option(options: dict[str, str], name: str, default: int) -> int:
    value = options.get(name)
    if value is None:
        return default
    if not value.isascii() or not value.isdigit():
        raise ValueError(f"{name} must be a non-negative integer")
    return int(value)


def _snippet(value: str, position: int, length: int) -> str:
    start = max(0, position - 120)
    end = min(len(value), position + length + 180)
    snippet = value[start:end].replace("\x00", "")
    return ("…" if start else "") + snippet + ("…" if end < len(value) else "")


def _cleanup_error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _json_error(code: str, message: str) -> str:
    return _json(_cleanup_error(code, message))


def _json_error_dict(code: str, message: str) -> dict[str, Any]:
    return _cleanup_error(code, message)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _redact(value: str) -> str:
    # 抹掉命令行里的密钥类参数，审计日志不保留明文。
    return re.sub(
        r"(?i)((?:api[_-]?key|token|password|passwd|secret)\s*(?:=|:)\s*)([^\s;]+)",
        r"\1[REDACTED]",
        value,
    )


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _error_code(error: BaseException) -> str:
    if isinstance(error, ChatRunBusyError):
        return "run_busy"
    if isinstance(error, BashApprovalExpired):
        return "approval_expired"
    return "agent_error"


def _safe_error_message(error: BaseException) -> str:
    if getattr(error, "error_code", None) == "approval_expired":
        return "Bash 审核已过期。"
    if isinstance(error, ChatRunBusyError):
        return str(error)
    if isinstance(error, BashApprovalExpired):
        return "Bash 审核已过期。"
    return "Agent 暂时无法处理该请求，请稍后重试。"
