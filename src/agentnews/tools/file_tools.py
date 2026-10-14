"""Bounded local file tools built on top of :mod:`path_security`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import threading
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


@dataclass(frozen=True)
class SearchMatch:
    """One line containing a search keyword."""

    path: str
    line_number: int
    column: int
    snippet: str

    @property
    def relative_path(self) -> str:
        return self.path

    @property
    def line(self) -> int:
        return self.line_number

    @property
    def match(self) -> str:
        return self.snippet

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line_number": self.line_number,
            "column": self.column,
            "snippet": self.snippet,
        }

    as_dict = to_dict


@dataclass(frozen=True)
class SearchResult:
    """Stable result returned by :func:`search_content`."""

    keyword: str
    path: str
    matches: tuple[SearchMatch, ...]
    files_scanned: int
    files_skipped: int = 0

    @property
    def count(self) -> int:
        return len(self.matches)

    @property
    def results(self) -> tuple[SearchMatch, ...]:
        return self.matches

    def to_dict(self) -> dict[str, Any]:
        return {
            "keyword": self.keyword,
            "path": self.path,
            "matches": [match.to_dict() for match in self.matches],
            "count": self.count,
            "files_scanned": self.files_scanned,
            "files_skipped": self.files_skipped,
        }

    as_dict = to_dict


@dataclass(frozen=True)
class FileWriteResult:
    """Stable result returned by :func:`write_file`."""

    path: str
    size_bytes: int
    content_hash: str
    overwritten: bool

    @property
    def bytes_written(self) -> int:
        return self.size_bytes

    @property
    def created(self) -> bool:
        return not self.overwritten

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "bytes_written": self.size_bytes,
            "content_hash": self.content_hash,
            "overwritten": self.overwritten,
            "created": self.created,
        }

    as_dict = to_dict


_WRITE_LOCK = threading.Lock()
DEFAULT_MAX_SEARCH_FILES = 1_000
DEFAULT_MAX_SEARCH_DEPTH = 32
DEFAULT_MAX_SEARCH_MATCHES = 10_000
MAX_SEARCH_KEYWORD_LENGTH = 256
MAX_SEARCH_SNIPPET_LENGTH = 500


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

    def search_content(
        self,
        keyword: str,
        directory: str | os.PathLike[str] | None = None,
        *,
        dir: str | os.PathLike[str] | None = None,
        recursive: bool = True,
        case_sensitive: bool = True,
        max_files: int = DEFAULT_MAX_SEARCH_FILES,
        max_depth: int = DEFAULT_MAX_SEARCH_DEPTH,
        max_matches: int = DEFAULT_MAX_SEARCH_MATCHES,
    ) -> SearchResult:
        """Search UTF-8 text files below a managed directory.

        Files that are protected, too large, invalid UTF-8, or disappear
        during traversal are skipped. The requested directory itself still
        produces a readable error when it is inaccessible or outside the root.
        """
        try:
            directory = _pick_search_directory(directory, dir)
        except Exception as error:
            raise _as_file_tool_error(error, operation="search", path="") from error
        raw_directory = _display_input(directory)
        try:
            _validate_search_options(
                keyword, recursive, case_sensitive, max_files, max_depth, max_matches
            )
            root_directory = self.resolver.resolve(
                directory,
                expected_type="directory",
                check_file_size=False,
            )
        except Exception as error:
            raise _as_file_tool_error(
                error, operation="search", path=raw_directory
            ) from error

        matches: list[SearchMatch] = []
        files_scanned = 0
        files_skipped = 0
        pending = [(root_directory, 0)]
        visited_directories: set[Path] = set()
        needle = keyword if case_sensitive else keyword.casefold()

        while pending:
            current, depth = pending.pop()
            try:
                current = self.resolver.resolve(
                    current,
                    expected_type="directory",
                    check_file_size=False,
                )
            except (FileNotFoundError, PermissionError, PathSecurityError, OSError):
                files_skipped += 1
                continue
            if current in visited_directories:
                continue
            visited_directories.add(current)

            try:
                children = sorted(
                    current.iterdir(), key=lambda child: (child.name.casefold(), child.name)
                )
            except (FileNotFoundError, PermissionError, OSError) as error:
                if current == root_directory:
                    raise _as_file_tool_error(
                        error, operation="search", path=raw_directory
                    ) from error
                files_skipped += 1
                continue

            for child in children:
                try:
                    resolved_child = self.resolver.resolve(
                        child,
                        expected_type="any",
                        check_file_size=False,
                    )
                    child_stat = resolved_child.stat()
                    if stat.S_ISDIR(child_stat.st_mode):
                        if recursive:
                            if depth >= max_depth:
                                raise FileToolError(
                                    "search_limit_exceeded",
                                    f"搜索超过最大递归深度 {max_depth}：{raw_directory}",
                                    path=raw_directory,
                                )
                            pending.append((resolved_child, depth + 1))
                        continue
                    if not stat.S_ISREG(child_stat.st_mode):
                        files_skipped += 1
                        continue

                    if files_scanned >= max_files:
                        raise FileToolError(
                            "search_limit_exceeded",
                            f"搜索文件数超过上限 {max_files}：{raw_directory}",
                            path=raw_directory,
                        )
                    files_scanned += 1
                    content = self.resolver.read_text(resolved_child)
                except (
                    FileNotFoundError,
                    PermissionError,
                    SensitivePathError,
                    UnsafePathError,
                    FileTooLargeError,
                    InvalidTextFileError,
                    PathTypeError,
                    OSError,
                ):
                    files_skipped += 1
                    continue
                except FileToolError:
                    raise

                relative_path = _relative_path(self.resolver, resolved_child)
                for line_number, line in enumerate(content.splitlines(), start=1):
                    column = _find_keyword_column(line, needle, case_sensitive)
                    if column >= 0:
                        if len(matches) >= max_matches:
                            raise FileToolError(
                                "search_limit_exceeded",
                                f"搜索匹配数超过上限 {max_matches}：{raw_directory}",
                                path=raw_directory,
                            )
                        matches.append(
                            SearchMatch(
                                path=relative_path,
                                line_number=line_number,
                                column=column + 1,
                                snippet=_make_snippet(line, column),
                            )
                        )

        matches.sort(key=lambda item: (item.path.casefold(), item.path, item.line_number))
        return SearchResult(
            keyword=keyword,
            path=_relative_path(self.resolver, root_directory),
            matches=tuple(matches),
            files_scanned=files_scanned,
            files_skipped=files_skipped,
        )

    def write_file(
        self,
        path: str | os.PathLike[str],
        content: str,
        *,
        overwrite: bool = False,
        confirm_overwrite: bool | None = None,
    ) -> FileWriteResult:
        """Write bounded UTF-8 text with exclusive creation and atomic replacement.

        Existing files are never replaced unless the caller explicitly passes
        ``overwrite=True`` or ``confirm_overwrite=True``.
        """
        raw_path = _display_input(path)
        try:
            if not isinstance(overwrite, bool):
                raise TypeError("overwrite must be a boolean")
            if confirm_overwrite is not None and not isinstance(confirm_overwrite, bool):
                raise TypeError("confirm_overwrite must be a boolean or None")
            if not isinstance(content, str):
                raise InvalidTextFileError("content must be text")
            encoded = self.resolver.encode_text(content)
            target = self.resolver.resolve(
                path,
                expected_type="file",
                allow_missing_leaf=True,
            )
            if _lexical_candidate(self.resolver, path).is_symlink():
                raise UnsafePathError("symbolic link targets cannot be written")
        except Exception as error:
            raise _as_file_tool_error(error, operation="write", path=raw_path) from error

        allow_overwrite = overwrite or confirm_overwrite is True
        with _WRITE_LOCK:
            try:
                target_exists = target.exists()
                if target_exists and not allow_overwrite:
                    raise FileToolError(
                        "overwrite_required",
                        f"文件已存在，覆盖写入需要明确确认：{raw_path}",
                        path=raw_path,
                    )

                if target_exists:
                    _atomic_replace(target, encoded)
                    overwritten = True
                else:
                    _exclusive_create(target, encoded)
                    overwritten = False
            except FileToolError:
                raise
            except FileExistsError as error:
                raise FileToolError(
                    "overwrite_required",
                    f"文件已存在，覆盖写入需要明确确认：{raw_path}",
                    path=raw_path,
                ) from error
            except Exception as error:
                raise _as_file_tool_error(error, operation="write", path=raw_path) from error

        return FileWriteResult(
            path=_relative_path(self.resolver, target),
            size_bytes=len(encoded),
            content_hash=hashlib.sha256(encoded).hexdigest(),
            overwritten=overwritten,
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


def search_content(
    keyword: str,
    directory: str | os.PathLike[str] | None = None,
    *,
    dir: str | os.PathLike[str] | None = None,
    resolver: ManagedPathResolver | None = None,
    recursive: bool = True,
    case_sensitive: bool = True,
    max_files: int = DEFAULT_MAX_SEARCH_FILES,
    max_depth: int = DEFAULT_MAX_SEARCH_DEPTH,
    max_matches: int = DEFAULT_MAX_SEARCH_MATCHES,
) -> SearchResult:
    """Search text files recursively beneath a managed directory."""
    return LocalFileTools(resolver).search_content(
        keyword,
        directory,
        dir=dir,
        recursive=recursive,
        case_sensitive=case_sensitive,
        max_files=max_files,
        max_depth=max_depth,
        max_matches=max_matches,
    )


def write_file(
    path: str | os.PathLike[str],
    content: str,
    *,
    resolver: ManagedPathResolver | None = None,
    overwrite: bool = False,
    confirm_overwrite: bool | None = None,
) -> FileWriteResult:
    """Write bounded UTF-8 text beneath the managed root."""
    return LocalFileTools(resolver).write_file(
        path,
        content,
        overwrite=overwrite,
        confirm_overwrite=confirm_overwrite,
    )


def _display_input(path: str | os.PathLike[str]) -> str:
    try:
        return os.fspath(path)
    except TypeError:
        return str(path)


def _validate_search_options(
    keyword: str,
    recursive: bool,
    case_sensitive: bool,
    max_files: int,
    max_depth: int,
    max_matches: int,
) -> None:
    if not isinstance(keyword, str) or not keyword or "\x00" in keyword:
        raise ValueError("keyword must be non-empty text without NUL characters")
    if len(keyword) > MAX_SEARCH_KEYWORD_LENGTH:
        raise ValueError(
            f"keyword must not exceed {MAX_SEARCH_KEYWORD_LENGTH} characters"
        )
    if not isinstance(recursive, bool):
        raise TypeError("recursive must be a boolean")
    if not isinstance(case_sensitive, bool):
        raise TypeError("case_sensitive must be a boolean")
    for name, value in (
        ("max_files", max_files),
        ("max_depth", max_depth),
        ("max_matches", max_matches),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")


def _pick_search_directory(
    directory: str | os.PathLike[str] | None,
    alias: str | os.PathLike[str] | None,
) -> str | os.PathLike[str]:
    if directory is None and alias is None:
        raise ValueError("directory is required")
    if directory is not None and alias is not None:
        raise ValueError("directory and dir cannot both be provided")
    if directory is not None:
        return directory
    if alias is not None:
        return alias
    raise ValueError("directory is required")


def _find_keyword_column(line: str, keyword: str, case_sensitive: bool) -> int:
    if case_sensitive:
        return line.find(keyword)

    folded_parts: list[str] = []
    original_columns: list[int] = []
    for index, character in enumerate(line):
        folded = character.casefold()
        folded_parts.append(folded)
        original_columns.extend([index] * len(folded))
    folded_line = "".join(folded_parts)
    position = folded_line.find(keyword)
    if position < 0:
        return -1
    return original_columns[position]


def _make_snippet(line: str, column: int) -> str:
    if len(line) <= MAX_SEARCH_SNIPPET_LENGTH:
        return line

    half_window = (MAX_SEARCH_SNIPPET_LENGTH - 3) // 2
    start = max(0, column - half_window)
    end = min(len(line), start + MAX_SEARCH_SNIPPET_LENGTH - 3)
    start = max(0, end - (MAX_SEARCH_SNIPPET_LENGTH - 3))
    return ("..." if start else "") + line[start:end] + (
        "..." if end < len(line) else ""
    )


def _lexical_candidate(resolver: ManagedPathResolver, path: str | os.PathLike[str]) -> Path:
    raw_path = os.fspath(path)
    native_path = Path(raw_path.replace("\\", os.sep))
    if native_path.is_absolute():
        return native_path
    return resolver.root / native_path


def _exclusive_create(path: Path, content: bytes) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=".agentnews-write-",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary_path = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.link(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _atomic_replace(path: Path, content: bytes) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=".agentnews-write-",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary_path = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


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
        code = "not_directory" if operation in {"list", "search"} else "not_file"
        expected = "目录" if operation in {"list", "search"} else "普通文件"
        return FileToolError(code, f"路径必须指向{expected}：{path}", path=path)
    if isinstance(error, PathSecurityError):
        return FileToolError("path_error", f"路径校验失败：{path}", path=path)
    if isinstance(error, OSError):
        return FileToolError("io_error", f"访问路径时发生 I/O 错误：{path}", path=path)
    operation_name = {
        "list": "列出目录",
        "read": "读取文件",
        "search": "搜索文件内容",
        "write": "写入文件",
    }.get(operation, "处理文件")
    return FileToolError("invalid_request", f"无法{operation_name}：{path}", path=path)
