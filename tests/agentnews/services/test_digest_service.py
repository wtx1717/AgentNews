from __future__ import annotations

from datetime import date
import asyncio
import json

import pytest

from agentnews.agent import AgentRunResult, ToolCallRecord
from agentnews.config import Settings
from agentnews.repositories import (
    InMemoryRepository,
    ReportStorage,
    SQLiteRepository,
    SourceArchiveStorage,
)
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


class RecordingRepository(InMemoryRepository):
    def __init__(self, *, events, fail_on_index=None, fail_on_finalize=False):
        super().__init__(report_id_factory=lambda: 1)
        self.events = events
        self.fail_on_index = fail_on_index
        self.fail_on_finalize = fail_on_finalize
        self._index_count = 0

    def reserve_report(self, report_date, title):
        self.events.append("reserve")
        return super().reserve_report(report_date, title)

    def add_report_source(self, report_id, title, url, source_name, **kwargs):
        self._index_count += 1
        self.events.append(f"index:{self._index_count}")
        if self.fail_on_index == self._index_count:
            raise RuntimeError("source index failed")
        return super().add_report_source(
            report_id, title, url, source_name, **kwargs
        )

    def finalize_report(self, report_id, file_path, summary, content_hash):
        self.events.append("finalize")
        if self.fail_on_finalize:
            raise RuntimeError("report finalize failed")
        return super().finalize_report(report_id, file_path, summary, content_hash)


class RecordingReportStorage(ReportStorage):
    def __init__(self, root, events):
        super().__init__(root)
        self.events = events

    def save_report(self, report_id, report_date, title, content):
        self.events.append("report_file")
        return super().save_report(report_id, report_date, title, content)


class RecordingSourceArchiveStorage(SourceArchiveStorage):
    def __init__(self, root, events, fail_on_index=None):
        super().__init__(root)
        self.events = events
        self.fail_on_index = fail_on_index

    def save_source(
        self,
        report_id,
        source_index,
        source_date,
        title,
        url,
        source_site,
        fetched_at,
        published_at,
        body,
    ):
        self.events.append(f"source_file:{source_index}")
        if self.fail_on_index == source_index:
            raise OSError("source file failed")
        return super().save_source(
            report_id,
            source_index,
            source_date,
            title,
            url,
            source_site,
            fetched_at,
            published_at,
            body,
        )


class FailingSQLiteRepository(SQLiteRepository):
    def __init__(self, database_path, *, fail_on_index=None, fail_on_finalize=False):
        self.fail_on_index = fail_on_index
        self.fail_on_finalize = fail_on_finalize
        self._index_count = 0
        super().__init__(database_path)

    def add_report_source(self, report_id, title, url, source_name, **kwargs):
        self._index_count += 1
        if self.fail_on_index == self._index_count:
            raise RuntimeError("source index failed")
        return super().add_report_source(
            report_id, title, url, source_name, **kwargs
        )

    def finalize_report(self, report_id, file_path, summary, content_hash):
        if self.fail_on_finalize:
            raise RuntimeError("report finalize failed")
        return super().finalize_report(report_id, file_path, summary, content_hash)


def _repository():
    return InMemoryRepository(report_id_factory=lambda: 1)


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


