from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path

import pytest

from agentnews.tools.file_tools import (
    FileToolError,
    LocalFileTools,
    list_dir,
    read_file,
    search_content,
    write_file,
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


def test_search_content_returns_recursive_matches_with_line_metadata(tmp_path):
    root = tmp_path / "data"
    (root / "nested").mkdir(parents=True)
    (root / "top.txt").write_bytes(
        "first\nneedle at the top\nlast\n".encode("utf-8")
    )
    (root / "nested" / "child.md").write_bytes(
        "nothing\nsecond needle\n".encode("utf-8")
    )
    resolver = ManagedPathResolver(root)

    result = search_content("needle", ".", resolver=resolver)

    assert result.path == "."
    assert result.count == 2
    assert result.files_scanned == 2
    assert [(match.path, match.line_number, match.column) for match in result.matches] == [
        ("nested/child.md", 2, 8),
        ("top.txt", 2, 1),
    ]
    assert result.matches[0].snippet == "second needle"
    assert result.to_dict()["matches"][1]["snippet"] == "needle at the top"


def test_search_content_supports_no_results_and_non_recursive_mode(tmp_path):
    root = tmp_path / "data"
    (root / "nested").mkdir(parents=True)
    (root / "nested" / "child.txt").write_bytes(b"needle")
    resolver = ManagedPathResolver(root)

    assert search_content("missing", ".", resolver=resolver).matches == ()
    non_recursive = search_content(
        "needle", ".", resolver=resolver, recursive=False
    )
    assert non_recursive.count == 0
    assert search_content("NEEDLE", ".", resolver=resolver, case_sensitive=False).count == 1
    assert search_content(keyword="needle", dir=".", resolver=resolver).count == 1


def test_search_content_enforces_resource_limits(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "one.txt").write_bytes(b"needle")
    (root / "two.txt").write_bytes(b"needle")
    resolver = ManagedPathResolver(root)

    with pytest.raises(FileToolError) as limited:
        search_content("needle", ".", resolver=resolver, max_files=1)
    assert limited.value.code == "search_limit_exceeded"

    with pytest.raises(FileToolError) as invalid:
        search_content("needle", ".", resolver=resolver, max_matches=0)
    assert invalid.value.code == "invalid_request"


def test_search_content_skips_sensitive_files(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / ".env").write_bytes(b"needle=private")
    (root / "public.txt").write_bytes(b"public")

    result = search_content("needle", ".", resolver=ManagedPathResolver(root))

    assert result.matches == ()
    assert result.files_skipped == 1


def test_write_file_creates_new_file_and_returns_hash(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    resolver = ManagedPathResolver(root)

    result = write_file("new.txt", "你好", resolver=resolver)

    assert result.path == "new.txt"
    assert result.created is True
    assert result.overwritten is False
    assert result.size_bytes == len("你好".encode("utf-8"))
    assert result.content_hash
    assert (root / "new.txt").read_bytes() == "你好".encode("utf-8")


def test_write_file_requires_explicit_overwrite_confirmation(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "existing.txt").write_bytes(b"old")
    resolver = ManagedPathResolver(root)

    with pytest.raises(FileToolError) as denied:
        write_file("existing.txt", "new", resolver=resolver)
    assert denied.value.code == "overwrite_required"
    assert (root / "existing.txt").read_bytes() == b"old"

    result = write_file("existing.txt", "new", resolver=resolver, overwrite=True)
    assert result.overwritten is True
    assert (root / "existing.txt").read_bytes() == b"new"

    write_file("existing.txt", "again", resolver=resolver, confirm_overwrite=True)
    assert (root / "existing.txt").read_bytes() == b"again"

    with pytest.raises(FileToolError) as still_requires_confirmation:
        write_file("existing.txt", "denied", resolver=resolver, confirm_overwrite=False)
    assert still_requires_confirmation.value.code == "overwrite_required"
    assert (root / "existing.txt").read_bytes() == b"again"


def test_write_file_rejects_invalid_content_and_requires_existing_parent(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    resolver = ManagedPathResolver(root, max_file_size_bytes=4)

    with pytest.raises(FileToolError) as too_large:
        write_file("large.txt", "12345", resolver=resolver)
    assert too_large.value.code == "file_too_large"

    with pytest.raises(FileToolError) as missing_parent:
        write_file("missing/new.txt", "ok", resolver=resolver)
    assert missing_parent.value.code == "not_file"

    with pytest.raises(FileToolError) as nul:
        write_file("nul.txt", "a\x00b", resolver=resolver)
    assert nul.value.code == "invalid_text"

    with pytest.raises(FileToolError) as outside:
        write_file("../outside.txt", "no", resolver=resolver)
    assert outside.value.code == "unsafe_path"


def test_concurrent_new_writes_have_one_winner(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    resolver = ManagedPathResolver(root)

    def attempt(value: str):
        try:
            return ("ok", write_file("race.txt", value, resolver=resolver))
        except FileToolError as error:
            return ("error", error)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, ("first", "second")))

    assert [kind for kind, _ in outcomes].count("ok") == 1
    assert [kind for kind, _ in outcomes].count("error") == 1
    error = next(value for kind, value in outcomes if kind == "error")
    assert error.code == "overwrite_required"
    assert (root / "race.txt").read_text(encoding="utf-8") in {"first", "second"}


def test_failed_atomic_create_leaves_no_partial_target(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    resolver = ManagedPathResolver(root)

    def fail_link(source, target):
        raise OSError("simulated publish failure")

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(FileToolError) as failure:
        write_file("target.txt", "complete content", resolver=resolver)

    assert failure.value.code == "io_error"
    assert list(root.iterdir()) == []
