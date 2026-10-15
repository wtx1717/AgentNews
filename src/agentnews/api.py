from __future__ import annotations

from datetime import date, datetime
import logging
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .agent import AgentRunner
from .config import Settings, get_settings
from .repositories import (
    InvalidReportPathError,
    InMemoryRepository,
    ReportStorage,
    RepositoryProtocol,
    SourceArchiveStorage,
    UserPreferences,
)
from .services.chat import ChatAgentError, ChatService, ChatSessionNotFoundError
from .services.digest import (
    DigestConfigurationError,
    DigestError,
    DigestIncompleteRunError,
    DigestNoSourcesError,
    DigestStorageError,
    DigestPreferences,
    DigestService,
)


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


def create_app(
    repository: RepositoryProtocol | None = None,
    *,
    agent_runner: AgentRunner | Any | None = None,
    digest_service: DigestService | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    """Create an API application with replaceable service dependencies."""

    app_settings = settings or get_settings()
    app_repository = repository or InMemoryRepository()
    report_storage = ReportStorage(app_settings.report_root)
    source_archive_storage = SourceArchiveStorage(app_settings.source_archive_root)
    chat_service = ChatService(app_repository, agent_runner=agent_runner)
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

    app = FastAPI(title="AgentNews API", version="0.1.0")
    static_root = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=static_root), name="static")
    app.state.repository = app_repository
    app.state.chat_service = chat_service
    app.state.settings = app_settings

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
    async def chat(payload: ChatRequest) -> dict[str, Any]:
        try:
            result = await chat_service.run(payload.message, payload.session_id)
        except ChatSessionNotFoundError as error:
            raise ApiError(404, "session_not_found", str(error)) from None
        except ChatAgentError:
            raise ApiError(
                502,
                "agent_error",
                "Agent 暂时无法处理该请求，请稍后重试。",
            ) from None
        except ValueError:
            raise ApiError(422, "validation_error", "请求参数无效") from None
        return {
            "session_id": result.session_id,
            "answer": result.answer,
            "tool_calls": result.tool_calls,
            "report_ids": list(result.report_ids),
        }

    @app.get("/api/settings")
    async def get_settings_endpoint() -> dict[str, Any]:
        return _preferences_to_dict(app_repository.get_preferences())

    @app.put("/api/settings")
    async def update_settings(payload: SettingsPayload) -> dict[str, Any]:
        try:
            preferences = UserPreferences(**payload.model_dump())
            saved = app_repository.save_preferences(preferences)
        except (TypeError, ValueError):
            raise ApiError(422, "invalid_settings", "设置参数无效") from None
        return _preferences_to_dict(saved)

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
        }

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


app = create_app()


__all__ = ["ApiError", "app", "create_app"]
