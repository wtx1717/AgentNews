from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from agentnews.agent import AgentRunResult
from agentnews import api as api_module
from agentnews.api import create_app
from agentnews.config import Settings
from agentnews.repositories import (
    InMemoryRepository,
    ReportStorage,
    SourceArchiveStorage,
    UserPreferences,
)
from agentnews.services.digest import DigestNoSourcesError, DigestResult


class FakeRunner:
    def __init__(self, result=None, error: Exception | None = None):
        self.result = result or AgentRunResult("回答", 0, "completed")
        self.error = error
        self.calls = []

    async def ainvoke(self, prompt, *, system_prompt=None, task_context=None):
        self.calls.append((prompt, system_prompt, task_context))
        if self.error:
            raise self.error
        return self.result


class FakeDigestService:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def run_once(self, preferences, business_date):
        self.calls.append((preferences, business_date))
        return self.result


class NoSourcesDigestService:
    async def run_once(self, preferences, business_date):
        raise DigestNoSourcesError("没有可靠来源")


class RecordingRepository(InMemoryRepository):
    def __init__(self):
        super().__init__()
        self.created_session_ids = []

    def create_session(self, title=None):
        session = super().create_session(title)
        self.created_session_ids.append(session.id)
        return session


def _settings(tmp_path):
    return Settings(
        _env_file=None,
        deepseek_api_key=None,
        deepseek_model=None,
        mcp_search_server_url=None,
        mcp_search_api_key=None,
        report_root=tmp_path / "reports",
        source_archive_root=tmp_path / "source_archive",
        sqlite_path=tmp_path / "agentnews.sqlite3",
    )


def test_chat_settings_health_and_session_context(tmp_path):
    repository = InMemoryRepository()
    runner = FakeRunner(AgentRunResult("收到", 2, "completed"))
    client = TestClient(create_app(repository, agent_runner=runner, settings=_settings(tmp_path)))

    response = client.put(
        "/api/settings",
        json={
            "display_name": "用户",
            "email": "user@example.test",
            "timezone": "Asia/Shanghai",
            "topics": ["不会自动注入"],
            "keywords": ["关键词"],
        },
    )
    assert response.status_code == 200
    assert client.get("/api/settings").json()["topics"] == ["不会自动注入"]

    first = client.post("/api/chat", json={"message": "你好"})
    assert first.status_code == 200
    payload = first.json()
    assert payload["session_id"]
    assert payload["answer"] == "收到"
    assert payload["tool_calls"] == 2
    assert payload["report_ids"] == []
    second = client.post(
        "/api/chat",
        json={"session_id": payload["session_id"], "message": "继续"},
    )
    assert second.status_code == 200
    assert "不会自动注入" not in (runner.calls[1][2] or "")
    assert "user: 你好" in (runner.calls[1][2] or "")
    assert len(repository.list_messages(payload["session_id"])) == 4

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["dependencies"]["agent"] == "unconfigured"
    assert "API_KEY" not in health.text


def test_default_sqlite_application_persists_across_instances(tmp_path):
    settings = _settings(tmp_path)
    first_runner = FakeRunner(AgentRunResult("第一条", 0, "completed"))
    first = TestClient(create_app(settings=settings, agent_runner=first_runner))

    assert first.put(
        "/api/settings",
        json={
            "display_name": "持久化用户",
            "email": "persist@example.test",
            "timezone": "Asia/Shanghai",
            "topics": ["AI"],
            "keywords": ["Agent"],
        },
    ).status_code == 200
    chat = first.post("/api/chat", json={"message": "持久化消息"})
    assert chat.status_code == 200
    session_id = chat.json()["session_id"]

    second_app = create_app(
        settings=settings,
        agent_runner=FakeRunner(AgentRunResult("第二条", 0, "completed")),
    )
    second = TestClient(second_app)
    assert second.get("/api/settings").json()["display_name"] == "持久化用户"
    assert second.get("/api/settings").json()["topics"] == ["AI"]
    assert second_app.state.repository.list_messages(session_id)[0].content == "持久化消息"


def test_frontend_and_static_assets_are_served_without_secrets_or_browser_storage(tmp_path):
    client = TestClient(create_app(settings=_settings(tmp_path), agent_runner=FakeRunner()))

    page = client.get("/")
    stylesheet = client.get("/static/app.css")
    script = client.get("/static/app.js")

    assert page.status_code == stylesheet.status_code == script.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "text/css" in stylesheet.headers["content-type"]
    assert "javascript" in script.headers["content-type"]
    assert '/static/app.css' in page.text
    assert '/static/app.js' in page.text

    static_root = Path(api_module.__file__).parent / "static"
    frontend_source = "\n".join(path.read_text(encoding="utf-8") for path in static_root.iterdir())
    lowered_source = frontend_source.lower()
    for forbidden in ("localstorage", "sessionstorage", "api_key", "smtp", "password", "secret"):
        assert forbidden not in lowered_source


