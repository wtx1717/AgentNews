from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
from typing import Literal


DEFAULT_MAX_FILE_SIZE_BYTES = 1024 * 1024
PathKind = Literal["any", "file", "directory"]


class PathSecurityError(ValueError):
    """Raised when a path violates the managed file-access policy."""


class UnsafePathError(PathSecurityError):
    """Raised when a path is outside the managed root or otherwise unsafe."""


class SensitivePathError(PathSecurityError):
    """Raised when a path refers to a protected file or directory."""


class PathTypeError(PathSecurityError):
    """Raised when a path does not refer to an allowed filesystem object."""


class FileTooLargeError(PathSecurityError):
    """Raised when a file or text payload exceeds the configured size limit."""


class InvalidTextFileError(PathSecurityError):
    """Raised when file contents are not safe UTF-8 text."""


class ManagedPathResolver:
    """Resolve file-tool paths beneath one managed root and enforce basic policy."""

    def __init__(
        self,
        root: str | Path = Path("data"),
        *,
        max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
    ) -> None:
        if (
            isinstance(max_file_size_bytes, bool)
            or not isinstance(max_file_size_bytes, int)
            or max_file_size_bytes < 1
        ):
            raise ValueError("max_file_size_bytes must be a positive integer")

        self.root = Path(root).expanduser().resolve()
        self.max_file_size_bytes = max_file_size_bytes

    def resolve(
        self,
        path: str | os.PathLike[str],
        *,
        expected_type: PathKind = "any",
        allow_missing_leaf: bool = False,
    ) -> Path:
        """Return the resolved in-root path after validating its type and size."""
        if expected_type not in {"any", "file", "directory"}:
            raise ValueError("expected_type must be 'any', 'file', or 'directory'")

        raw_path = os.fspath(path)
        if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
            raise UnsafePathError("path must be non-empty text without NUL characters")

        posix_path = PurePosixPath(raw_path)
        windows_path = PureWindowsPath(raw_path)
        if any(part == ".." for part in (*posix_path.parts, *windows_path.parts)):
            raise UnsafePathError("parent-directory traversal is not allowed")
        if windows_path.drive and not windows_path.is_absolute():
            raise UnsafePathError("drive-relative paths are not allowed")

        native_path = Path(raw_path.replace("\\", os.sep))
        is_absolute = (
            native_path.is_absolute()
            or posix_path.is_absolute()
            or windows_path.is_absolute()
        )
        candidate = native_path if is_absolute else self.root / native_path

        self._reject_sensitive(candidate, check_lexical=True)
        resolved = candidate.resolve(strict=False)
        self._ensure_contained(resolved)
        self._reject_sensitive(resolved)

        if resolved.exists():
            file_stat = resolved.stat()
            if stat.S_ISREG(file_stat.st_mode):
                if file_stat.st_size > self.max_file_size_bytes:
                    raise FileTooLargeError("file exceeds the configured size limit")
                if expected_type == "directory":
                    raise PathTypeError("path must refer to a directory")
                return resolved
            if stat.S_ISDIR(file_stat.st_mode):
                if expected_type == "file":
                    raise PathTypeError("path must refer to a regular file")
                return resolved
            raise PathTypeError("only regular files and directories are allowed")

        if not allow_missing_leaf:
            raise FileNotFoundError(raw_path)
        if expected_type != "file":
            raise PathTypeError("only a missing file target may be allowed")
        if candidate.is_symlink():
            raise UnsafePathError("a dangling symbolic link is not a valid file target")

        parent = resolved.parent
        self._ensure_contained(parent)
        try:
            parent_stat = parent.stat()
        except FileNotFoundError:
            raise PathTypeError("parent directory must already exist") from None
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise PathTypeError("parent path must refer to a directory")
        return resolved

    def read_text(self, path: str | os.PathLike[str]) -> str:
        """Read a bounded UTF-8 text file after applying path checks."""
        target = self.resolve(path, expected_type="file")
        with target.open("rb") as file:
            content = file.read(self.max_file_size_bytes + 1)
        if len(content) > self.max_file_size_bytes:
            raise FileTooLargeError("file exceeds the configured size limit")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            raise InvalidTextFileError("file is not valid UTF-8 text") from None
        if "\x00" in text:
            raise InvalidTextFileError("NUL characters are not allowed in text files")
        return text

    def encode_text(self, content: str) -> bytes:
        """Validate and encode text content for a future bounded file write."""
        if not isinstance(content, str):
            raise InvalidTextFileError("content must be text")
        if "\x00" in content:
            raise InvalidTextFileError("NUL characters are not allowed in text files")
        try:
            encoded = content.encode("utf-8")
        except UnicodeEncodeError:
            raise InvalidTextFileError("content is not valid UTF-8 text") from None
        if len(encoded) > self.max_file_size_bytes:
            raise FileTooLargeError("content exceeds the configured size limit")
        return encoded

    def _ensure_contained(self, path: Path) -> None:
        try:
            path.relative_to(self.root)
        except ValueError:
            raise UnsafePathError("path escapes the managed root") from None

    def _reject_sensitive(self, path: Path, *, check_lexical: bool = False) -> None:
        candidate = path
        if check_lexical:
            candidate = path.absolute()
        try:
            relative = candidate.relative_to(self.root)
        except ValueError:
            return

        if any(self._is_sensitive_component(part) for part in relative.parts):
            raise SensitivePathError("sensitive paths are not accessible")

    @staticmethod
    def _is_sensitive_component(name: str) -> bool:
        normalized = name.casefold()
        if normalized.startswith(".env"):
            return True
        if normalized in {
            "id_dsa",
            "id_ecdsa",
            "id_ed25519",
            "id_ed25519_sk",
            "id_rsa",
        }:
            return True
        if normalized.endswith((".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")):
            return True
        for database_suffix in (".db", ".sqlite", ".sqlite3"):
            if normalized.endswith(database_suffix) or any(
                normalized.endswith(f"{database_suffix}-{sidecar}")
                for sidecar in ("wal", "shm", "journal")
            ):
                return True
        return False
