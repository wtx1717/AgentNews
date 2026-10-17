from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agentnews.agent import AgentRunner, AgentRunResult
from agentnews.config import Settings
from agentnews.repositories import InMemoryRepository, ReportStorage, SourceArchiveStorage, UserPreferences
from agentnews.services.digest import DigestNoSourcesError, DigestPreferences, DigestService
from agentnews.tools import MCPToolDefinition
from agentnews.tools.mcp_client import MCPUnavailableError


class MockModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.messages = []
        self.tools = None

    def bind_tools(self, tools):
        self.tools = list(tools)
        return self

    async def ainvoke(self, messages):
        self.messages.append(list(messages))
        return self.responses.pop(0)


class MockMCP:
    def __init__(self, _settings, *, fail_tools=()):
        self.fail_tools = set(fail_tools)
        self.calls = []
        self.closed = False
        self.definitions = (
            MCPToolDefinition(
                "tavily_search",
                "Search news sources.",
                {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            ),
            MCPToolDefinition(
                "tavily_extract",
                "Fetch text from a URL.",
                {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            ),
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    @property
    def tools(self):
        return self.definitions

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name in self.fail_tools:
            raise MCPUnavailableError("mock upstream failure")

        if name == "tavily_search":
            payload = {
                "results": [
                    {
                        "title": "AgentNews 发布新版本",
                        "url": "https://news.example.test/agentnews-v2",
                        "snippet": "项目发布了面向 Agent 的新版本。",
                        "published_at": "2026-10-15T08:30:00+08:00",
                        "source_site": "news.example.test",
                    }
                ]
            }
        else:
            payload = {
                "title": "AgentNews 发布新版本",
                "url": arguments["url"],
                "content_type": "text/plain",
                "text": "正文：AgentNews 发布了新版本，并介绍了新增的 Agent 功能。",
                "fetched_at": "2026-10-15T01:00:00Z",
            }

        # Match the MCP client's JSON envelope and text content serialization.
        return json.dumps(
            {
                "ok": True,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]
                },
            },
            ensure_ascii=False,
        )


class RecordingAgentRunner(AgentRunner):
    last_result: AgentRunResult | None = None

    async def ainvoke(self, prompt, *, system_prompt=None, task_context=None):
        result = await super().ainvoke(
            prompt,
            system_prompt=system_prompt,
            task_context=task_context,
        )
        self.last_result = result
        return result


def _settings(tmp_path):
    return Settings(
        _env_file=None,
        mcp_search_server_url="https://mcp.example.test/mcp",
        mcp_search_api_key="mock-key",
        report_root=tmp_path / "reports",
        source_archive_root=tmp_path / "source_archive",
    )


def _tool_call(name, args, call_id):
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _digest_model_responses(*, fetch=True):
    responses = [
        AIMessage(
            content="",
            tool_calls=[_tool_call("tavily_search", {"query": "人工智能 Agent"}, "search-1")],
        )
    ]
    if fetch:
        responses.append(
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call(
                        "tavily_extract",
                        {"url": "https://news.example.test/agentnews-v2"},
                        "extract-1",
                    )
                ],
            )
        )
    responses.append(AIMessage(content="AgentNews 发布了新版本，新增 Agent 功能。"))
    return responses


def test_phase_one_digest_runs_search_fetch_and_persists_markdown(tmp_path):
    settings = _settings(tmp_path)
    repository = InMemoryRepository(report_id_factory=lambda: 420)
    model = MockModel(_digest_model_responses())
    mcp = MockMCP(settings)
    runner = RecordingAgentRunner(
        model,
        settings=settings,
        mcp_client_factory=lambda _settings: mcp,
    )
    service = DigestService(runner, repository=repository, settings=settings)

    result = asyncio.run(
        service.run_once(
            DigestPreferences(
                topics=["人工智能"],
                keywords=["Agent", "AgentNews"],
                timezone="Asia/Shanghai",
            ),
            date(2026, 10, 15),
        )
    )

    assert result.report_id == 420
    assert result.tool_calls == 2
    assert result.final_text == "AgentNews 发布了新版本，新增 Agent 功能。"
    assert mcp.calls == [
        ("tavily_search", {"query": "人工智能 Agent"}),
        ("tavily_extract", {"url": "https://news.example.test/agentnews-v2"}),
    ]
    assert mcp.closed is True

    initial_messages = model.messages[0]
    assert [type(message) for message in initial_messages] == [
        SystemMessage,
        SystemMessage,
        HumanMessage,
    ]
    context = initial_messages[1].content
    assert "业务日期：2026-10-15" in context
    assert "时区：Asia/Shanghai" in context
    assert "关注主题：人工智能" in context
    assert "关键词：Agent、AgentNews" in context
    assert initial_messages[2].content == (
        "请根据本次任务上下文检索并撰写一份新闻日报。只返回日报 Markdown 正文，"
        "从一级标题开始，不要附加任何过程说明。"
    )

    run = runner.last_result
    assert run is not None
    assert run.stop_reason == "completed"
    assert [(call.name, call.call_id) for call in run.tool_call_records] == [
        ("tavily_search", "search-1"),
        ("tavily_extract", "extract-1"),
    ]
    assert run.tool_call_records[0].arguments == {"query": "人工智能 Agent"}
    assert json.loads(run.tool_call_records[0].result)["ok"] is True
    assert run.tool_call_records[1].arguments == {
        "url": "https://news.example.test/agentnews-v2"
    }
    assert "正文：AgentNews 发布了新版本" in run.tool_call_records[1].result
    replayed_tool_messages = [
        message for message in model.messages[-1] if isinstance(message, ToolMessage)
    ]
    assert [message.name for message in replayed_tool_messages] == [
        "tavily_search",
        "tavily_extract",
    ]

    report = ReportStorage(settings.report_root).read_report(result.report_path)
    assert result.report_path.endswith(".md")
    assert "AgentNews 发布了新版本" in report
    assert "https://news.example.test/agentnews-v2" in report
    assert len(result.source_archive_paths) == 1
    archive = SourceArchiveStorage(settings.source_archive_root).read_source(
        result.source_archive_paths[0]
    )
    assert result.source_archive_paths[0].endswith(".md")
    assert "# AgentNews 发布新版本" in archive
    assert "原始 URL：<https://news.example.test/agentnews-v2>" in archive
    assert "正文：AgentNews 发布了新版本" in archive
    assert repository.get_report(result.report_id) is not None
    assert repository.get_report_sources(result.report_id)[0].url == (
        "https://news.example.test/agentnews-v2"
    )


