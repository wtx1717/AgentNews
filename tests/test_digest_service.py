from __future__ import annotations

from datetime import date
import asyncio
import json

import pytest

from agentnews.agent import AgentRunResult, ToolCallRecord
from agentnews.config import Settings
from agentnews.repositories import ReportStorage, SourceArchiveStorage
from agentnews.services.digest import (
    DigestConfigurationError,
    DigestEmptyResponseError,
    DigestIncompleteRunError,
    DigestNoSourcesError,
    DigestPreferences,
    DigestService,
    DigestStorageError,
)


class FakeRunner:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def ainvoke(self, prompt, *, system_prompt=None, task_context=None):
        self.calls.append((prompt, system_prompt, task_context))
        return self.result


def _settings(tmp_path):
    return Settings(
        _env_file=None,
        report_root=tmp_path / "reports",
        source_archive_root=tmp_path / "source_archive",
    )


def _record(name, payload, call_id):
    return ToolCallRecord(name=name, arguments={}, result=json.dumps(payload), call_id=call_id)


def _result(*records, text="日报正文", stop_reason="completed"):
    return AgentRunResult(text=text, tool_calls=len(records), stop_reason=stop_reason, tool_call_records=records)


def test_run_once_separates_preferences_from_user_request_and_archives_sources(tmp_path):
    result = _result(
        _record(
            "web_search",
            {"ok": True, "result": {"results": [
                {"title": "AI 新闻", "url": "https://example.com/news", "content": "摘要", "published_date": "2026-10-14"},
                {"title": "重复", "url": "https://example.com/news", "content": "重复摘要"},
            ]}},
            "search-1",
        ),
        _record(
            "fetch_webpage",
            {"ok": True, "result": {"url": "https://example.com/news", "title": "AI 新闻", "text": "正文内容", "fetched_at": "2026-10-14T09:00:00+08:00"}},
            "fetch-1",
        ),
    )
    runner = FakeRunner(result)
    service = DigestService(runner, settings=_settings(tmp_path), report_id_factory=lambda: 7)

    digest = asyncio.run(service.run_once(
        DigestPreferences(topics=["人工智能"], keywords=["Agent"], timezone="Asia/Shanghai"),
        date(2026, 10, 14),
    ))

    assert digest.report_id == 7
    assert digest.tool_calls == 2
    assert digest.final_text == "日报正文"
    assert digest.report_path == "2026-10-14/7-2026-10-14-新闻日报.md"
    assert len(digest.source_archive_paths) == 1
    assert "https://example.com/news" in ReportStorage(tmp_path / "reports").read_report(digest.report_path)
    archive = SourceArchiveStorage(tmp_path / "source_archive").read_source(digest.source_archive_paths[0])
    assert "# AI 新闻" in archive
    assert "原始 URL：<https://example.com/news>" in archive
    assert "正文内容" in archive
    assert "摘要" not in archive
    assert "抓取时间：2026-10-14T01:00:00Z" in archive
    prompt_call, system_prompt, task_context = runner.calls[0]
    assert prompt_call == "请根据本次任务上下文生成一次性新闻日报，并保留真实来源 URL。"
    assert "人工智能" in task_context and "Agent" in task_context
    assert "2026-10-14" in task_context and "日报系统提示词" not in task_context
    assert "一次性日报任务系统提示词" in system_prompt


def test_search_summary_can_generate_report_without_archive(tmp_path):
    runner = FakeRunner(_result(_record("web_search", {"results": [{"title": "标题", "url": "https://example.com", "content": "简要摘要"}]}, "1")))
    service = DigestService(runner, settings=_settings(tmp_path), report_id_factory=lambda: 1)

    digest = asyncio.run(service.run_once({"topics": ["AI"], "keywords": [], "timezone": "Asia/Shanghai"}, date(2026, 10, 14)))

    assert digest.source_archive_paths == ()
    assert (tmp_path / "reports" / digest.report_path).exists()


def test_empty_preferences_and_unreliable_sources_fail_before_success(tmp_path):
    service = DigestService(FakeRunner(_result()), settings=_settings(tmp_path), report_id_factory=lambda: 1)
    with pytest.raises(DigestConfigurationError):
        asyncio.run(service.run_once(DigestPreferences(), date(2026, 10, 14)))

    runner = FakeRunner(_result(_record("web_search", {"results": [{"title": "坏链接", "url": "javascript:bad"}]}, "1")))
    service = DigestService(runner, settings=_settings(tmp_path), report_id_factory=lambda: 1)
    with pytest.raises(DigestNoSourcesError):
        asyncio.run(service.run_once(DigestPreferences(topics=["AI"]), date(2026, 10, 14)))


