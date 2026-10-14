from datetime import date

import pytest

from agentnews.repositories import InMemoryRepository, UserPreferences


def _repository():
    return InMemoryRepository(report_id_factory=lambda: 1)


def _finish(repository, report_date, title, path):
    reservation = repository.reserve_report(report_date, title)
    return repository.finalize_report(
        reservation.id,
        file_path=path,
        summary=f"摘要 {title}",
        content_hash=f"hash-{reservation.id}",
    )


def test_preferences_have_defaults_and_save_normalized_immutable_values():
    repository = _repository()
    topics = [" AI "]
    directly_constructed = UserPreferences(topics=topics)
    topics.append("changed")

    assert repository.get_preferences() == UserPreferences()
    assert directly_constructed.topics == ("AI",)
    saved = repository.save_preferences(
        UserPreferences(
            display_name=" 用户 ",
            email=" user@example.com ",
            timezone=" Asia/Shanghai ",
            topics=[" AI ", "AI", ""],
            keywords=[" Agent "],
        )
    )

    assert saved == UserPreferences(
        display_name="用户",
        email="user@example.com",
        timezone="Asia/Shanghai",
        topics=("AI",),
        keywords=("Agent",),
    )
    assert repository.get_preferences() == saved


def test_sessions_messages_preserve_append_order_and_detach_metadata():
    repository = _repository()
    session = repository.create_session("  Chat  ")
    metadata = {"tool": {"arguments": ["query"]}}

    first = repository.append_message(session.id, "user", "Hello", metadata)
    metadata["tool"]["arguments"].append("changed")
    second = repository.append_message(session.id, "assistant", "Hi")

    assert session.title == "Chat"
    assert first.id < second.id
    assert repository.list_messages(session.id) == (first, second)
    assert first.metadata["tool"]["arguments"] == ("query",)
    with pytest.raises(TypeError):
        first.metadata["tool"]["new"] = "value"


def test_unknown_sessions_are_rejected():
    repository = _repository()

    with pytest.raises(KeyError):
        repository.append_message("missing", "user", "Hello")
    with pytest.raises(KeyError):
        repository.list_messages("missing")


def test_reserved_reports_are_hidden_until_finalized_and_ids_are_not_reused():
    repository = _repository()
    reservation = repository.reserve_report(date(2026, 10, 14), "Daily News")

    assert reservation.id == 1
    assert repository.get_report(reservation.id) is None
    assert repository.list_reports() == ()
    assert repository.cancel_report(reservation.id)

    next_report = repository.reserve_report(date(2026, 10, 14), "Daily News")
    assert next_report.id == 2
    report = repository.finalize_report(
        next_report.id,
        file_path="2026-10-14/2-news.md",
        summary="Daily summary",
        content_hash="abc123",
    )
    assert repository.get_report(2) == report
    assert repository.list_reports() == (report,)
    assert not repository.cancel_report(2)


def test_report_sources_are_ordered_deduplicated_and_removed_on_cancel():
    repository = _repository()
    first = repository.reserve_report(date(2026, 10, 14), "One")
    source = repository.add_report_source(
        first.id,
        title="Source",
        url="https://example.com/news",
        source_name="Example",
        published_at="2026-10-14",
        snippet="Summary",
        archive_file_path="2026-10-14/1-1-source.md",
    )
    duplicate = repository.add_report_source(
        first.id,
        title="Changed title",
        url="https://example.com/news",
        source_name="Other",
    )

    assert duplicate == source
    assert repository.get_report_sources(first.id) == ()
    repository.finalize_report(
        first.id,
        file_path="2026-10-14/1-one.md",
        summary="One",
        content_hash="hash-1",
    )
    assert repository.get_report_sources(first.id) == (source,)

    pending = repository.reserve_report(date(2026, 10, 14), "Pending")
    repository.add_report_source(
        pending.id,
        title="Pending source",
        url="https://example.com/pending",
        source_name="Example",
    )
    assert repository.cancel_report(pending.id)
    assert repository.get_report_sources(pending.id) == ()
    assert repository.get_report(pending.id) is None


def test_report_listing_filters_dates_orders_newest_first_and_paginates():
    repository = _repository()
    old = _finish(repository, date(2026, 10, 12), "Old", "old.md")
    first_today = _finish(repository, date(2026, 10, 14), "First", "first.md")
    second_today = _finish(repository, date(2026, 10, 14), "Second", "second.md")

    assert repository.list_reports() == (second_today, first_today, old)
    assert repository.list_reports(
        start_date=date(2026, 10, 14), end_date=date(2026, 10, 14)
    ) == (second_today, first_today)
    assert repository.list_reports(limit=1, offset=1) == (first_today,)
    assert repository.get_report(999) is None


def test_report_reservation_and_pagination_validate_inputs():
    repository = _repository()

    with pytest.raises(TypeError):
        repository.reserve_report("2026-10-14", "Invalid date")
    with pytest.raises(ValueError):
        repository.reserve_report(date(2026, 10, 14), " ")
    with pytest.raises(ValueError):
        repository.list_reports(limit=0)
    with pytest.raises(ValueError):
        repository.list_reports(offset=-1)
    with pytest.raises(ValueError):
        repository.list_reports(
            start_date=date(2026, 10, 15), end_date=date(2026, 10, 14)
        )


def test_default_report_ids_are_unique_across_repository_instances():
    first = InMemoryRepository().reserve_report(date(2026, 10, 14), "First")
    second = InMemoryRepository().reserve_report(date(2026, 10, 14), "Second")

    assert second.id > first.id
