from __future__ import annotations

from pathlib import Path

import pytest

from agentnews.tools.file_tools import (
    FileToolError,
    LocalFileTools,
    list_dir,
    read_file,
)
from agentnews.tools.path_security import ManagedPathResolver


def test_list_dir_returns_sorted_structured_entries(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "z.txt").write_text("z", encoding="utf-8")
    (root / "A.txt").write_text("a", encoding="utf-8")
    (root / "folder").mkdir()

    result = list_dir(".", resolver=ManagedPathResolver(root))

    assert result.path == "."
    assert [entry.name for entry in result.entries] == ["A.txt", "folder", "z.txt"]
    assert result.count == 3
    assert result.entries[0].kind == "file"
    assert result.entries[0].type == "file"
    assert result.entries[0].entry_type == "file"
    assert result.entries[0].path == "A.txt"
    assert result.entries[0].size_bytes == 1
    assert result.entries[1].kind == "directory"
    assert result.entries[1].size_bytes is None
    assert result.to_dict()["entries"][0]["relative_path"] == "A.txt"


def test_list_dir_reports_missing_and_non_directory_paths(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "note.txt").write_text("note", encoding="utf-8")
    tools = LocalFileTools(ManagedPathResolver(root))

    with pytest.raises(FileToolError) as missing:
        tools.list_dir("missing")
    assert missing.value.code == "not_found"
    assert "不存在" in missing.value.message

    with pytest.raises(FileToolError) as wrong_type:
        tools.list_dir("note.txt")
    assert wrong_type.value.code == "not_directory"


def test_read_file_supports_utf8_and_empty_files(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "hello.txt").write_bytes("你好\nworld".encode("utf-8"))
    (root / "empty.txt").write_bytes(b"")
    tools = LocalFileTools(ManagedPathResolver(root))

    result = tools.read_file("hello.txt")
    assert result.content == "你好\nworld"
    assert result.encoding == "utf-8"
    assert result.size_bytes == len("你好\nworld".encode("utf-8"))
    assert result.path == "hello.txt"
    assert result.to_dict()["encoding"] == "utf-8"
    assert tools.read_file("empty.txt").to_dict()["content"] == ""


@pytest.mark.parametrize(
    ("filename", "code"),
    [("invalid.txt", "invalid_text"), ("missing.txt", "not_found")],
)
def test_read_file_reports_read_errors(tmp_path, filename, code):
    root = tmp_path / "data"
    root.mkdir()
    if filename == "invalid.txt":
        (root / filename).write_bytes(b"\xff")
    tools = LocalFileTools(ManagedPathResolver(root))

    with pytest.raises(FileToolError) as error:
        tools.read_file(filename)
    assert error.value.code == code
    assert error.value.to_dict()["path"] == filename


def test_read_file_reports_non_file_and_permission_errors(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    (root / "folder").mkdir()
    tools = LocalFileTools(ManagedPathResolver(root))

    with pytest.raises(FileToolError) as wrong_type:
        tools.read_file("folder")
    assert wrong_type.value.code == "not_file"

    target = root / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    original_open = Path.open

    def deny_open(self, *args, **kwargs):
        if self == target:
            raise PermissionError("permission denied")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_open)
    with pytest.raises(FileToolError) as denied:
        tools.read_file("secret.txt")
    assert denied.value.code == "permission_denied"

    original_iterdir = Path.iterdir

    def deny_iterdir(self):
        if self == root:
            raise PermissionError("permission denied")
        return original_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", deny_iterdir)
    with pytest.raises(FileToolError) as denied_listing:
        tools.list_dir(".")
    assert denied_listing.value.code == "permission_denied"
