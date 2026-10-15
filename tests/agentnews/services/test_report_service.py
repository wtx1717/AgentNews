from datetime import date

import pytest

from agentnews.repositories import (
    InMemoryRepository,
    ReportStorage,
    SQLiteRepository,
    SourceArchiveStorage,
)
from agentnews.services.report import ReportDeletionError, ReportService


def _create_indexed_report(repository, settings):
    reservation = repository.reserve_report(date(2026, 10, 14), "2026-10-14 新闻日报")
    report_storage = ReportStorage(settings.report_root)
    stored_report = report_storage.save_report(
        reservation.id,
        reservation.report_date,
        reservation.title,
        "# 日报\n\n正文",
    )
    source_storage = SourceArchiveStorage(settings.source_archive_root)
    stored_source = source_storage.save_source(
        reservation.id,
        1,
        reservation.report_date,
        "来源标题",
        "https://example.com/news",
        "example.com",
        "2026-10-14T00:00:00Z",
        None,
        "来源正文",
    )
    source = repository.add_report_source(
        reservation.id,
        "来源标题",
        "https://example.com/news",
        "example.com",
        archive_file_path=stored_source.relative_path,
    )
    report = repository.finalize_report(
        reservation.id,
        stored_report.relative_path,
        "摘要",
        stored_report.content_hash,
    )
    return report, source, report_storage, source_storage


def test_delete_report_removes_index_sources_and_managed_files(tmp_path):
    settings = type(
        "Settings",
        (),
        {
            "report_root": tmp_path / "reports",
            "source_archive_root": tmp_path / "source_archive",
        },
    )()
    repository = InMemoryRepository(report_id_factory=lambda: 1)
    report, source, report_storage, source_storage = _create_indexed_report(
        repository, settings
    )

    service = ReportService(repository, report_storage, source_storage)
    assert service.delete_report(report.id)
    assert not service.delete_report(report.id)
    assert repository.get_report(report.id) is None
    assert repository.get_report_sources(report.id) == ()
    assert repository.get_source(source.id) is None
    with pytest.raises(FileNotFoundError):
        report_storage.read_report(report.file_path)
    with pytest.raises(FileNotFoundError):
        source_storage.read_source(source.archive_file_path)


def test_sqlite_delete_report_cascades_sources_and_survives_restart(tmp_path):
    settings = type(
        "Settings",
        (),
        {
            "report_root": tmp_path / "reports",
            "source_archive_root": tmp_path / "source_archive",
        },
    )()
    database_path = tmp_path / "agentnews.sqlite3"
    repository = SQLiteRepository(database_path)
    report, source, report_storage, source_storage = _create_indexed_report(
        repository, settings
    )

    assert ReportService(repository, report_storage, source_storage).delete_report(
        report.id
    )
    repository.close()

    reopened = SQLiteRepository(database_path)
    try:
        assert reopened.get_report(report.id) is None
        assert reopened.get_report_sources(report.id) == ()
        assert reopened.get_source(source.id) is None
    finally:
        reopened.close()

    assert not (tmp_path / "reports" / report.file_path).exists()
    assert not (tmp_path / "source_archive" / source.archive_file_path).exists()


def test_report_file_cleanup_failure_is_reported_after_index_deletion(tmp_path):
    settings = type(
        "Settings",
        (),
        {
            "report_root": tmp_path / "reports",
            "source_archive_root": tmp_path / "source_archive",
        },
    )()
    repository = InMemoryRepository(report_id_factory=lambda: 1)
    report, _source, report_storage, source_storage = _create_indexed_report(
        repository, settings
    )
    report_storage.delete_report = lambda _path: (_ for _ in ()).throw(OSError("denied"))

    with pytest.raises(ReportDeletionError, match="文件清理失败"):
        ReportService(repository, report_storage, source_storage).delete_report(report.id)
    assert repository.get_report(report.id) is None
    assert repository.get_report_sources(report.id) == ()