def test_existing_different_report_is_not_overwritten(tmp_path):
    settings = _settings(tmp_path)
    storage = ReportStorage(settings.report_root)
    storage.save_report(1, date(2026, 10, 14), "2026-10-14 新闻日报", "different")
    runner = FakeRunner(_result(_record("web_search", {"url": "https://example.com", "title": "标题", "content": "简要摘要"}, "1")))
    service = DigestService(runner, settings=settings, report_id_factory=lambda: 1)

    with pytest.raises(DigestStorageError):
        asyncio.run(service.run_once(DigestPreferences(topics=["AI"]), date(2026, 10, 14)))
    assert storage.read_report("2026-10-14/1-2026-10-14-新闻日报.md") == "different"


def test_archive_conflict_rolls_back_new_report_and_preserves_archive(tmp_path):
    settings = _settings(tmp_path)
    archive_storage = SourceArchiveStorage(settings.source_archive_root)
    stored = archive_storage.save_source(
        1,
        1,
        date(2026, 10, 14),
        "标题",
        "https://example.com",
        "example.com",
        "2026-10-14T00:00:00Z",
        None,
        "pre-existing archive",
    )
    runner = FakeRunner(
        _result(
            _record(
                "web_search",
                {"url": "https://example.com", "title": "标题", "content": "简要摘要"},
                "search-1",
            ),
            _record(
                "fetch_page",
                {"url": "https://example.com", "title": "标题", "text": "new body"},
                "fetch-1",
            ),
        )
    )
    service = DigestService(runner, settings=settings, report_id_factory=lambda: 1)

    with pytest.raises(DigestStorageError):
        asyncio.run(service.run_once(DigestPreferences(topics=["AI"]), date(2026, 10, 14)))

    report_path = tmp_path / "reports" / "2026-10-14" / "1-2026-10-14-新闻日报.md"
    assert not report_path.exists()
    assert archive_storage.read_source(stored.relative_path).endswith("pre-existing archive")


def test_empty_agent_answer_does_not_write_report(tmp_path):
    runner = FakeRunner(_result(text="  "))
    service = DigestService(runner, settings=_settings(tmp_path), report_id_factory=lambda: 1)

    with pytest.raises(DigestEmptyResponseError):
        asyncio.run(service.run_once(DigestPreferences(topics=["AI"]), date(2026, 10, 14)))
    assert not (tmp_path / "reports").exists()


def test_incomplete_agent_run_does_not_write_report(tmp_path):
    runner = FakeRunner(_result(text="工具调用达到上限", stop_reason="max_tool_calls"))
    service = DigestService(runner, settings=_settings(tmp_path), report_id_factory=lambda: 1)

    with pytest.raises(DigestIncompleteRunError):
        asyncio.run(service.run_once(DigestPreferences(topics=["AI"]), date(2026, 10, 14)))
    assert not (tmp_path / "reports").exists()


def test_html_fetch_result_is_never_archived(tmp_path):
    runner = FakeRunner(
        _result(
            _record(
                "web_search",
                {"results": [{"title": "标题", "url": "https://example.com", "content": "简要摘要"}]},
                "search-1",
            ),
            _record(
                "fetch_page",
                {"url": "https://example.com", "title": "标题", "text": "<html><body>完整页面</body></html>"},
                "fetch-1",
            ),
        )
    )
    service = DigestService(runner, settings=_settings(tmp_path), report_id_factory=lambda: 1)

    digest = asyncio.run(service.run_once(DigestPreferences(topics=["AI"]), date(2026, 10, 14)))

    assert digest.source_archive_paths == ()
    assert not (tmp_path / "source_archive").exists()


def test_binary_fetch_result_is_never_archived(tmp_path):
    runner = FakeRunner(
        _result(
            _record(
                "web_search",
                {"results": [{"title": "标题", "url": "https://example.com/file", "content": "简要摘要"}]},
                "search-1",
            ),
            _record(
                "fetch_page",
                {"url": "https://example.com/file", "content_type": "application/pdf", "text": "二进制内容表示"},
                "fetch-1",
            ),
        )
    )
    service = DigestService(runner, settings=_settings(tmp_path), report_id_factory=lambda: 1)

    digest = asyncio.run(service.run_once(DigestPreferences(topics=["AI"]), date(2026, 10, 14)))

    assert digest.source_archive_paths == ()
