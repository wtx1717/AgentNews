from __future__ import annotations

import pytest

from agentnews.repositories import (
    InMemoryRepository,
    SQLiteRepository,
    SessionArchivedError,
)


@pytest.fixture(params=["memory", "sqlite"])
def repository(request, tmp_path):
    if request.param == "memory":
        yield InMemoryRepository()
        return
    value = SQLiteRepository(tmp_path / "agentnews.sqlite3")
    try:
        yield value
    finally:
        value.close()


def test_session_title_lifecycle_and_rename(repository):
    session = repository.create_session()
    assert session.status == "active"
    assert session.archived_at is None

    first = repository.append_message(
        session.id,
        "user",
        "  第一行\n\n第二行\t带有多余空白  ",
    )
    current = repository.get_session(session.id)
    assert first.role == "user"
    assert current is not None
    assert current.title == "第一行 第二行 带有多余空白"

    repository.append_message(session.id, "assistant", "回答")
    assert repository.get_session(session.id).title == current.title

    renamed = repository.rename_session(session.id, "  手动标题  ")
    assert renamed.title == "手动标题"
    repository.append_message(session.id, "user", "后续消息不应覆盖标题")
    assert repository.get_session(session.id).title == "手动标题"


def test_first_user_title_is_limited_to_80_unicode_characters(repository):
    session = repository.create_session()
    repository.append_message(session.id, "user", "字" * 81)

    assert repository.get_session(session.id).title == "字" * 80


def test_session_listing_lifecycle_and_pagination(repository):
    first = repository.create_session("first")
    second = repository.create_session("second")
    assert [item.id for item in repository.list_sessions()] == [second.id, first.id]
    assert repository.list_sessions(limit=1, offset=1) == (first,)

    assert repository.archive_session(first.id)
    archived = repository.get_session(first.id)
    assert archived.status == "archived"
    assert archived.archived_at is not None
    assert repository.list_sessions() == (second,)
    assert repository.list_sessions(status="archived") == (archived,)
    assert not repository.archive_session(first.id)

    with pytest.raises(SessionArchivedError):
        repository.append_message(first.id, "user", "不能追加")
    assert repository.list_messages(first.id) == ()

    assert repository.restore_session(first.id)
    restored = repository.get_session(first.id)
    assert restored.status == "active"
    assert restored.archived_at is None
    assert not repository.restore_session(first.id)


def test_delete_session_cascades_messages(repository):
    session = repository.create_session()
    repository.append_message(session.id, "user", "消息")
    assert repository.delete_session(session.id)
    assert repository.get_session(session.id) is None
    with pytest.raises(KeyError):
        repository.list_messages(session.id)
    assert not repository.delete_session(session.id)


def test_session_inputs_are_validated(repository):
    session = repository.create_session()
    with pytest.raises(ValueError):
        repository.rename_session(session.id, "  ")
    with pytest.raises(TypeError):
        repository.rename_session(session.id, None)
    with pytest.raises(ValueError):
        repository.list_sessions(status="unknown")
    with pytest.raises(ValueError):
        repository.list_sessions(limit=0)
    with pytest.raises(ValueError):
        repository.list_sessions(offset=-1)
    assert repository.get_session("missing") is None
    assert not repository.archive_session("missing")
    assert not repository.restore_session("missing")
    assert not repository.delete_session("missing")