def _two_source_result():
    return _result(
        _record(
            "web_search",
            {
                "results": [
                    {
                        "title": "标题一",
                        "url": "https://example.com/one",
                        "content": "摘要一",
                    },
                    {
                        "title": "标题二",
                        "url": "https://example.com/two",
                        "content": "摘要二",
                    },
                ]
            },
            "search-1",
        ),
        _record(
            "fetch_webpage",
            {
                "url": "https://example.com/one",
                "title": "标题一",
                "text": "正文一",
            },
            "fetch-1",
        ),
        _record(
            "fetch_webpage",
            {
                "url": "https://example.com/two",
                "title": "标题二",
                "text": "正文二",
            },
            "fetch-2",
        ),
    )


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
    repository = _repository()
    service = DigestService(runner, settings=_settings(tmp_path), repository=repository)

    digest = asyncio.run(service.run_once(
        DigestPreferences(topics=["人工智能"], keywords=["Agent"], timezone="Asia/Shanghai"),
        date(2026, 10, 14),
    ))

    assert digest.report_id == 1
    assert digest.tool_calls == 2
    assert digest.final_text == "日报正文"
    assert digest.title == "人工智能 日报"
    assert digest.report_path == "2026-10-14/1-人工智能-日报.md"
    stored_report = ReportStorage(tmp_path / "reports").read_report(digest.report_path)
    assert stored_report.startswith("# 人工智能 日报\n\n")
    assert len(digest.source_archive_paths) == 1
    assert "https://example.com/news" in stored_report
    archive = SourceArchiveStorage(tmp_path / "source_archive").read_source(digest.source_archive_paths[0])
    assert "# AI 新闻" in archive
    assert "原始 URL：<https://example.com/news>" in archive
    assert "正文内容" in archive
    assert "摘要" not in archive
    assert "抓取时间：2026-10-14T01:00:00Z" in archive
    report = repository.get_report(digest.report_id)
    assert report.title == digest.title
    assert report.file_path == digest.report_path
    assert report.content_hash
    assert report.summary == "日报正文"
    indexed_source = repository.get_report_sources(digest.report_id)[0]
    assert indexed_source.url == "https://example.com/news"
    assert indexed_source.archive_file_path == digest.source_archive_paths[0]
    prompt_call, system_prompt, task_context = runner.calls[0]
    assert prompt_call == (
        "请根据本次任务上下文检索并撰写一份新闻日报。只返回日报 Markdown 正文，"
        "从一级标题开始，不要附加任何过程说明。"
    )
    assert "人工智能" in task_context and "Agent" in task_context
    assert "2026-10-14" in task_context and "日报系统提示词" not in task_context
    assert "一次性日报任务系统提示词" in system_prompt
    assert "日报 Markdown 输出规则" in system_prompt
    assert "来源正文归档规则" in system_prompt
    assert "搜索与网页正文抓取规则" in system_prompt


def test_agent_heading_becomes_the_report_title(tmp_path):
    agent_text = (
        "I have enough verified evidence. Here is the daily report.\n\n"
        "---\n\n"
        "# Ave Mujica 新闻日报（2026-10-16 · Asia/Shanghai）\n\n"
        "## 概览\n\n"
        "剧场版今日在日本全国上映。\n"
    )
    runner = FakeRunner(
        _result(
            _record(
                "web_search",
                {
                    "results": [
                        {
                            "title": "上映",
                            "url": "https://example.com/film",
                            "content": "摘要",
                        }
                    ]
                },
                "search-1",
            ),
            text=agent_text,
        )
    )
    repository = _repository()
    service = DigestService(runner, settings=_settings(tmp_path), repository=repository)

    digest = asyncio.run(
        service.run_once(DigestPreferences(topics=["Ave Mujica"]), date(2026, 10, 16))
    )

    assert digest.title == "Ave Mujica 新闻日报（2026-10-16 · Asia/Shanghai）"
    report = repository.get_report(digest.report_id)
    assert report is not None
    assert report.title == digest.title
    assert "ave-mujica" in digest.report_path
    stored = ReportStorage(tmp_path / "reports").read_report(digest.report_path)
    assert stored.count("# Ave Mujica 新闻日报（2026-10-16 · Asia/Shanghai）") == 1


def test_report_title_is_cleaned_from_markdown(tmp_path):
    runner = FakeRunner(
        _result(
            _record(
                "web_search",
                {"results": [{"title": "标题", "url": "https://example.com", "content": "摘要"}]},
                "search-1",
            ),
            text="# **AI 周报**：[比亚迪](https://example.com/b) 与特斯拉\n\n正文",
        )
    )
    service = DigestService(
        runner, settings=_settings(tmp_path), repository=_repository()
    )

    digest = asyncio.run(
        service.run_once(DigestPreferences(topics=["比亚迪"]), date(2026, 10, 16))
    )

    assert digest.title == "AI 周报：比亚迪 与特斯拉"
    assert digest.report_path == "2026-10-16/1-ai-周报-比亚迪-与特斯拉.md"


