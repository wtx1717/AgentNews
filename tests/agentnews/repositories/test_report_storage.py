from datetime import date, datetime
import hashlib

import pytest

from agentnews import ReportStorage as PublicReportStorage
from agentnews.repositories.report_storage import (
    InvalidReportPathError,
    ReportConflictError,
    ReportStorage,
)


def test_report_storage_is_reexported_from_package_root():
    assert PublicReportStorage is ReportStorage


def test_save_and_read_report_creates_root_and_date_directory(tmp_path):
    root = tmp_path / "missing" / "reports"
    storage = ReportStorage(root)
    content = "# 今日 AI 新闻\n\n原样保存。\r\n"

    stored = storage.save_report(17, date(2026, 10, 13), "今日 AI 新闻", content)

    assert stored.relative_path == "2026-10-13/17-今日-ai-新闻.md"
    assert stored.content_hash == hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert (root / "2026-10-13" / "17-今日-ai-新闻.md").read_bytes() == content.encode(
        "utf-8"
    )
    assert storage.read_report(stored.relative_path) == content


def test_same_report_and_content_is_idempotent_without_rewriting(tmp_path):
    storage = ReportStorage(tmp_path / "reports")
    report_date = date(2026, 10, 13)
    first = storage.save_report(1, report_date, "Daily News", "same content")
    report_file = storage.root / first.relative_path
    original_bytes = report_file.read_bytes()
    original_mtime = report_file.stat().st_mtime_ns

    second = storage.save_report(1, report_date, "Daily News", "same content")

    assert second == first
    assert report_file.read_bytes() == original_bytes
    assert report_file.stat().st_mtime_ns == original_mtime


def test_same_report_path_with_different_content_is_not_overwritten(tmp_path):
    storage = ReportStorage(tmp_path / "reports")
    report_date = date(2026, 10, 13)
    stored = storage.save_report(1, report_date, "Daily News", "original")

    with pytest.raises(ReportConflictError):
        storage.save_report(1, report_date, "Daily News", "replacement")

    assert storage.read_report(stored.relative_path) == "original"


def test_equal_content_for_different_reports_has_distinct_paths(tmp_path):
    storage = ReportStorage(tmp_path / "reports")
    report_date = date(2026, 10, 13)

    first = storage.save_report(1, report_date, "Daily News", "same")
    second = storage.save_report(2, report_date, "Daily News", "same")

    assert first.relative_path != second.relative_path
    assert first.content_hash == second.content_hash
    assert storage.read_report(first.relative_path) == storage.read_report(
        second.relative_path
    )


@pytest.mark.parametrize(
    "relative_path",
    [
        "../2026-10-13/1-news.md",
        "2026-10-13/../../outside.md",
        "2026-10-13/1-news.md/extra",
        "2026-13-40/1-news.md",
        "2026-10-13/01-news.md",
        f"2026-10-13/1-{'a' * 81}.md",
        "C:/outside/1-news.md",
        "/outside/2026-10-13/1-news.md",
    ],
)
def test_read_rejects_noncanonical_or_absolute_paths(tmp_path, relative_path):
    storage = ReportStorage(tmp_path / "reports")

    with pytest.raises(InvalidReportPathError):
        storage.read_report(relative_path)


def test_read_missing_report_raises_file_not_found(tmp_path):
    storage = ReportStorage(tmp_path / "reports")

    with pytest.raises(FileNotFoundError):
        storage.read_report("2026-10-13/1-news.md")


def test_symlink_outside_root_is_rejected_when_supported(tmp_path):
    root = tmp_path / "reports"
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "1-news.md"
    outside_file.write_text("outside", encoding="utf-8")
    (root / "2026-10-13").parent.mkdir(parents=True)

    try:
        (root / "2026-10-13").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")

    storage = ReportStorage(root)
    with pytest.raises(InvalidReportPathError, match="escapes"):
        storage.read_report("2026-10-13/1-news.md")


@pytest.mark.parametrize("report_id", [0, -1, True, "1"])
def test_save_requires_positive_integer_report_id(tmp_path, report_id):
    storage = ReportStorage(tmp_path / "reports")

    with pytest.raises(ValueError, match="positive integer"):
        storage.save_report(report_id, date(2026, 10, 13), "News", "content")


def test_save_requires_date_not_datetime(tmp_path):
    storage = ReportStorage(tmp_path / "reports")

    with pytest.raises(TypeError, match="must be a date"):
        storage.save_report(1, datetime(2026, 10, 13), "News", "content")


def test_slug_is_bounded_and_falls_back_for_empty_title(tmp_path):
    storage = ReportStorage(tmp_path / "reports")
    report_date = date(2026, 10, 13)

    empty_title = storage.save_report(1, report_date, "!!!", "content")
    long_title = storage.save_report(2, report_date, "A" * 200, "content")

    assert empty_title.relative_path.endswith("/1-report.md")
    assert long_title.relative_path == f"2026-10-13/2-{'a' * 80}.md"
