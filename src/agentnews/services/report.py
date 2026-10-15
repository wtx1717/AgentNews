from __future__ import annotations

from ..repositories import (
    InvalidReportPathError,
    InvalidSourceArchivePathError,
    ReportStorage,
    RepositoryProtocol,
    SourceArchiveStorage,
)


class ReportDeletionError(RuntimeError):
    """The report index was removed but one or more files could not be cleaned."""


class ReportService:
    """Coordinate report index deletion with managed report file cleanup."""

    def __init__(
        self,
        repository: RepositoryProtocol,
        report_storage: ReportStorage,
        source_archive_storage: SourceArchiveStorage,
    ) -> None:
        self.repository = repository
        self.report_storage = report_storage
        self.source_archive_storage = source_archive_storage

    def delete_report(self, report_id: int) -> bool:
        report = self.repository.get_report(report_id)
        if report is None:
            return False
        sources = self.repository.get_report_sources(report_id)
        if not self.repository.delete_report(report_id):
            return False

        cleanup_errors: list[BaseException] = []
        self._delete_report_file(report.file_path, cleanup_errors)
        for source in sources:
            if source.archive_file_path:
                self._delete_source_file(source.archive_file_path, cleanup_errors)
        if cleanup_errors:
            raise ReportDeletionError("日报索引已删除，但文件清理失败") from cleanup_errors[0]
        return True

    def _delete_report_file(self, path: str, errors: list[BaseException]) -> None:
        try:
            self.report_storage.delete_report(path)
        except (OSError, UnicodeError, InvalidReportPathError) as error:
            errors.append(error)

    def _delete_source_file(self, path: str, errors: list[BaseException]) -> None:
        try:
            self.source_archive_storage.delete_source(path)
        except (OSError, UnicodeError, InvalidSourceArchivePathError) as error:
            errors.append(error)
