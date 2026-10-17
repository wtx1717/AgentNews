from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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


def test_chat_exposes_one_digest_tool_call_to_the_model(tmp_path):
    class DigestRunner:
        async def ainvoke(self, prompt, *, task_context=None, bash_approval_handler=None, digest_handler=None):
            assert digest_handler is not None
            tool_result = await digest_handler("比亚迪", "专利")
            return AgentRunResult(tool_result, 1, "completed")

    digest = FakeDigestService(
        DigestResult(
            report_id=1,
            report_path="2026-10-16/1-2026-10-16-新闻日报.md",
            source_archive_paths=(),
            tool_calls=2,
            final_text="日报正文",
        )
    )
    repository = InMemoryRepository()
    client = TestClient(
        create_app(
            repository,
            agent_runner=DigestRunner(),
            digest_service=digest,
            settings=_settings(tmp_path),
        )
    )

    response = client.post("/api/chat", json={"message": "生成一份比亚迪专利日报"})

    assert response.status_code == 200
    assert '"report_id":1' in response.json()["answer"]
    assert len(digest.calls) == 1
    preferences, _business_date = digest.calls[0]
    assert preferences.topics == ("比亚迪",)
    assert preferences.keywords == ("专利",)


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
    markdown_script = client.get("/static/markdown-render.js")

    assert page.status_code == stylesheet.status_code == script.status_code == 200
    assert markdown_script.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "text/css" in stylesheet.headers["content-type"]
    assert "javascript" in script.headers["content-type"]
    assert "javascript" in markdown_script.headers["content-type"]
    assert '/static/app.css' in page.text
    assert '/static/app.js' in page.text
    assert '/static/markdown-render.js' in page.text

    static_root = Path(api_module.__file__).parent / "static"
    frontend_source = "\n".join(path.read_text(encoding="utf-8") for path in static_root.iterdir())
    lowered_source = frontend_source.lower()
    for forbidden in ("sessionstorage", "api_key", "smtp", "password", "secret"):
        assert forbidden not in lowered_source
    # Stored report and source text must never be injected as HTML.
    assert "innerhtml" not in lowered_source
    assert "insertadjacenthtml" not in lowered_source
    assert "currentSessionId" in script.text
    assert 'id="session-list"' in page.text
    assert 'id="sessions-archived"' in page.text


def test_frontend_layout_keeps_settings_off_the_main_grid(tmp_path):
    client = TestClient(create_app(settings=_settings(tmp_path), agent_runner=FakeRunner()))

    page = client.get("/")
    stylesheet = client.get("/static/app.css").text

    # Preferences live in a topbar-triggered drawer, not beside the conversation.
    assert 'id="settings-dialog"' in page.text
    assert 'id="open-settings"' in page.text
    assert 'id="close-settings"' in page.text
    assert 'aria-controls="settings-dialog"' in page.text
    assert 'id="preferences-form"' in page.text
    assert 'class="preferences section-panel"' not in page.text
    assert ".primary-grid" not in stylesheet
    assert "min(1280px" not in stylesheet
    assert "min(1680px" in stylesheet

    # Markdown containers render into elements, not <pre> blocks.
    assert 'id="detail-markdown" class="markdown-body"' in page.text
    assert 'id="source-markdown" class="markdown-body"' in page.text
    assert "height: 286px" not in stylesheet
    assert ".markdown-body h1" in stylesheet
    assert ".markdown-body table" in stylesheet

    # Report deletion lives in the page markup.
    assert 'id="delete-report"' in page.text


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


def test_delete_report_removes_indexed_files_and_sources(tmp_path):
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
    response = client.delete("/api/reports/1")

    assert response.status_code == 204
    assert client.get("/api/reports/1").status_code == 404
    assert client.get("/api/sources/1").status_code == 404
    assert not (settings.report_root / stored_report.relative_path).exists()
    assert not (settings.source_archive_root / stored_source.relative_path).exists()
    assert client.delete("/api/reports/1").status_code == 404


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
    assert digest.calls[0][1] == datetime.now(ZoneInfo("Asia/Shanghai")).date()


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


def test_session_api_lifecycle_and_message_reading(tmp_path):
    repository = InMemoryRepository()
    runner = FakeRunner(AgentRunResult("回答", 1, "completed"))
    client = TestClient(create_app(repository, agent_runner=runner, settings=_settings(tmp_path)))

    created = client.post("/api/sessions")
    assert created.status_code == 200
    session = created.json()
    assert session["status"] == "active"
    assert session["title"] is None
    assert session["archived_at"] is None

    chat = client.post(
        "/api/chat", json={"session_id": session["id"], "message": "第一条消息"}
    )
    assert chat.status_code == 200
    detail = client.get(f"/api/sessions/{session['id']}")
    assert detail.status_code == 200
    assert [message["role"] for message in detail.json()["messages"]] == [
        "user",
        "assistant",
    ]
    assert "metadata" not in detail.json()["messages"][0]

    renamed = client.patch(
        f"/api/sessions/{session['id']}", json={"title": "手动标题"}
    )
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "手动标题"

    archived = client.post(f"/api/sessions/{session['id']}/archive")
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"
    assert client.get("/api/sessions").json()["items"] == []
    assert client.get("/api/sessions?status=archived").json()["items"][0]["id"] == session["id"]

    blocked = client.post(
        "/api/chat", json={"session_id": session["id"], "message": "不应写入"}
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "session_archived"
    assert len(repository.list_messages(session["id"])) == 2
    assert client.post(f"/api/sessions/{session['id']}/archive").json()["error"]["code"] == "session_state_conflict"

    restored = client.post(f"/api/sessions/{session['id']}/restore")
    assert restored.status_code == 200
    assert restored.json()["status"] == "active"
    assert client.post(f"/api/sessions/{session['id']}/restore").json()["error"]["code"] == "session_state_conflict"

    deleted = client.delete(f"/api/sessions/{session['id']}")
    assert deleted.status_code == 204
    assert client.get(f"/api/sessions/{session['id']}").json()["error"]["code"] == "session_not_found"
    assert client.delete(f"/api/sessions/{session['id']}").json()["error"]["code"] == "session_not_found"


def test_session_api_pagination_validation_and_sqlite_restart(tmp_path):
    repository = InMemoryRepository()
    client = TestClient(create_app(repository, agent_runner=FakeRunner(), settings=_settings(tmp_path)))
    first = client.post("/api/sessions").json()
    second = client.post("/api/sessions").json()
    page = client.get("/api/sessions?limit=1&offset=1")
    assert page.status_code == 200
    assert page.json()["limit"] == 1
    assert len(page.json()["items"]) == 1
    assert page.json()["items"][0]["id"] in {first["id"], second["id"]}
    assert client.get("/api/sessions?status=unknown").status_code == 422
    assert client.get("/api/sessions?limit=0").status_code == 422
    assert client.get("/api/sessions/missing").json()["error"]["code"] == "session_not_found"

    settings = _settings(tmp_path / "sqlite")
    first_client = TestClient(create_app(settings=settings, agent_runner=FakeRunner()))
    created = first_client.post("/api/sessions").json()
    sent = first_client.post(
        "/api/chat", json={"session_id": created["id"], "message": "跨实例消息"}
    )
    assert sent.status_code == 200

    second_app = create_app(settings=settings, agent_runner=FakeRunner())
    second_client = TestClient(second_app)
    restarted = second_client.get(f"/api/sessions/{created['id']}")
    assert restarted.status_code == 200
    assert restarted.json()["messages"][0]["content"] == "跨实例消息"