def test_run_once_writes_all_files_before_indexing_sources(tmp_path):
    events = []
    settings = _settings(tmp_path)
    repository = RecordingRepository(events=events)
    service = DigestService(
        FakeRunner(_two_source_result()),
        repository=repository,
        settings=settings,
        report_storage=RecordingReportStorage(settings.report_root, events),
        source_archive_storage=RecordingSourceArchiveStorage(
            settings.source_archive_root, events
        ),
    )

    asyncio.run(service.run_once(DigestPreferences(topics=["AI"]), date(2026, 10, 14)))

    assert events == [
        "reserve",
        "report_file",
        "source_file:1",
        "source_file:2",
        "index:1",
        "index:2",
        "finalize",
    ]


def test_second_source_file_failure_cleans_all_created_files_and_indexes(tmp_path):
    settings = _settings(tmp_path)
    repository = RecordingRepository(events=[])
    service = DigestService(
        FakeRunner(_two_source_result()),
        repository=repository,
        settings=settings,
        source_archive_storage=RecordingSourceArchiveStorage(
            settings.source_archive_root, repository.events, fail_on_index=2
        ),
    )

    with pytest.raises(DigestStorageError):
        asyncio.run(service.run_once(DigestPreferences(topics=["AI"]), date(2026, 10, 14)))

    assert not list(settings.report_root.rglob("*.md"))
    assert not list(settings.source_archive_root.rglob("*.md"))
    assert repository.get_report(1) is None
    assert repository.get_report_sources(1) == ()


def test_source_index_failure_cleans_files_and_cancels_reservation(tmp_path):
    events = []
    settings = _settings(tmp_path)
    repository = RecordingRepository(events=events, fail_on_index=2)
    service = DigestService(
        FakeRunner(_two_source_result()),
        repository=repository,
        settings=settings,
        report_storage=RecordingReportStorage(settings.report_root, events),
        source_archive_storage=RecordingSourceArchiveStorage(
            settings.source_archive_root, events
        ),
    )

    with pytest.raises(DigestStorageError):
        asyncio.run(service.run_once(DigestPreferences(topics=["AI"]), date(2026, 10, 14)))

    assert events == [
        "reserve",
        "report_file",
        "source_file:1",
        "source_file:2",
        "index:1",
        "index:2",
    ]
    assert not list(settings.report_root.rglob("*.md"))
    assert not list(settings.source_archive_root.rglob("*.md"))
    assert repository.get_report(1) is None
    assert repository.get_report_sources(1) == ()


def test_finalize_failure_cleans_files_and_cancels_reservation(tmp_path):
    events = []
    settings = _settings(tmp_path)
    repository = RecordingRepository(events=events, fail_on_finalize=True)
    service = DigestService(
        FakeRunner(_two_source_result()),
        repository=repository,
        settings=settings,
        report_storage=RecordingReportStorage(settings.report_root, events),
        source_archive_storage=RecordingSourceArchiveStorage(
            settings.source_archive_root, events
        ),
    )

    with pytest.raises(DigestStorageError):
        asyncio.run(service.run_once(DigestPreferences(topics=["AI"]), date(2026, 10, 14)))

    assert events[-1] == "finalize"
    assert not list(settings.report_root.rglob("*.md"))
    assert not list(settings.source_archive_root.rglob("*.md"))
    assert repository.get_report(1) is None
    assert repository.get_report_sources(1) == ()


