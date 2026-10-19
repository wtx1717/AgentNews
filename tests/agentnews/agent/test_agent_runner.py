from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from agentnews.agent import AgentRunner, get_prompt
from agentnews.config import Settings
from agentnews.tools.path_security import ManagedPathResolver
from agentnews.tools import OnlineSearchResult


def _settings(**overrides):
    values = {"agent_max_tool_calls": 8}
    values.update(overrides)
    return Settings(_env_file=None, **values)


class FakeModel:
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


class StreamingFakeModel:
    """每次 astream 依次产出一组预置分片，用于验证流式聚合。"""

    def __init__(self, turns):
        self.turns = [list(turn) for turn in turns]
        self.messages = []
        self.tools = None

    def bind_tools(self, tools):
        self.tools = list(tools)
        return self

    async def astream(self, messages):
        self.messages.append(list(messages))
        for chunk in self.turns.pop(0):
            yield chunk


def _tool_call(name, args, call_id):
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def test_direct_answer_does_not_initialize_search(monkeypatch):
    model = FakeModel([AIMessage(content="直接回答")])

    def unexpected_search(_settings):
        pytest.fail("search client must be lazy")

    runner = AgentRunner(model, settings=_settings(), search_client_factory=unexpected_search)
    result = asyncio.run(runner.ainvoke("hello"))

    assert result.text == "直接回答"
    assert result.tool_calls == 0
    assert result.stop_reason == "completed"
    assert isinstance(model.messages[0][0], SystemMessage)
    assert model.messages[0][0].content == get_prompt("interaction")
    assert isinstance(model.messages[0][1], HumanMessage)
    assert model.messages[0][1].content == "hello"
    assert result.tool_call_records == ()
    assert {tool.name for tool in model.tools} == {
        "web_search", "list_dir", "read_file", "search_content", "write_file"
    }


def test_streams_final_answer_tokens():
    model = StreamingFakeModel(
        [[AIMessageChunk(content="你"), AIMessageChunk(content="好")]]
    )
    tokens = []

    async def on_token(text: str) -> None:
        tokens.append(text)

    result = asyncio.run(
        AgentRunner(model, settings=_settings()).ainvoke("hi", on_token=on_token)
    )

    assert result.text == "你好"
    assert tokens == ["你", "好"]


def test_streaming_falls_back_for_models_without_astream():
    model = FakeModel([AIMessage(content="完整回答")])
    tokens = []

    async def on_token(text: str) -> None:
        tokens.append(text)

    result = asyncio.run(
        AgentRunner(model, settings=_settings()).ainvoke("hi", on_token=on_token)
    )

    assert result.text == "完整回答"
    assert tokens == ["完整回答"]


def test_streaming_resets_partial_text_before_tool_calls():
    model = StreamingFakeModel(
        [
            [
                AIMessageChunk(content="我先查一下"),
                AIMessageChunk(
                    content="",
                    tool_calls=[
                        _tool_call(
                            "generate_digest",
                            {"topic": "AI", "keyword": "Agent"},
                            "digest-1",
                        )
                    ],
                ),
            ],
            [AIMessageChunk(content="日报已生成")],
        ]
    )
    tokens = []
    resets = []

    async def on_token(text: str) -> None:
        tokens.append(text)

    async def on_reset() -> None:
        resets.append(True)

    async def digest_handler(topic: str, keyword: str) -> str:
        return '{"ok":true}'

    result = asyncio.run(
        AgentRunner(model, settings=_settings()).ainvoke(
            "生成日报",
            on_token=on_token,
            on_reset=on_reset,
            digest_handler=digest_handler,
        )
    )

    assert result.text == "日报已生成"
    assert tokens == ["我先查一下", "日报已生成"]
    assert resets == [True]


def test_bash_is_registered_only_with_approval_handler():
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call(
                        "bash",
                        {"command": "Write-Output test", "explanation": "执行测试命令"},
                        "bash-1",
                    )
                ],
            ),
            AIMessage(content="已完成"),
        ]
    )
    approvals = []

    async def approval_handler(command, explanation):
        approvals.append((command, explanation))
        return '{"ok":true}'

    result = asyncio.run(
        AgentRunner(model, settings=_settings()).ainvoke(
            "执行命令",
            bash_approval_handler=approval_handler,
        )
    )

    assert result.text == "已完成"
    assert "bash" in {tool.name for tool in model.tools}
    assert approvals == [("Write-Output test", "执行测试命令")]


