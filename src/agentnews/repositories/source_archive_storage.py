from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import tempfile
import unicodedata
from urllib.parse import urlsplit

from .report_storage import ReportConflictError


@dataclass(frozen=True, slots=True)
class StoredSourceArchive:
    relative_path: str
    content_hash: str
    created: bool = field(default=False, compare=False)


class InvalidSourceArchivePathError(ValueError):
    """Raised when an archive path is not canonical or leaves its root."""


class SourceArchiveStorage:
    """Store fetched source text as conflict-safe Markdown files."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def save_source(
        self,
        report_id: int,
        source_index: int,
        source_date: date,
        title: str,
        url: str,
        source_site: str,
        fetched_at: str,
        published_at: str | None,
        body: str,
    ) -> StoredSourceArchive:
        if isinstance(report_id, bool) or not isinstance(report_id, int) or report_id < 1:
            raise ValueError("report_id must be a positive integer")
        if isinstance(source_index, bool) or not isinstance(source_index, int) or source_index < 1:
            raise ValueError("source_index must be a positive integer")
        if not isinstance(source_date, date) or isinstance(source_date, datetime):
            raise TypeError("source_date must be a date")
        values = (title, url, source_site, fetched_at, body)
        if not all(isinstance(value, str) for value in values):
            raise TypeError("source archive fields must be strings")
        if published_at is not None and not isinstance(published_at, str):
            raise TypeError("published_at must be a string or None")

        display_title = _one_line(title) or "未知来源"
        parsed_url = urlsplit(url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.hostname
            or any(character.isspace() or ord(character) < 32 for character in url)
            or "<" in url
            or ">" in url
        ):
            raise ValueError("source URL must be a valid HTTP or HTTPS URL")
        source_site = _one_line(source_site) or "未知"
        fetched_at = _one_line(fetched_at) or "未知"
        published_at = _one_line(published_at or "") or "未知"
        content = (
            f"# {display_title}\n\n"
            f"- 原始 URL：<{url}>\n"
            f"- 来源站点：{source_site}\n"
            f"- 抓取时间：{fetched_at}\n"
            f"- 原文发布时间：{published_at}\n\n"
            "## 正文\n\n"
            f"{body}"
        )
        relative_path = (
            f"{source_date.isoformat()}/{report_id}-{source_index}-"
            f"{self._slugify(display_title)}.md"
        )
        target = self._resolve_path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        content_bytes = content.encode("utf-8")
        content_hash = hashlib.sha256(content_bytes).hexdigest()
        if target.exists() or target.is_symlink():
            self._check_existing(target, content_hash)
            return StoredSourceArchive(relative_path, content_hash)

        temporary_path: Path | None = None
        created = False
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=target.parent, prefix=f".{target.name}.", suffix=".tmp", delete=False
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(content_bytes)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            try:
                os.link(temporary_path, target)
                created = True
            except FileExistsError:
                self._check_existing(target, content_hash)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return StoredSourceArchive(relative_path, content_hash, created)

    def discard_created_source(self, source: StoredSourceArchive) -> bool:
        """Remove a source file created by this save, if its bytes are unchanged."""
        if not source.created:
            return False
        target = self._resolve_path(source.relative_path)
        self._ensure_contained(target)
        if not target.is_file():
            return False
        actual_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual_hash != source.content_hash:
            return False
        target.unlink()
        return True

    def delete_source(self, relative_path: str | Path) -> bool:
        """Delete a source archive under the configured root if it exists."""
        target = self._resolve_path(relative_path)
        if not target.exists() and not target.is_symlink():
            return False
        if not target.is_file():
            raise InvalidSourceArchivePathError(
                "archive path does not refer to a regular file"
            )
        target.unlink()
        return True

    def read_source(self, relative_path: str | Path) -> str:
        target = self._resolve_path(relative_path)
        return target.read_bytes().decode("utf-8")

    def _resolve_path(self, relative_path: str | Path) -> Path:
        raw = os.fspath(relative_path)
        if not isinstance(raw, str):
            raise InvalidSourceArchivePathError("archive path must be text")
        normalized = raw.replace("\\", "/")
        parts = normalized.split("/")
        posix = PurePosixPath(normalized)
        windows = PureWindowsPath(normalized)
        if (
            not normalized or len(parts) != 2 or posix.is_absolute() or windows.is_absolute()
            or windows.drive or any(part in {"", ".", ".."} for part in parts)
        ):
            raise InvalidSourceArchivePathError("archive path must be canonical")
        try:
            parsed_date = date.fromisoformat(parts[0])
        except ValueError:
            raise InvalidSourceArchivePathError("archive path contains an invalid date") from None
        if parsed_date.isoformat() != parts[0] or not parts[1].endswith(".md"):
            raise InvalidSourceArchivePathError("archive path is not canonical")
        target = self.root.joinpath(*parts)
        self._ensure_contained(target)
        return target

    def _ensure_contained(self, path: Path) -> None:
        try:
            path.resolve().relative_to(self.root)
        except ValueError:
            raise InvalidSourceArchivePathError("archive path escapes the configured root") from None

    @staticmethod
    def _slugify(title: str) -> str:
        normalized = unicodedata.normalize("NFC", title).lower()
        result = "".join(character if character.isalnum() else "-" for character in normalized)
        result = "-".join(part for part in result.split("-") if part)[:80].rstrip("-")
        return result or "source"

    def _check_existing(self, target: Path, expected_hash: str) -> None:
        self._ensure_contained(target)
        if not target.is_file():
            raise InvalidSourceArchivePathError("archive path does not refer to a regular file")
        actual_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            raise ReportConflictError("source archive path already exists with different content")


def _one_line(value: str) -> str:
    return " ".join(value.split())