def test_sqlite_digest_persists_report_and_sources_after_restart(tmp_path):
    settings = _settings(tmp_path)
    database_path = tmp_path / "agentnews.sqlite3"
    repository = SQLiteRepository(database_path)
    try:
        digest = asyncio.run(
            DigestService(
                FakeRunner(_two_source_result()),
                repository=repository,
                settings=settings,
            ).run_once(DigestPreferences(topics=["AI"]), date(2026, 10, 14))
        )
    finally:
        repository.close()

    reopened = SQLiteRepository(database_path)
    try:
        report = reopened.get_report(digest.report_id)
        sources = reopened.get_report_sources(digest.report_id)
        assert report is not None
        assert report.file_path == digest.report_path
        assert len(sources) == 2
        assert [source.archive_file_path for source in sources] == list(
            digest.source_archive_paths
        )
    finally:
        reopened.close()

    assert ReportStorage(settings.report_root).read_report(digest.report_path)
    archive_storage = SourceArchiveStorage(settings.source_archive_root)
    assert all(archive_storage.read_source(path) for path in digest.source_archive_paths)


@pytest.mark.parametrize(
    "failure_kwargs",
    ({"fail_on_index": 2}, {"fail_on_finalize": True}),
    ids=("source-index", "finalize"),
)
def test_sqlite_digest_failure_cleans_files_and_hides_cancelled_report(
    tmp_path, failure_kwargs
):
    settings = _settings(tmp_path)
    database_path = tmp_path / "agentnews.sqlite3"
    repository = FailingSQLiteRepository(database_path, **failure_kwargs)
    try:
        with pytest.raises(DigestStorageError):
            asyncio.run(
                DigestService(
                    FakeRunner(_two_source_result()),
                    repository=repository,
                    settings=settings,
                ).run_once(DigestPreferences(topics=["AI"]), date(2026, 10, 14))
            )
        assert repository.get_report(1) is None
        assert repository.get_report_sources(1) == ()
    finally:
        repository.close()

    reopened = SQLiteRepository(database_path)
    try:
        assert reopened.get_report(1) is None
        assert reopened.get_report_sources(1) == ()
    finally:
        reopened.close()
    assert not list(settings.report_root.rglob("*.md"))
    assert not list(settings.source_archive_root.rglob("*.md"))


def test_search_summary_can_generate_report_without_archive(tmp_path):
    runner = FakeRunner(_result(_record("web_search", {"results": [{"title": "标题", "url": "https://example.com", "content": "简要摘要"}]}, "1")))
    repository = _repository()
    service = DigestService(runner, repository=repository, settings=_settings(tmp_path))

    digest = asyncio.run(service.run_once({"topics": ["AI"], "keywords": [], "timezone": "Asia/Shanghai"}, date(2026, 10, 14)))

    assert digest.source_archive_paths == ()
    assert (tmp_path / "reports" / digest.report_path).exists()
    assert repository.get_report_sources(digest.report_id)[0].archive_file_path is None


def test_empty_preferences_and_unreliable_sources_fail_before_success(tmp_path):
    service = DigestService(
        FakeRunner(_result()), repository=_repository(), settings=_settings(tmp_path)
    )
    with pytest.raises(DigestConfigurationError):
        asyncio.run(service.run_once(DigestPreferences(), date(2026, 10, 14)))

    runner = FakeRunner(_result(_record("web_search", {"results": [{"title": "坏链接", "url": "javascript:bad"}]}, "1")))
    service = DigestService(runner, repository=_repository(), settings=_settings(tmp_path))
    with pytest.raises(DigestNoSourcesError):
        asyncio.run(service.run_once(DigestPreferences(topics=["AI"]), date(2026, 10, 14)))