def test_generate_digest_is_registered_only_for_this_invoke_and_replays_results():
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call(
                        "generate_digest",
                        {"topic": "比亚迪", "keyword": "固态电池"},
                        "digest-1",
                    ),
                    _tool_call(
                        "generate_digest",
                        {"topic": "比亚迪", "keyword": "自燃"},
                        "digest-2",
                    ),
                ],
            ),
            AIMessage(content="两份日报已生成"),
        ]
    )
    calls = []

    async def digest_handler(topic: str, keyword: str) -> str:
        calls.append((topic, keyword))
        return json.dumps({"ok": True, "report_id": len(calls)})

    result = asyncio.run(
        AgentRunner(model, settings=_settings()).ainvoke(
            "生成日报",
            digest_handler=digest_handler,
        )
    )

    assert result.text == "两份日报已生成"
    assert result.tool_calls == 2
    assert calls == [("比亚迪", "固态电池"), ("比亚迪", "自燃")]
    assert {tool.name for tool in model.tools} >= {"generate_digest"}


def test_file_tool_result_is_returned_to_model(tmp_path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "note.txt").write_text("hello", encoding="utf-8")
    model = FakeModel(
        [
                AIMessage(content="", tool_calls=[_tool_call("read_file", {"path": "note.txt"}, "call-1")]),
            AIMessage(content="文件内容是 hello"),
        ]
    )

    result = asyncio.run(
        AgentRunner(
            model,
            settings=_settings(),
            resolver=ManagedPathResolver(data_root),
        ).ainvoke("读取 note.txt")
    )

    assert result.text == "文件内容是 hello"
    assert result.tool_calls == 1
    tool_message = model.messages[1][-1]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.tool_call_id == "call-1"
    assert json.loads(tool_message.content)["result"]["content"] == "hello"


def test_multiple_tools_run_in_order_and_results_are_replayed(tmp_path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "note.txt").write_text("needle", encoding="utf-8")
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("list_dir", {"path": "."}, "call-1"),
                    _tool_call("search_content", {"keyword": "needle", "dir": "."}, "call-2"),
                ]
            ),
            AIMessage(content="已检查目录并完成搜索"),
        ]
    )

    result = asyncio.run(
        AgentRunner(model, settings=_settings(), resolver=ManagedPathResolver(data_root)).ainvoke("检查")
    )

    assert result.tool_calls == 2
    replayed = model.messages[1]
    assert [message.tool_call_id for message in replayed if isinstance(message, ToolMessage)] == [
        "call-1", "call-2"
    ]
    assert [message.name for message in replayed if isinstance(message, ToolMessage)] == [
        "list_dir", "search_content"
    ]
    assert [record.call_id for record in result.tool_call_records] == ["call-1", "call-2"]
    assert [record.name for record in result.tool_call_records] == ["list_dir", "search_content"]
    assert result.tool_call_records[0].arguments == {"path": "."}
    assert json.loads(result.tool_call_records[0].result)["ok"] is True
    assert result.tool_call_records[1].arguments == {"keyword": "needle", "dir": "."}
    assert json.loads(result.tool_call_records[1].result)["ok"] is True


def test_daily_prompt_and_task_context_are_separate_from_user_request():
    model = FakeModel([AIMessage(content="日报已生成")])
    system_prompt = get_prompt("digest_system")
    task_context = "关注主题：人工智能\n时区：Asia/Shanghai"

    result = asyncio.run(
        AgentRunner(model, settings=_settings()).ainvoke(
            "请生成日报",
            system_prompt=system_prompt,
            task_context=task_context,
        )
    )

    messages = model.messages[0]
    assert [type(message) for message in messages] == [SystemMessage, SystemMessage, HumanMessage]
    assert messages[0].content == system_prompt
    assert messages[1].content == task_context
    assert messages[2].content == "请生成日报"
    assert result.text == "日报已生成"


def test_empty_task_context_is_not_added_to_ordinary_conversation():
    model = FakeModel([AIMessage(content="ok")])

    asyncio.run(
        AgentRunner(model, settings=_settings()).ainvoke("你好", task_context="  ")
    )

    messages = model.messages[0]
    assert [type(message) for message in messages] == [SystemMessage, HumanMessage]
    assert messages[-1].content == "你好"


def test_model_can_request_tools_across_multiple_turns(tmp_path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "note.txt").write_text("needle", encoding="utf-8")
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("list_dir", {"path": "."}, "call-1")],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call(
                        "search_content",
                        {"keyword": "needle", "dir": "."},
                        "call-2",
                    )
                ],
            ),
            AIMessage(content="连续工具调用完成"),
        ]
    )

    runner = AgentRunner(
        model,
        settings=_settings(),
        resolver=ManagedPathResolver(data_root),
    )
    result = asyncio.run(runner.ainvoke("列出并搜索文件"))

    assert result.tool_calls == 2
    assert result.text == "连续工具调用完成"
    assert len(model.messages) == 3
    assert [
        message.tool_call_id
        for message in model.messages[2]
        if isinstance(message, ToolMessage)
    ] == ["call-1", "call-2"]