def test_ordinary_chat_does_not_load_saved_preferences(tmp_path):
    repository = InMemoryRepository()
    repository.save_preferences(
        UserPreferences(
            display_name="Lin",
            email="lin@example.test",
            timezone="Asia/Tokyo",
            topics=("不会自动带入",),
            keywords=("私有关键词",),
        )
    )
    model = MockModel([AIMessage(content="普通回答")])
    runner = AgentRunner(model, settings=Settings(_env_file=None))

    result = asyncio.run(runner.ainvoke("帮我解释一下 Python 的装饰器"))

    assert result.text == "普通回答"
    assert [type(message) for message in model.messages[0]] == [SystemMessage, HumanMessage]
    rendered_messages = "\n".join(str(message.content) for message in model.messages[0])
    assert "不会自动带入" not in rendered_messages
    assert "私有关键词" not in rendered_messages
    assert "Asia/Tokyo" not in rendered_messages


@pytest.mark.parametrize("failure_mode", ["no_results", "tool_error"])
def test_search_without_reliable_sources_does_not_create_success_record(tmp_path, failure_mode):
    settings = _settings(tmp_path)
    repository = InMemoryRepository(report_id_factory=lambda: 421)
    model = MockModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("tavily_search", {"query": "人工智能"}, "search-fail")
                ],
            ),
            AIMessage(content="当前无法确认可靠来源。"),
        ]
    )
    mcp = MockMCP(settings, fail_tools={"tavily_search"} if failure_mode == "tool_error" else ())
    if failure_mode == "no_results":
        async def empty_search(name, arguments):
            mcp.calls.append((name, arguments))
            return json.dumps({"ok": True, "result": {"results": []}})

        mcp.call_tool = empty_search

    runner = AgentRunner(model, settings=settings, mcp_client_factory=lambda _settings: mcp)
    service = DigestService(runner, repository=repository, settings=settings)

    with pytest.raises(DigestNoSourcesError, match="没有可靠来源"):
        asyncio.run(
            service.run_once(
                DigestPreferences(topics=["人工智能"]),
                date(2026, 10, 15),
            )
        )

    assert repository.list_reports() == ()
    assert repository.get_report(421) is None
    assert not settings.report_root.exists()
    assert not settings.source_archive_root.exists()
    if failure_mode == "tool_error":
        tool_result = model.messages[-1][-1]
        assert isinstance(tool_result, ToolMessage)
        assert json.loads(tool_result.content)["error"]["code"] == "mcp_error"


def test_fetch_failure_keeps_reliable_source_but_skips_archive(tmp_path):
    settings = _settings(tmp_path)
    repository = InMemoryRepository(report_id_factory=lambda: 422)
    model = MockModel(_digest_model_responses())
    mcp = MockMCP(settings, fail_tools={"tavily_extract"})
    runner = AgentRunner(model, settings=settings, mcp_client_factory=lambda _settings: mcp)
    service = DigestService(runner, repository=repository, settings=settings)

    result = asyncio.run(
        service.run_once(
            DigestPreferences(topics=["人工智能"]),
            date(2026, 10, 15),
        )
    )

    assert result.report_id == 422
    assert result.source_archive_paths == ()
    assert "https://news.example.test/agentnews-v2" in ReportStorage(
        settings.report_root
    ).read_report(result.report_path)
    assert repository.get_report(422) is not None
    assert repository.get_report_sources(422)[0].archive_file_path is None
    assert not settings.source_archive_root.exists()