def test_existing_different_report_is_not_overwritten(tmp_path):
    settings = _settings(tmp_path)
    storage = ReportStorage(settings.report_root)
    storage.save_report(1, date(2026, 10, 14), "AI 日报", "different")
    runner = FakeRunner(_result(_record("web_search", {"url": "https://example.com", "title": "标题", "content": "简要摘要"}, "1")))
    repository = _repository()
    service = DigestService(runner, settings=settings, repository=repository)

    with pytest.raises(DigestStorageError):
        asyncio.run(service.run_once(DigestPreferences(topics=["AI"]), date(2026, 10, 14)))
    assert storage.read_report("2026-10-14/1-ai-日报.md") == "different"
    assert repository.get_report(1) is None


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
    repository = _repository()
    service = DigestService(runner, settings=settings, repository=repository)

    with pytest.raises(DigestStorageError):
        asyncio.run(service.run_once(DigestPreferences(topics=["AI"]), date(2026, 10, 14)))

    report_path = tmp_path / "reports" / "2026-10-14" / "1-ai-日报.md"
    assert not report_path.exists()
    assert archive_storage.read_source(stored.relative_path).endswith("pre-existing archive")
    assert repository.get_report(1) is None


def test_empty_agent_answer_does_not_write_report(tmp_path):
    runner = FakeRunner(_result(text="  "))
    service = DigestService(runner, repository=_repository(), settings=_settings(tmp_path))

    with pytest.raises(DigestEmptyResponseError):
        asyncio.run(service.run_once(DigestPreferences(topics=["AI"]), date(2026, 10, 14)))
    assert not (tmp_path / "reports").exists()


def test_incomplete_agent_run_does_not_write_report(tmp_path):
    runner = FakeRunner(_result(text="工具调用达到上限", stop_reason="max_tool_calls"))
    service = DigestService(runner, repository=_repository(), settings=_settings(tmp_path))

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
    service = DigestService(runner, repository=_repository(), settings=_settings(tmp_path))

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
    service = DigestService(runner, repository=_repository(), settings=_settings(tmp_path))

    digest = asyncio.run(service.run_once(DigestPreferences(topics=["AI"]), date(2026, 10, 14)))

    assert digest.source_archive_paths == ()


def test_agent_report_strips_preamble_and_curates_cited_sources(tmp_path):
    agent_text = (
        "I now have verified evidence. Here is the daily report.\n\n"
        "---\n\n"
        "# 比亚迪固态电池时间表落定\n\n"
        "## 今日要点\n\n"
        "- 2027 年小批量装车\n"
        "- 2030 年规模化量产\n\n"
        "## 事件进展\n\n"
        "比亚迪公布时间表，详见[报道](https://example.com/a)。\n\n"
        "## 来源\n\n"
        "- [引用来源](https://example.com/a)\n"
        "- [无关来源](https://example.com/b)\n"
    )
    runner = FakeRunner(
        _result(
            _record(
                "web_search",
                {
                    "results": [
                        {
                            "title": "引用来源",
                            "url": "https://example.com/a",
                            "content": "摘要一",
                            "source_site": "示例站",
                            "published_date": "2026-10-16",
                        },
                        {
                            "title": "无关来源",
                            "url": "https://example.com/b",
                            "content": "摘要二",
                        },
                    ]
                },
                "search-1",
            ),
            _record(
                "fetch_webpage",
                {"url": "https://example.com/a", "title": "引用来源", "text": "正文一"},
                "fetch-1",
            ),
            text=agent_text,
        )
    )
    repository = _repository()
    service = DigestService(runner, repository=repository, settings=_settings(tmp_path))

    digest = asyncio.run(
        service.run_once(DigestPreferences(topics=["比亚迪"]), date(2026, 10, 17))
    )

    assert digest.title == "比亚迪固态电池时间表落定"
    assert digest.summary == "2027 年小批量装车；2030 年规模化量产"
    stored = ReportStorage(tmp_path / "reports").read_report(digest.report_path)
    assert stored.startswith("# 比亚迪固态电池时间表落定\n\n")
    assert "I now have verified evidence" not in stored
    assert "\n## 来源\n" not in stored
    assert "无关来源" not in stored
    assert "## 参考来源" in stored
    assert "- [引用来源](https://example.com/a)（示例站，2026-10-16）" in stored
    assert "](<" not in stored
    assert [source.url for source in repository.get_report_sources(digest.report_id)] == [
        "https://example.com/a"
    ]
    assert len(digest.source_archive_paths) == 1
    assert repository.get_report(digest.report_id).summary == digest.summary


