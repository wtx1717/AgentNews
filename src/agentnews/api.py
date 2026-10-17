from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime
import inspect
import json
import logging
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .agent import AgentRunner
from .config import Settings, get_settings
from .repositories import (
    InvalidReportPathError,
    ChatMessage,
    ChatSession,
    ReportStorage,
    RepositoryProtocol,
    SQLiteRepository,
    SourceArchiveStorage,
    UserPreferences,
    normalize_daily_send_time,
)
from .services.chat import (
    ChatAgentError,
    ChatService,
    ChatSessionArchivedError,
    ChatSessionNotFoundError,
)
from .services.digest import (
    DigestConfigurationError,
    DigestError,
    DigestIncompleteRunError,
    DigestNoSourcesError,
    DigestStorageError,
    DigestPreferences,
    DigestService,
)
from .services.report import ReportDeletionError, ReportService
from .services.bash import ChatRunBusyError, ChatRunManager, BashService
from .services.email import (
    EmailService,
    ReportContentUnavailableError,
    ReportNotFoundError,
    _validate_email_address,
)
from .services.scheduler import DailyDigestScheduler


logger = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatRequest(_StrictModel):
    session_id: str | None = None
    message: str = Field(min_length=1)

    @field_validator("session_id", "message")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("文本不能为空")
        return normalized


class SettingsPayload(_StrictModel):
    display_name: str = ""
    email: str = ""
    timezone: str = "Asia/Shanghai"
    topics: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    daily_send_enabled: bool = Field(default=False, strict=True)
    daily_send_time: str = "08:00"

    @field_validator("daily_send_time")
    @classmethod
    def validate_daily_send_time(cls, value: str) -> str:
        return normalize_daily_send_time(value)


class SessionTitlePayload(_StrictModel):
    title: str = Field(min_length=1)

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("标题不能为空")
        return normalized


class BashApprovalPayload(_StrictModel):
    decision: Literal["approve", "reject"]


class EmailSendPayload(_StrictModel):
    resend: bool = Field(default=False, strict=True)


