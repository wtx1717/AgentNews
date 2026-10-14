from __future__ import annotations

import json

from langchain_core.tools import StructuredTool

from agentnews.tools import create_local_file_tools
from agentnews.tools.path_security import ManagedPathResolver


def _tool_map(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    tools = create_local_file_tools(ManagedPathResolver(root))
    return root, {tool.name: tool for tool in tools}


def _payload(tool, arguments):
    return json.loads(tool.invoke(arguments))


def test_registry_exposes_only_four_described_langchain_tools(tmp_path):
    _, tools = _tool_map(tmp_path)

    assert set(tools) == {"list_dir", "read_file", "search_content", "write_file"}
    assert all(isinstance(tool, StructuredTool) for tool in tools.values())
    assert all(tool.description for tool in tools.values())
    assert "bash" not in tools
    assert "path" in tools["list_dir"].args_schema.model_fields
    assert "dir" in tools["search_content"].args_schema.model_fields
    assert "overwrite" in tools["write_file"].args_schema.model_fields


def test_tool_schemas_reject_invalid_and_extra_arguments(tmp_path):
    _, tools = _tool_map(tmp_path)

    missing_path = _payload(tools["list_dir"], {})
    assert missing_path["ok"] is False
    assert missing_path["error"]["code"] == "invalid_arguments"
    assert missing_path["error"]["details"][0]["field"] == "path"

    empty_keyword = _payload(tools["search_content"], {"keyword": "", "dir": "."})
    assert empty_keyword["error"]["code"] == "invalid_arguments"

    empty_path = _payload(tools["list_dir"], {"path": ""})
    assert empty_path["error"]["code"] == "invalid_arguments"

    wrong_type = _payload(
        tools["search_content"],
        {"keyword": "x", "dir": ".", "recursive": "yes"},
    )
    assert wrong_type["error"]["code"] == "invalid_arguments"

    nul_path = _payload(tools["list_dir"], {"path": "bad\x00path"})
    assert nul_path["error"]["code"] == "invalid_arguments"

    extra = _payload(tools["read_file"], {"path": "note.txt", "command": "echo"})
    assert extra["error"]["code"] == "invalid_arguments"


def test_tools_return_unified_success_and_runtime_error_envelopes(tmp_path):
    root, tools = _tool_map(tmp_path)
    (root / "note.txt").write_text("hello needle", encoding="utf-8")

    listed = _payload(tools["list_dir"], {"path": "."})
    assert listed["ok"] is True
    assert listed["result"]["count"] == 1

    read = _payload(tools["read_file"], {"path": "note.txt"})
    assert read["ok"] is True
    assert read["result"]["content"] == "hello needle"

    searched = _payload(
        tools["search_content"], {"keyword": "needle", "dir": "."}
    )
    assert searched["ok"] is True
    assert searched["result"]["matches"][0]["line_number"] == 1

    missing = _payload(tools["read_file"], {"path": "missing.txt"})
    assert missing["ok"] is False
    assert missing["error"]["code"] == "not_found"
    assert set(missing["error"]) == {"code", "message", "path", "details"}
    assert missing["error"]["details"] == []


def test_dangerous_paths_are_reported_without_accessing_outside_root(tmp_path):
    root, tools = _tool_map(tmp_path)
    outside = tmp_path / "outside.txt"

    read = _payload(tools["read_file"], {"path": "../outside.txt"})
    write = _payload(
        tools["write_file"], {"path": "../outside.txt", "content": "blocked"}
    )

    assert read["error"]["code"] == "unsafe_path"
    assert write["error"]["code"] == "unsafe_path"
    assert not outside.exists()
    assert list(root.iterdir()) == []

    (root / ".env").write_text("SECRET=value", encoding="utf-8")
    sensitive = _payload(tools["read_file"], {"path": ".env"})
    assert sensitive["error"]["code"] == "sensitive_path"


def test_write_tool_requires_overwrite_authorization(tmp_path):
    root, tools = _tool_map(tmp_path)
    (root / "existing.txt").write_text("old", encoding="utf-8")

    denied = _payload(
        tools["write_file"], {"path": "existing.txt", "content": "new"}
    )
    assert denied["error"]["code"] == "overwrite_required"
    assert (root / "existing.txt").read_text(encoding="utf-8") == "old"

    written = _payload(
        tools["write_file"],
        {"path": "existing.txt", "content": "new", "overwrite": True},
    )
    assert written["ok"] is True
    assert (root / "existing.txt").read_text(encoding="utf-8") == "new"