def test_uncited_report_keeps_only_fetched_sources(tmp_path):
    runner = FakeRunner(
        _result(
            _record(
                "web_search",
                {
                    "results": [
                        {"title": "已读", "url": "https://example.com/a", "content": "摘要一"},
                        {"title": "仅搜索", "url": "https://example.com/b", "content": "摘要二"},
                    ]
                },
                "search-1",
            ),
            _record(
                "fetch_webpage",
                {"url": "https://example.com/a", "title": "已读", "text": "正文一"},
                "fetch-1",
            ),
            text="# 芯片产业动态\n\n正文没有引用任何链接。",
        )
    )
    repository = _repository()
    service = DigestService(runner, repository=repository, settings=_settings(tmp_path))

    digest = asyncio.run(
        service.run_once(DigestPreferences(topics=["芯片"]), date(2026, 10, 17))
    )

    assert [source.url for source in repository.get_report_sources(digest.report_id)] == [
        "https://example.com/a"
    ]
    stored = ReportStorage(tmp_path / "reports").read_report(digest.report_path)
    assert "https://example.com/a" in stored
    assert "https://example.com/b" not in stored


def test_task_context_and_reference_lines_carry_window_and_metadata(tmp_path):
    runner = FakeRunner(
        _result(
            _record(
                "web_search",
                {
                    "results": [
                        {
                            "title": "标题",
                            "url": "https://example.com/x",
                            "content": "摘要",
                            "source_site": "示例站",
                            "published_date": "2026-10-16",
                        }
                    ]
                },
                "search-1",
            ),
            text="# 标题\n\n正文 [来源](https://example.com/x)。",
        )
    )
    service = DigestService(runner, repository=_repository(), settings=_settings(tmp_path))

    digest = asyncio.run(
        service.run_once(DigestPreferences(topics=["AI"]), date(2026, 10, 17))
    )

    _, _, task_context = runner.calls[0]
    assert "业务日期：2026-10-17（星期六）" in task_context
    assert "检索窗口：2026-10-11 至 2026-10-17" in task_context
    assert "优先 2026-10-15 至 2026-10-17" in task_context
    assert "报告语言：简体中文" in task_context
    stored = ReportStorage(tmp_path / "reports").read_report(digest.report_path)
    assert "- [标题](https://example.com/x)（示例站，2026-10-16）" in stored


def test_trailing_process_notes_are_removed_from_the_report(tmp_path):
    runner = FakeRunner(
        _result(
            _record(
                "web_search",
                {"results": [{"title": "标题", "url": "https://example.com/x", "content": "摘要"}]},
                "search-1",
            ),
            text=(
                "# 标题\n\n正文。\n\n"
                "**证据说明**：本次实际抓取 2 篇正文。\n"
            ),
        )
    )
    service = DigestService(runner, repository=_repository(), settings=_settings(tmp_path))

    digest = asyncio.run(
        service.run_once(DigestPreferences(topics=["AI"]), date(2026, 10, 17))
    )

    assert digest.final_text == "# 标题\n\n正文。"
    stored = ReportStorage(tmp_path / "reports").read_report(digest.report_path)
    assert "证据说明" not in stored
    assert "实际抓取" not in stored


def test_h1_inside_a_code_fence_is_not_the_report_title(tmp_path):
    runner = FakeRunner(
        _result(
            _record(
                "web_search",
                {"results": [{"title": "标题", "url": "https://example.com/x", "content": "摘要"}]},
                "search-1",
            ),
            text=(
                "```text\n# 不是标题\n```\n\n"
                "# 真正的标题\n\n正文 [来源](https://example.com/x)。"
            ),
        )
    )
    service = DigestService(runner, repository=_repository(), settings=_settings(tmp_path))

    digest = asyncio.run(
        service.run_once(DigestPreferences(topics=["AI"]), date(2026, 10, 17))
    )

    assert digest.title == "真正的标题"
