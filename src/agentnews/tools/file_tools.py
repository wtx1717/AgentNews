"""Bounded local file tools built on top of :mod:`path_security`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import stat
from typing import Any

from .path_security import (
    FileTooLargeError,
    InvalidTextFileError,
    ManagedPathResolver,
    PathSecurityError,
    PathTypeError,
    SensitivePathError,
    UnsafePathError,
)


class FileToolError(RuntimeError):
    """A user-facing error raised by a local file tool.

    ``code`` is stable for callers that need to branch on an error, while
    ``message`` remains suitable for displaying directly to a user.
    """

    def __init__(self, code: str, message: str, *, path: str | None = None) -> None:
        self.code = code
        self.message = message
        self.path = path
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "path": self.path}

    as_dict = to_dict


@dataclass(frozen=True)
class DirectoryEntry:
    """Metadata for one direct child of a managed directory."""

    name: str
    kind: str
    size_bytes: int | None
    modified_at: str
    relative_path: str

    @property
    def type(self) -> str:
        """Compatibility alias for consumers that call the kind a type."""
        return self.kind

    @property
    def entry_type(self) -> str:
        return self.kind

    @property
    def path(self) -> str:
        return self.relative_path

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "modified_at": self.modified_at,
            "relative_path": self.relative_path,
        }

    as_dict = to_dict


@dataclass(frozen=True)
class DirectoryListing:
    """Stable result returned by :func:`list_dir`."""

    path: str
    entries: tuple[DirectoryEntry, ...]

    @property
    def count(self) -> int:
        return len(self.entries)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "entries": [entry.to_dict() for entry in self.entries],
            "count": self.count,
        }

    as_dict = to_dict


@dataclass(frozen=True)
class FileReadResult:
    """Stable result returned by :func:`read_file`."""

    path: str
    content: str
    encoding: str
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "content": self.content,
            "encoding": self.encoding,
            "size_bytes": self.size_bytes,
        }

    as_dict = to_dict


class LocalFileTools:
    """Expose safe directory listing and UTF-8 file reading operations."""

    def __init__(self, resolver: ManagedPathResolver | None = None) -> None:
        self.resolver = resolver or ManagedPathResolver()

    def list_dir(self, path: str | os.PathLike[str]) -> DirectoryListing:
        raw_path = _display_input(path)
        try:
            directory = self.resolver.resolve(
                path,
                expected_type="directory",
                check_file_size=False,
            )
            children = list(directory.iterdir())
        except Exception as error:
            raise _as_file_tool_error(error, operation="list", path=raw_path) from error

        entries: list[DirectoryEntry] = []
        for child in children:
            child_path = _display_input(child)
            try:
                # Resolve every child so a symlink cannot make metadata escape
                # the managed root. Size checking is deliberately deferred to
                # read_file; listing only needs bounded metadata.
                resolved_child = self.resolver.resolve(
                    child,
                    expected_type="any",
                    check_file_size=False,
                )
                child_stat = resolved_child.stat()
                if not (
                    stat.S_ISREG(child_stat.st_mode)
                    or stat.S_ISDIR(child_stat.st_mode)
                ):
                    raise PathTypeError("only regular files and directories are allowed")
                kind = "file" if resolved_child.is_file() else "directory"
                size = child_stat.st_size if kind == "file" else None
                relative_path = _relative_path(self.resolver, resolved_child)
                entries.append(
                    DirectoryEntry(
                        name=child.name,
                        kind=kind,
                        size_bytes=size,
                        modified_at=_utc_timestamp(child_stat.st_mtime),
                        relative_path=relative_path,
                    )
                )
            except (SensitivePathError, FileNotFoundError):
                # Protected names and races with a disappearing child are not
                # exposed as directory contents.
                continue
            except Exception as error:
                raise _as_file_tool_error(error, operation="list", path=child_path) from error

        entries.sort(key=lambda entry: (entry.name.casefold(), entry.name))
        return DirectoryListing(
            path=_relative_path(self.resolver, directory),
            entries=tuple(entries),
        )

    def read_file(self, path: str | os.PathLike[str]) -> FileReadResult:
        raw_path = _display_input(path)
        try:
            target = self.resolver.resolve(path, expected_type="file")
            content = self.resolver.read_text(target)
            size_bytes = target.stat().st_size
        except Exception as error:
            raise _as_file_tool_error(error, operation="read", path=raw_path) from error

        return FileReadResult(
            path=_relative_path(self.resolver, target),
            content=content,
            encoding="utf-8",
            size_bytes=size_bytes,
        )


def list_dir(
    path: str | os.PathLike[str], *, resolver: ManagedPathResolver | None = None
) -> DirectoryListing:
    """List direct children beneath the configured managed root."""
    return LocalFileTools(resolver).list_dir(path)


def read_file(
    path: str | os.PathLike[str], *, resolver: ManagedPathResolver | None = None
) -> FileReadResult:
    """Read one bounded UTF-8 text file beneath the configured managed root."""
    return LocalFileTools(resolver).read_file(path)


def _display_input(path: str | os.PathLike[str]) -> str:
    try:
        return os.fspath(path)
    except TypeError:
        return str(path)


def _relative_path(resolver: ManagedPathResolver, path: Path) -> str:
    relative = path.resolve().relative_to(resolver.root)
    return relative.as_posix() or "."


def _utc_timestamp(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _as_file_tool_error(error: Exception, *, operation: str, path: str) -> FileToolError:
    if isinstance(error, FileToolError):
        return error
    if isinstance(error, FileNotFoundError):
        return FileToolError("not_found", f"路径不存在：{path}", path=path)
    if isinstance(error, PermissionError):
        return FileToolError("permission_denied", f"没有权限访问路径：{path}", path=path)
    if isinstance(error, FileTooLargeError):
        return FileToolError("file_too_large", f"文件超过大小限制：{path}", path=path)
    if isinstance(error, InvalidTextFileError):
        return FileToolError("invalid_text", f"文件不是有效的 UTF-8 文本：{path}", path=path)
    if isinstance(error, SensitivePathError):
        return FileToolError("sensitive_path", f"敏感路径不可访问：{path}", path=path)
    if isinstance(error, UnsafePathError):
        return FileToolError("unsafe_path", f"路径不在允许的目录内：{path}", path=path)
    if isinstance(error, PathTypeError):
        code = "not_directory" if operation == "list" else "not_file"
        expected = "目录" if operation == "list" else "普通文件"
        return FileToolError(code, f"路径必须指向{expected}：{path}", path=path)
    if isinstance(error, PathSecurityError):
        return FileToolError("path_error", f"路径校验失败：{path}", path=path)
    if isinstance(error, OSError):
        return FileToolError("io_error", f"访问路径时发生 I/O 错误：{path}", path=path)
    return FileToolError("invalid_request", f"无法{('列出目录' if operation == 'list' else '读取文件')}：{path}", path=path)
