"""RACE-04: a slow callback must not resurrect a session another callback deleted."""

import pytest

from bot.db import Database
from bot.models import ContentType, SearchResult, SearchSession, User


@pytest.mark.asyncio
async def test_update_session_does_not_resurrect_deleted_session():
    """update_session is UPDATE-only: if the row was deleted (e.g. by a concurrent
    Cancel/grab), it must NOT re-create it and must report failure."""
    db = Database(":memory:")
    await db.connect()
    await db.create_user(User(tg_id=1, username="u", first_name="f"))

    session = SearchSession(
        user_id=1, query="q", content_type=ContentType.MOVIE,
        results=[SearchResult(guid="g", title="t")],
    )
    await db.save_session(1, session)
    assert await db.get_session(1) is not None

    # Concurrent Cancel/grab deletes the session mid-flow.
    await db.delete_session(1)

    # The slow callback now tries to persist its stale copy.
    resurrected = await db.update_session(1, session)
    assert resurrected is False, "update_session must not resurrect a deleted session"
    assert await db.get_session(1) is None, "deleted session must stay deleted"

    await db.close()


@pytest.mark.asyncio
async def test_update_session_updates_existing_session():
    """When the row still exists, update_session updates it and reports success."""
    db = Database(":memory:")
    await db.connect()
    await db.create_user(User(tg_id=1, username="u", first_name="f"))

    session = SearchSession(user_id=1, query="q", content_type=ContentType.MOVIE)
    await db.save_session(1, session)

    session.current_page = 3
    ok = await db.update_session(1, session)
    assert ok is True
    got = await db.get_session(1)
    assert got is not None and got.current_page == 3

    await db.close()