def create_app(
    repository: RepositoryProtocol | None = None,
    *,
    agent_runner: AgentRunner | Any | None = None,
    digest_service: DigestService | None = None,
    email_service: EmailService | None = None,
    settings: Settings | None = None,
    scheduler: DailyDigestScheduler | Any | None = None,
    scheduler_clock: Callable[[], datetime] | None = None,
    scheduler_sleep: Callable[[float], Awaitable[None]] | None = None,
    scheduler_poll_interval: float = 30.0,
    scheduler_retry_delays: tuple[float, ...] = (1.0, 5.0),
) -> FastAPI:
    """Create an API application with replaceable service dependencies."""

    app_settings = settings or get_settings()
    app_repository = (
        repository
        if repository is not None
        else SQLiteRepository(app_settings.sqlite_path)
    )
    report_storage = ReportStorage(app_settings.report_root)
    source_archive_storage = SourceArchiveStorage(app_settings.source_archive_root)
    report_service = ReportService(
        app_repository, report_storage, source_archive_storage
    )
    email_service = email_service or EmailService(
        app_repository, report_storage, settings=app_settings
    )
    chat_service = ChatService(app_repository, agent_runner=agent_runner)
    bash_service = BashService(
        app_repository,
        report_service,
        report_storage,
        source_archive_storage,
        temp_root=app_settings.temp_root,
        working_directory=Path.cwd(),
    )
    resolved_digest_service = digest_service

    def get_digest_service() -> DigestService:
        nonlocal resolved_digest_service
        if resolved_digest_service is None:
            resolved_digest_service = DigestService(
                chat_service.get_agent_runner(),
                repository=app_repository,
                settings=app_settings,
                report_storage=report_storage,
                source_archive_storage=source_archive_storage,
            )
        return resolved_digest_service

    async def generate_digest_for_agent(topic: str, keyword: str) -> str:
        """Run one explicit digest requested by the conversational Agent."""
        preferences = app_repository.get_preferences()
        try:
            business_date = datetime.now(ZoneInfo(preferences.timezone)).date()
            result = await get_digest_service().run_once(
                DigestPreferences(
                    topics=(topic,),
                    keywords=(keyword,),
                    timezone=preferences.timezone,
                ),
                business_date,
            )
        except DigestConfigurationError:
            return _json_tool_error(
                "invalid_digest_preferences",
                "日报主题或关键词无效，未生成日报。",
            )
        except DigestNoSourcesError:
            return _json_tool_error("no_sources", "没有可靠来源，未生成日报。")
        except DigestIncompleteRunError:
            return _json_tool_error(
                "digest_incomplete",
                "日报搜索或抓取流程未完成，未保存日报。",
            )
        except DigestStorageError:
            return _json_tool_error(
                "digest_storage_error",
                "日报保存失败，未确认生成成功。",
            )
        except DigestError:
            return _json_tool_error(
                "digest_error",
                "日报流程执行失败，未确认生成成功。",
            )
        except Exception:
            logger.exception("Agent 请求日报生成失败")
            return _json_tool_error(
                "digest_error",
                "日报流程执行失败，未确认生成成功。",
            )

        report = app_repository.get_report(result.report_id)
        return _json_result(
            {
                "ok": True,
                "result": {
                    "report_id": result.report_id,
                    "title": report.title if report is not None else None,
                    "report_path": result.report_path,
                    "source_count": len(app_repository.get_report_sources(result.report_id)),
                    "source_archive_paths": list(result.source_archive_paths),
                    "tool_calls": result.tool_calls,
                    "summary": (
                        report.summary if report is not None else result.summary
                    )[:240],
                },
            }
        )

    async def send_report_email_for_agent(
        report_id: int | None,
        resend: bool,
    ) -> str:
        try:
            result = await asyncio.to_thread(
                email_service.send_report,
                report_id,
                resend=resend,
                trigger_source="natural_language",
            )
        except ReportNotFoundError:
            return _json_tool_error("report_not_found", "没有找到可发送的已完成日报。")
        except ReportContentUnavailableError:
            return _json_tool_error("report_content_unavailable", "日报正文不可用，未发送邮件。")
        return _json_result(
            {
                "ok": result.delivery.status == "sent",
                "result": {
                    "delivery": _email_delivery_to_dict(
                        result.delivery,
                        app_repository.list_email_delivery_attempts(result.delivery.id),
                    ),
                    "duplicate": result.duplicate,
                },
            }
        )

    chat_run_manager = ChatRunManager(
        app_repository,
        chat_service,
        bash_service,
        digest_handler=generate_digest_for_agent,
        email_handler=send_report_email_for_agent,
    )
    resolved_scheduler = scheduler or DailyDigestScheduler(
        app_repository,
        get_digest_service,
        email_service,
        clock=scheduler_clock,
        sleep=scheduler_sleep,
        poll_interval=scheduler_poll_interval,
        retry_delays=scheduler_retry_delays,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        resolved_scheduler.start()
        try:
            yield
        finally:
            stopped = resolved_scheduler.stop()
            if inspect.isawaitable(stopped):
                await stopped

    app = FastAPI(title="AgentNews API", version="0.1.0", lifespan=lifespan)
    static_root = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=static_root), name="static")
    app.state.repository = app_repository
    app.state.chat_service = chat_service
    app.state.bash_service = bash_service
    app.state.email_service = email_service
    app.state.chat_run_manager = chat_run_manager
    app.state.settings = app_settings
    app.state.scheduler = resolved_scheduler

    @app.get("/", include_in_schema=False)
    async def frontend() -> FileResponse:
        return FileResponse(static_root / "index.html")

    @app.exception_handler(ApiError)
    async def handle_api_error(_request: Request, error: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={"error": {"code": error.code, "message": error.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "请求参数无效",
                }
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_request: Request, _error: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "服务暂时不可用，请稍后重试。",
                }
            },
        )

    @app.post("/api/chat")
    async def chat(payload: ChatRequest) -> Response:
        try:
            run = await chat_run_manager.start(payload.message, payload.session_id)
        except ChatSessionNotFoundError as error:
            raise ApiError(404, "session_not_found", str(error)) from None
        except ChatSessionArchivedError as error:
            raise ApiError(409, "session_archived", str(error)) from None
        except ChatRunBusyError as error:
            raise ApiError(409, "run_busy", str(error)) from None
        except ChatAgentError:
            raise ApiError(
                502,
                "agent_error",
                "Agent 暂时无法处理该请求，请稍后重试。",
            ) from None
        except ValueError:
            raise ApiError(422, "validation_error", "请求参数无效") from None
        snapshot = chat_run_manager.snapshot(run.id)
        assert snapshot is not None
        if run.status == "pending_approval":
            return JSONResponse(status_code=202, content=snapshot)
        if run.status == "completed":
            return JSONResponse(status_code=200, content=snapshot["result"])
        _raise_chat_run_error(run)
        raise AssertionError("unreachable")

    @app.get("/api/chat/runs/{run_id}")
    async def get_chat_run(run_id: str) -> dict[str, Any]:
        snapshot = chat_run_manager.snapshot(run_id)
        if snapshot is None:
            raise ApiError(404, "run_not_found", "Agent 运行不存在或已过期")
        return snapshot

    @app.post("/api/chat/runs/{run_id}/approvals/{approval_id}")
    async def decide_bash_approval(
        run_id: str,
        approval_id: str,
        payload: BashApprovalPayload,
    ) -> Response:
        try:
            run = await chat_run_manager.decide(run_id, approval_id, payload.decision)
        except KeyError:
            raise ApiError(404, "approval_not_found", "审核请求不存在") from None
        except RuntimeError:
            raise ApiError(409, "approval_unavailable", "审核请求已处理或已失效") from None
        snapshot = chat_run_manager.snapshot(run.id)
        assert snapshot is not None
        return JSONResponse(status_code=202, content=snapshot)

    @app.post("/api/sessions")
    async def create_session() -> dict[str, Any]:
        return _session_to_dict(app_repository.create_session())

    @app.get("/api/sessions")
    async def list_sessions(
        status: Literal["active", "archived"] = "active",
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        sessions = app_repository.list_sessions(status=status, limit=limit, offset=offset)
        return {
            "items": [_session_to_dict(session) for session in sessions],
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        session = _find_session(app_repository, session_id)
        try:
            messages = app_repository.list_messages(session_id)
        except KeyError:
            raise ApiError(404, "session_not_found", "会话不存在") from None
        return {
            **_session_to_dict(session),
            "messages": [_message_to_dict(message) for message in messages],
        }

    @app.patch("/api/sessions/{session_id}")
    async def rename_session(
        session_id: str,
        payload: SessionTitlePayload,
    ) -> dict[str, Any]:
        _find_session(app_repository, session_id)
        try:
            session = app_repository.rename_session(session_id, payload.title)
        except (TypeError, ValueError):
            raise ApiError(422, "validation_error", "请求参数无效") from None
        except KeyError:
            raise ApiError(404, "session_not_found", "会话不存在") from None
        return _session_to_dict(session)

    @app.post("/api/sessions/{session_id}/archive")
    async def archive_session(session_id: str) -> dict[str, Any]:
        session = _find_session(app_repository, session_id)
        if session.status != "active":
            raise ApiError(409, "session_state_conflict", "会话当前不是活动状态")
        if not app_repository.archive_session(session_id):
            raise ApiError(409, "session_state_conflict", "会话状态已发生变化")
        return _session_to_dict(_find_session(app_repository, session_id))

    @app.post("/api/sessions/{session_id}/restore")
    async def restore_session(session_id: str) -> dict[str, Any]:
        session = _find_session(app_repository, session_id)
        if session.status != "archived":
            raise ApiError(409, "session_state_conflict", "会话当前不是归档状态")
        if not app_repository.restore_session(session_id):
            raise ApiError(409, "session_state_conflict", "会话状态已发生变化")
        return _session_to_dict(_find_session(app_repository, session_id))

    @app.delete("/api/sessions/{session_id}", status_code=204)
    async def delete_session(session_id: str) -> Response:
        _find_session(app_repository, session_id)
        if not app_repository.delete_session(session_id):
            raise ApiError(404, "session_not_found", "会话不存在")
        return Response(status_code=204)

    @app.get("/api/settings")
    async def get_settings_endpoint() -> dict[str, Any]:
        return _settings_response(app_repository, app_repository.get_preferences())

    @app.put("/api/settings")
    async def update_settings(payload: SettingsPayload) -> dict[str, Any]:
        try:
            existing = app_repository.get_preferences()
            values = payload.model_dump()
            if "daily_send_enabled" not in payload.model_fields_set:
                values["daily_send_enabled"] = existing.daily_send_enabled
            if "daily_send_time" not in payload.model_fields_set:
                values["daily_send_time"] = existing.daily_send_time
            preferences = UserPreferences(**values)
            if preferences.daily_send_enabled:
                _validate_automatic_send_configuration(preferences, app_settings)
            saved = app_repository.save_preferences(preferences)
        except ApiError:
            raise
        except (TypeError, ValueError):
            raise ApiError(422, "invalid_settings", "设置参数无效") from None
        return _settings_response(app_repository, saved)

    @app.post("/api/digest/run-once")
    async def run_digest_once() -> dict[str, Any]:
        preferences = app_repository.get_preferences()
        try:
            business_date = datetime.now(ZoneInfo(preferences.timezone)).date()
            result = await get_digest_service().run_once(
                DigestPreferences(
                    topics=preferences.topics,
                    keywords=preferences.keywords,
                    timezone=preferences.timezone,
                ),
                business_date,
            )
        except DigestConfigurationError:
            raise ApiError(
                422,
                "invalid_digest_preferences",
                "当前日报偏好不足以生成日报。",
            ) from None
        except DigestNoSourcesError:
            return {
                "report_id": None,
                "title": None,
                "report_path": None,
                "source_count": 0,
                "source_archive_paths": [],
                "status": "no_results",
                "message": "没有可靠来源，未生成日报。",
            }
        except DigestIncompleteRunError:
            logger.warning("日报任务未完成：工具调用达到上限")
            raise ApiError(
                502,
                "digest_incomplete",
                "日报任务未完成，工具调用达到上限，请稍后重试。",
            ) from None
        except DigestStorageError:
            logger.warning("日报任务失败：报告或来源归档保存失败")
            raise ApiError(
                502,
                "digest_storage_error",
                "日报保存失败，请检查报告目录权限后重试。",
            ) from None
        except DigestError as error:
            logger.warning(
                "日报任务失败 error_type=%s message=%s",
                type(error).__name__,
                str(error),
            )
            raise ApiError(
                502,
                "digest_error",
                "日报任务执行失败，请稍后重试。",
            ) from None
        except Exception as error:
            logger.exception("日报任务发生未预期异常 error_type=%s", type(error).__name__)
            raise ApiError(
                502,
                "digest_error",
                "日报任务执行失败，请稍后重试。",
            ) from None

        report = app_repository.get_report(result.report_id)
        if report is None:
            raise ApiError(500, "report_unavailable", "日报索引不可用。")
        return {
            "report_id": report.id,
            "title": report.title,
            "report_path": result.report_path,
            "source_count": len(app_repository.get_report_sources(report.id)),
            "source_archive_paths": list(result.source_archive_paths),
            "status": "completed",
        }

    @app.get("/api/reports")
    async def list_reports(
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        try:
            reports = app_repository.list_reports(
                start_date=start_date,
                end_date=end_date,
                limit=limit,
                offset=offset,
            )
        except ValueError:
            raise ApiError(422, "invalid_date_range", "日期范围无效") from None
        return {
            "items": [
                {
                    "id": report.id,
                    "date": report.report_date.isoformat(),
                    "title": report.title,
                    "summary": report.summary,
                    "path": report.file_path,
                    "source_count": len(app_repository.get_report_sources(report.id)),
                }
                for report in reports
            ],
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/reports/{report_id}")
    async def get_report(report_id: int) -> dict[str, Any]:
        report = _find_report(app_repository, report_id)
        try:
            markdown = report_storage.read_report(report.file_path)
        except (OSError, UnicodeError, InvalidReportPathError):
            raise ApiError(404, "report_content_not_found", "日报正文不存在") from None
        return {
            "id": report.id,
            "date": report.report_date.isoformat(),
            "title": report.title,
            "summary": report.summary,
            "path": report.file_path,
            "markdown": markdown,
            "sources": [_source_to_dict(source) for source in app_repository.get_report_sources(report.id)],
            "email_deliveries": _email_deliveries_to_dict(app_repository, report_id),
        }

    @app.get("/api/reports/{report_id}/email-deliveries")
    async def list_report_email_deliveries(report_id: int) -> dict[str, Any]:
        _find_report(app_repository, report_id)
        return {"items": _email_deliveries_to_dict(app_repository, report_id)}

    @app.post("/api/reports/{report_id}/email")
    async def send_report_email(
        report_id: int,
        payload: EmailSendPayload,
    ) -> dict[str, Any]:
        _find_report(app_repository, report_id)
        try:
            result = await asyncio.to_thread(
                email_service.send_report,
                report_id,
                resend=payload.resend,
                trigger_source="manual_api",
            )
        except ReportContentUnavailableError:
            raise ApiError(
                404, "report_content_not_found", "日报正文不可用，未发送邮件。"
            ) from None
        except ReportNotFoundError:
            raise ApiError(404, "report_not_found", "日报不存在") from None
        return {
            "ok": result.delivery.status == "sent",
            "duplicate": result.duplicate,
            "delivery": _email_delivery_to_dict(
                result.delivery,
                app_repository.list_email_delivery_attempts(result.delivery.id),
            ),
        }

    @app.delete("/api/reports/{report_id}", status_code=204)
    async def delete_report(report_id: int) -> Response:
        _find_report(app_repository, report_id)
        try:
            deleted = report_service.delete_report(report_id)
        except ReportDeletionError:
            raise ApiError(500, "report_cleanup_failed", "日报索引已删除，但文件清理失败") from None
        if not deleted:
            raise ApiError(404, "report_not_found", "日报不存在")
        return Response(status_code=204)

    @app.get("/api/sources/{source_id}")
    async def get_source(source_id: int) -> dict[str, Any]:
        if isinstance(source_id, bool) or source_id < 1:
            raise ApiError(404, "source_not_found", "来源不存在")
        source = app_repository.get_source(source_id)
        if source is None:
            raise ApiError(404, "source_not_found", "来源不存在")
        markdown = None
        if source.archive_file_path:
            try:
                markdown = source_archive_storage.read_source(source.archive_file_path)
            except (OSError, UnicodeError, ValueError):
                raise ApiError(404, "source_archive_not_found", "来源归档不存在") from None
        return {**_source_to_dict(source), "markdown": markdown}

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "dependencies": {
                "repository": "ok",
                "report_storage": "configured",
                "source_archive_storage": "configured",
                "agent": "configured"
                if app_settings.deepseek_api_key is not None and app_settings.deepseek_model
                else "unconfigured",
                "search": "configured"
                if app_settings.mcp_search_server_url is not None
                and app_settings.mcp_search_api_key is not None
                else "unconfigured",
            },
        }

    return app


def _preferences_to_dict(preferences: UserPreferences) -> dict[str, Any]:
    return {
        "display_name": preferences.display_name,
        "email": preferences.email,
        "timezone": preferences.timezone,
        "topics": list(preferences.topics),
        "keywords": list(preferences.keywords),
        "daily_send_enabled": preferences.daily_send_enabled,
        "daily_send_time": preferences.daily_send_time,
    }


def _settings_response(
    repository: RepositoryProtocol,
    preferences: UserPreferences,
) -> dict[str, Any]:
    response = _preferences_to_dict(preferences)
    recent = repository.list_scheduled_runs(limit=1)
    response["recent_scheduled_run"] = (
        _scheduled_run_to_dict(recent[0]) if recent else None
    )
    return response


def _scheduled_run_to_dict(run: Any) -> dict[str, Any]:
    return {
        "id": run.id,
        "business_date": run.business_date.isoformat(),
        "status": run.status,
        "attempt_count": run.attempt_count,
        "report_id": run.report_id,
        "email_delivery_id": run.email_delivery_id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "error_code": run.error_code,
    }


def _validate_automatic_send_configuration(
    preferences: UserPreferences,
    settings: Settings,
) -> None:
    if not preferences.email:
        raise ApiError(422, "recipient_not_configured", "启用自动发送前请配置收件邮箱")
    try:
        _validate_email_address(preferences.email)
    except ValueError:
        raise ApiError(422, "invalid_recipient", "收件邮箱格式无效") from None
    try:
        host, _port, username, password, sender = settings.require_smtp()
        _ = (host, username, password)
        _validate_email_address(sender)
    except ValueError:
        raise ApiError(422, "smtp_not_configured", "启用自动发送前请完整配置 SMTP") from None
    if not preferences.topics and not preferences.keywords:
        raise ApiError(422, "invalid_digest_preferences", "至少配置一个日报主题或关键词")


def _find_session(repository: RepositoryProtocol, session_id: str) -> ChatSession:
    if not isinstance(session_id, str) or not session_id.strip():
        raise ApiError(404, "session_not_found", "会话不存在")
    session = repository.get_session(session_id)
    if session is None:
        raise ApiError(404, "session_not_found", "会话不存在")
    return session


def _session_to_dict(session: ChatSession) -> dict[str, Any]:
    return {
        "id": session.id,
        "title": session.title,
        "status": session.status,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
        "archived_at": session.archived_at.isoformat() if session.archived_at else None,
    }


def _message_to_dict(message: ChatMessage) -> dict[str, Any]:
    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "created_at": message.created_at.isoformat(),
    }


def _find_report(repository: RepositoryProtocol, report_id: int):
    if isinstance(report_id, bool) or report_id < 1:
        raise ApiError(404, "report_not_found", "日报不存在")
    report = repository.get_report(report_id)
    if report is None:
        raise ApiError(404, "report_not_found", "日报不存在")
    return report


def _source_to_dict(source: Any) -> dict[str, Any]:
    return {
        "id": source.id,
        "report_id": source.report_id,
        "title": source.title,
        "url": source.url,
        "source_name": source.source_name,
        "published_at": source.published_at,
        "snippet": source.snippet,
        "archive_path": source.archive_file_path,
    }


def _email_deliveries_to_dict(
    repository: RepositoryProtocol,
    report_id: int,
) -> list[dict[str, Any]]:
    return [
        _email_delivery_to_dict(
            delivery,
            repository.list_email_delivery_attempts(delivery.id),
        )
        for delivery in repository.list_email_deliveries(report_id)
    ]


def _email_delivery_to_dict(delivery: Any, attempts: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": delivery.id,
        "report_id": delivery.report_id,
        "recipient": delivery.recipient,
        "subject": delivery.subject,
        "status": delivery.status,
        "error_code": delivery.error_code,
        "error_message": delivery.error_message,
        "retry_count": delivery.retry_count,
        "trigger_source": delivery.trigger_source,
        "requested_at": delivery.requested_at.isoformat(),
        "completed_at": (
            delivery.completed_at.isoformat() if delivery.completed_at is not None else None
        ),
        "attempts": [
            {
                "attempt_number": attempt.attempt_number,
                "status": attempt.status,
                "error_code": attempt.error_code,
                "error_message": attempt.error_message,
                "attempted_at": attempt.attempted_at.isoformat(),
                "completed_at": attempt.completed_at.isoformat(),
            }
            for attempt in attempts
        ],
    }


def _json_result(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_tool_error(code: str, message: str) -> str:
    return _json_result({"ok": False, "error": {"code": code, "message": message}})


def _raise_chat_run_error(run: Any) -> None:
    error = getattr(run, "error", None)
    if getattr(run, "status", None) == "expired":
        raise ApiError(409, "approval_expired", "Bash 审核已过期")
    if isinstance(error, ChatSessionNotFoundError):
        raise ApiError(404, "session_not_found", str(error))
    if isinstance(error, ChatSessionArchivedError):
        raise ApiError(409, "session_archived", str(error))
    raise ApiError(502, "agent_error", "Agent 暂时无法处理该请求，请稍后重试。")


app = create_app()


__all__ = ["ApiError", "app", "create_app"]
