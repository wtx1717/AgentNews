from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from agentnews.tools.digest import GenerateDigestInput, create_digest_tool


def test_generate_digest_schema_is_strict_and_requires_topic_and_keyword():
    valid = GenerateDigestInput(topic="比亚迪", keyword="固态电池")
    assert valid.topic == "比亚迪"
    assert valid.keyword == "固态电池"

    with pytest.raises(ValidationError):
        GenerateDigestInput(topic="比亚迪")
    with pytest.raises(ValidationError):
        GenerateDigestInput(topic="比亚迪", keyword="固态电池", extra=True)
    with pytest.raises(ValidationError):
        GenerateDigestInput(topic=" ", keyword="固态电池")


def test_generate_digest_tool_forwards_one_call_without_orchestration():
    calls = []

    async def handler(topic: str, keyword: str) -> str:
        calls.append((topic, keyword))
        return json.dumps({"ok": True, "topic": topic, "keyword": keyword})

    tool = create_digest_tool(handler)
    result = asyncio.run(tool.ainvoke({"topic": "比亚迪", "keyword": "专利"}))

    assert json.loads(result)["keyword"] == "专利"
    assert calls == [("比亚迪", "专利")]