def test_chat_errors_are_safe_and_messages_are_saved(tmp_path):
    repository = RecordingRepository()
    runner = FakeRunner(error=RuntimeError("secret API key and stack"))
    client = TestClient(create_app(repository, agent_runner=runner, settings=_settings(tmp_path)))

    response = client.post("/api/chat", json={"message": "失败测试"})
    assert response.status_code == 502
    assert response.json() == {
        "error": {
            "code": "agent_error",
            "message": "Agent 暂时无法处理该请求，请稍后重试。",
        }
    }
    session_id = repository.created_session_ids[0]
    messages = repository.list_messages(session_id)
    assert [message.role for message in messages] == ["user", "assistant"]
    assert "secret" not in response.text


def test_reports_and_sources_are_read_from_indexed_paths(tmp_path):
    settings = _settings(tmp_path)
    repository = InMemoryRepository(report_id_factory=lambda: 1)
    reservation = repository.reserve_report(date(2026, 10, 14), "2026-10-14 新闻日报")
    report_storage = ReportStorage(settings.report_root)
    stored_report = report_storage.save_report(
        reservation.id,
        reservation.report_date,
        reservation.title,
        "# 日报\n\n正文",
    )
    archive_storage = SourceArchiveStorage(settings.source_archive_root)
    stored_source = archive_storage.save_source(
        1,
        1,
        date(2026, 10, 14),
        "来源标题",
        "https://example.com/news",
        "example.com",
        "2026-10-14T00:00:00Z",
        None,
        "来源正文",
    )
    repository.add_report_source(
        1,
        "来源标题",
        "https://example.com/news",
        "example.com",
        archive_file_path=stored_source.relative_path,
    )
    repository.finalize_report(1, stored_report.relative_path, "摘要", stored_report.content_hash)

    client = TestClient(create_app(repository, settings=settings, agent_runner=FakeRunner()))
    listing = client.get("/api/reports?start_date=2026-10-14&end_date=2026-10-14")
    assert listing.status_code == 200
    assert listing.json()["items"][0]["source_count"] == 1
    detail = client.get("/api/reports/1")
    assert detail.status_code == 200
    assert detail.json()["markdown"] == "# 日报\n\n正文"
    source = client.get("/api/sources/1")
    assert source.status_code == 200
    assert source.json()["markdown"].endswith("来源正文")
    assert client.get("/api/sources/999").status_code == 404


def test_digest_endpoint_uses_saved_timezone_and_returns_report(tmp_path):
    settings = _settings(tmp_path)
    repository = InMemoryRepository(report_id_factory=lambda: 1)
    repository.save_preferences(
        UserPreferences(
            timezone="Asia/Shanghai", topics=["AI"], keywords=["Agent"]
        )
    )
    reservation = repository.reserve_report(date(2026, 10, 15), "2026-10-15 新闻日报")
    result = DigestResult(1, "2026-10-15/1-news.md", ("2026-10-15/1-1-source.md",), 3, "日报")
    repository.finalize_report(1, result.report_path, "日报", "hash")
    digest = FakeDigestService(result)
    client = TestClient(
        create_app(repository, digest_service=digest, agent_runner=FakeRunner(), settings=settings)
    )

    response = client.post("/api/digest/run-once")
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["report_id"] == 1
    assert digest.calls[0][0].topics == ("AI",)
    assert digest.calls[0][1] == date(2026, 10, 15)


def test_digest_without_reliable_sources_returns_no_results_without_indexing(tmp_path):
    repository = InMemoryRepository()
    repository.save_preferences(UserPreferences(topics=["AI"], timezone="Asia/Shanghai"))
    client = TestClient(
        create_app(
            repository,
            digest_service=NoSourcesDigestService(),
            agent_runner=FakeRunner(),
            settings=_settings(tmp_path),
        )
    )

    response = client.post("/api/digest/run-once")

    assert response.status_code == 200
    assert response.json() == {
        "report_id": None,
        "title": None,
        "report_path": None,
        "source_count": 0,
        "source_archive_paths": [],
        "status": "no_results",
        "message": "没有可靠来源，未生成日报。",
    }
    assert repository.list_reports() == ()


def test_validation_and_missing_session_use_unified_errors(tmp_path):
    client = TestClient(create_app(settings=_settings(tmp_path), agent_runner=FakeRunner()))
    assert client.post("/api/chat", json={"message": " "}).status_code == 422
    missing = client.post(
        "/api/chat", json={"session_id": "missing", "message": "你好"}
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "session_not_found"
