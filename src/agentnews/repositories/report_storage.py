"""报告文件的落盘存储。

按「日期/报告ID-slug.md」结构保存 Markdown 报告，并做路径校验、原子写入与内容冲突检测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import tempfile
import unicodedata


class ReportConflictError(FileExistsError):
    """Raised when a report path already contains different content."""


class InvalidReportPathError(ValueError):
    """Raised when a report path is not a canonical path inside the report root."""


@dataclass(frozen=True)
class StoredReport:
    """已落盘报告的相对路径、内容哈希，以及本次是否新建。"""
    relative_path: str
    content_hash: str
    created: bool = field(default=False, compare=False)


class ReportStorage:
    """Store Markdown reports under a single configured root directory."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def save_report(
        self,
        report_id: int,
        report_date: date,
        title: str,
        content: str,
    ) -> StoredReport:
        """保存报告 Markdown；内容一致的重复保存视为成功且不重复写入。"""
        if isinstance(report_id, bool) or not isinstance(report_id, int) or report_id < 1:
            raise ValueError("report_id must be a positive integer")
        if not isinstance(report_date, date) or isinstance(report_date, datetime):
            raise TypeError("report_date must be a date")
        if not isinstance(title, str):
            raise TypeError("title must be a string")
        if not isinstance(content, str):
            raise TypeError("content must be a string")

        relative_path = (
            f"{report_date.isoformat()}/{report_id}-{self._slugify(title)}.md"
        )
        content_bytes = content.encode("utf-8")
        content_hash = hashlib.sha256(content_bytes).hexdigest()
        target = self._resolve_report_path(relative_path)

        self._ensure_contained(target.parent)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_contained(target)
        # 同时判断 is_symlink，避免悬空软链绕过存在性检查。
        if target.exists() or target.is_symlink():
            self._check_existing_report(target, content_hash)
            return StoredReport(relative_path, content_hash)

        temporary_path: Path | None = None
        created = False
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(content_bytes)
                temporary_file.flush()
                # 先落盘再发布，避免崩溃后留下内容为空的正式文件。
                os.fsync(temporary_file.fileno())

            try:
                # 用硬链接原子发布：目标已存在会抛错，避免并发写互相覆盖。
                os.link(temporary_path, target)
                created = True
            except FileExistsError:
                self._check_existing_report(target, content_hash)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

        return StoredReport(relative_path, content_hash, created)

    def discard_created_report(self, report: StoredReport) -> bool:
        """Remove a report created by this save, but only if its bytes are unchanged."""
        if not report.created:
            return False
        target = self._resolve_report_path(report.relative_path)
        self._ensure_contained(target)
        if not target.is_file():
            return False
        actual_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual_hash != report.content_hash:
            return False
        target.unlink()
        return True

    def delete_report(self, relative_path: str | Path) -> bool:
        """Delete a report file under the configured root if it exists."""
        target = self._resolve_report_path(relative_path)
        if not target.exists() and not target.is_symlink():
            return False
        if not target.is_file():
            raise InvalidReportPathError("report path does not refer to a regular file")
        target.unlink()
        return True

    def read_report(self, relative_path: str | Path) -> str:
        """读取根目录下指定相对路径的报告文本。"""
        target = self._resolve_report_path(relative_path)
        self._ensure_contained(target)
        return target.read_bytes().decode("utf-8")

    @staticmethod
    def _slugify(title: str) -> str:
        """把标题转成可安全用作文件名的 slug。"""
        normalized = unicodedata.normalize("NFC", title).lower()
        slug: list[str] = []
        pending_separator = False

        for character in normalized:
            category = unicodedata.category(character)
            # 组合字符须紧跟主体字符，否则重音等标记会与词干分离。
            if category.startswith("M"):
                if slug and not pending_separator and slug[-1] != "-":
                    slug.append(character)
                continue

            if character.isalnum():
                if pending_separator and slug:
                    slug.append("-")
                slug.append(character)
                pending_separator = False
            else:
                pending_separator = True

        result = "".join(slug).strip("-")[:80].rstrip("-")
        return result or "report"

    def _resolve_report_path(self, relative_path: str | Path) -> Path:
        raw_path = os.fspath(relative_path)
        if not isinstance(raw_path, str):
            raise InvalidReportPathError("report path must be text")

        # 统一分隔符，并同时按 POSIX 与 Windows 规则解析，防止跨平台路径绕过校验。
        normalized = raw_path.replace("\\", "/")
        posix_path = PurePosixPath(normalized)
        windows_path = PureWindowsPath(normalized)
        parts = normalized.split("/")
        if (
            not normalized
            or posix_path.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or len(parts) != 2
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise InvalidReportPathError("report path must be a canonical relative path")

        date_part, filename = parts
        try:
            parsed_date = date.fromisoformat(date_part)
        except ValueError:
            raise InvalidReportPathError("report path contains an invalid date") from None
        # 要求严格的 YYYY-MM-DD，拒绝 2024-1-1 之类非规范写法导致重复目录。
        if parsed_date.isoformat() != date_part:
            raise InvalidReportPathError("report path date must use YYYY-MM-DD format")

        if not filename.endswith(".md"):
            raise InvalidReportPathError("report filename is not canonical")
        filename_stem = filename[:-3]
        report_id, separator, slug = filename_stem.partition("-")
        # 文件名须为「报告ID-slug」，且 ID 不允许前导零，保证路径唯一可解析。
        if (
            not separator
            or not report_id.isascii()
            or not report_id.isdigit()
            or report_id.startswith("0")
            or not self._valid_slug(slug)
        ):
            raise InvalidReportPathError("report filename is not canonical")

        target = self.root.joinpath(*parts)
        self._ensure_contained(target)
        return target

    @staticmethod
    def _valid_slug(slug: str) -> bool:
        if (
            not slug
            or len(slug) > 80
            or slug.startswith("-")
            or slug.endswith("-")
            or "--" in slug
        ):
            return False
        return all(
            character.isalnum()
            or unicodedata.category(character).startswith("M")
            or character == "-"
            for character in slug
        )

    def _ensure_contained(self, path: Path) -> None:
        # 先 resolve 再比较，防止 .. 或符号链接把路径带出根目录。
        resolved = path.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise InvalidReportPathError("report path escapes the configured root") from None

    def _check_existing_report(self, target: Path, expected_hash: str) -> None:
        self._ensure_contained(target)
        if not target.is_file():
            raise InvalidReportPathError("report path does not refer to a regular file")

        existing_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        if existing_hash != expected_hash:
            raise ReportConflictError(
                "report path already exists with different content"
            )