def test_search_tool_uses_lazy_client_and_serializes_results():
    class FakeSearch:
        def __init__(self, settings):
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            self.closed = True

        async def search(self, query, limit=5):
            assert query == "AI"
            assert limit == 6
            return [
                OnlineSearchResult(
                    title="Headline",
                    url="https://example.com",
                    snippet="Summary",
                    published_at=None,
                    source_site="example.com",
                    searched_at=datetime.now(UTC),
                )
            ]

    clients = []

    def factory(settings):
        client = FakeSearch(settings)
        clients.append(client)
        return client

    model = FakeModel(
        [
                AIMessage(content="", tool_calls=[_tool_call("web_search", {"query": "AI", "limit": 6}, "search-1")]),
            AIMessage(content="搜索完成"),
        ]
    )
    result = asyncio.run(
        AgentRunner(model, settings=_settings(), search_client_factory=factory).ainvoke("搜索 AI")
    )

    assert result.text == "搜索完成"
    assert len(clients) == 1 and clients[0].closed is True
    payload = json.loads(model.messages[1][-1].content)
    assert payload["results"][0]["title"] == "Headline"


def test_search_error_is_sanitized_and_client_is_closed():
    class FakeSearch:
        closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            self.closed = True

        async def search(self, query, limit=5):
            raise RuntimeError("upstream failure test-api-key")

    clients = []

    def factory(settings):
        client = FakeSearch()
        clients.append(client)
        return client

    model = FakeModel(
        [
            AIMessage(content="", tool_calls=[_tool_call("web_search", {"query": "AI"}, "search-1")]),
            AIMessage(content="搜索暂不可用"),
        ]
    )
    asyncio.run(
        AgentRunner(model, settings=_settings(), search_client_factory=factory).ainvoke("搜索")
    )

    tool_result = model.messages[1][-1].content
    assert "upstream failure" not in tool_result
    assert "test-api-key" not in tool_result
    assert json.loads(tool_result)["error"]["code"] == "search_error"
    assert clients[0].closed is True


def test_unknown_tool_and_invalid_arguments_are_safe():
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("not_registered", {}, "call-1"),
                    _tool_call("read_file", {"unexpected": True}, "call-2"),
                ]
            ),
            AIMessage(content="已处理")
        ]
    )
    result = asyncio.run(AgentRunner(model, settings=_settings()).ainvoke("处理"))

    assert result.tool_calls == 2
    errors = [json.loads(message.content)["error"] for message in model.messages[1] if isinstance(message, ToolMessage)]
    assert [error["code"] for error in errors] == ["unknown_tool", "invalid_arguments"]
    assert [record.call_id for record in result.tool_call_records] == ["call-1", "call-2"]
    assert [record.name for record in result.tool_call_records] == ["not_registered", "read_file"]
    assert result.tool_call_records[0].arguments == {}
    assert json.loads(result.tool_call_records[0].result)["error"]["code"] == "unknown_tool"
    assert result.tool_call_records[1].arguments == {"unexpected": True}
    assert json.loads(result.tool_call_records[1].result)["error"]["code"] == "invalid_arguments"


def test_tool_call_limit_stops_without_an_extra_model_call(tmp_path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("list_dir", {"path": "."}, "call-1"),
                    _tool_call("list_dir", {"path": "."}, "call-2"),
                ]
            ),
            AIMessage(content="不应调用")
        ]
    )
    result = asyncio.run(
        AgentRunner(
            model,
            settings=_settings(agent_max_tool_calls=1),
            resolver=ManagedPathResolver(data_root),
        ).ainvoke("持续调用")
    )

    assert result.tool_calls == 1
    assert len(result.tool_call_records) == 1
    assert result.tool_call_records[0].call_id == "call-1"
    assert result.tool_call_records[0].name == "list_dir"
    assert result.stop_reason == "max_tool_calls"
    assert "工具调用达到上限" in result.text
    assert '"ok":true' in result.text
    assert len(model.messages) == 1


def test_sync_invoke_rejects_running_event_loop():
    runner = AgentRunner(FakeModel([AIMessage(content="ok")]), settings=_settings())

    async def run():
        with pytest.raises(RuntimeError, match="active event loop"):
            runner.invoke("hello")

    asyncio.run(run())


def test_sync_invoke_runs_direct_answer():
    runner = AgentRunner(FakeModel([AIMessage(content="同步回答")]), settings=_settings())

    assert runner.invoke("hello").text == "同步回答"


def test_sync_invoke_forwards_system_prompt_and_task_context():
    model = FakeModel([AIMessage(content="完成")])
    runner = AgentRunner(model, settings=_settings())

    runner.invoke("执行日报", system_prompt="日报系统提示词", task_context="日报偏好")

    messages = model.messages[0]
    assert [type(message) for message in messages] == [SystemMessage, SystemMessage, HumanMessage]
    assert [message.content for message in messages] == [
        "日报系统提示词",
        "日报偏好",
        "执行日报",
    ]
