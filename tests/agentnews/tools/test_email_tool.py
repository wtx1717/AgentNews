from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from agentnews.agent import AgentRunner
from agentnews.config import Settings
from agentnews.tools.email import SendReportEmailInput, create_send_report_email_tool


def test_email_tool_only_accepts_report_selection_and_explicit_resend():
    calls = []

    async def handler(report_id, resend):
        calls.append((report_id, resend))
        return '{"ok":true}'

    tool = create_send_report_email_tool(handler)
    assert set(tool.args) == {"report_id", "resend"}
    with pytest.raises(ValidationError):
        SendReportEmailInput.model_validate(
            {"report_id": 1, "recipient": "other@example.test", "body": "自由正文"}
        )

    result = asyncio.run(tool.ainvoke({"report_id": 7, "resend": True}))

    assert result == '{"ok":true}'
    assert calls == [(7, True)]


def test_agent_runner_registers_email_tool_only_for_explicit_handler():
    class FakeModel:
        def __init__(self):
            self.responses = [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "send_report_email",
                            "args": {"report_id": 7, "resend": False},
                            "id": "email-1",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="已处理"),
            ]
            self.tools = []

        def bind_tools(self, tools):
            self.tools = list(tools)
            return self

        async def ainvoke(self, _messages):
            return self.responses.pop(0)

    model = FakeModel()
    calls = []

    async def email_handler(report_id, resend):
        calls.append((report_id, resend))
        return '{"ok":true}'

    async def run():
        runner = AgentRunner(model, settings=Settings(_env_file=None))
        return await runner.ainvoke("请发送日报", email_handler=email_handler)

    result = asyncio.run(run())
    assert result.text == "已处理"
    assert "send_report_email" in {tool.name for tool in model.tools}
    assert calls == [(7, False)]
