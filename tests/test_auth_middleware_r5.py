"""TEST-10 (r5): AuthMiddleware fail-closed + RateLimitMiddleware coverage.

Covers findings from analysis/r5/08-testing-quality.md (TEST-10):
  (a) is_allowed_in_db raising -> deny (fail-closed), not fail-open
  (b) event with no from_user -> None, handler not called
  (c) 31st request within the rate-limit window -> rejected

Also exercises the PERF-09/BUG-17b cleanup fix in
bot/middleware/auth.py::RateLimitMiddleware (stale-entry eviction keyed off
the newest recorded timestamp instead of the always-false `not reqs`).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message

from bot.middleware.auth import MAX_REQUESTS_PER_MINUTE, AuthMiddleware, RateLimitMiddleware

UNKNOWN_ID = 555000111


def _make_message(user_id: int | None) -> Message:
    """A Message mock that passes isinstance(event, Message)."""
    event = MagicMock(spec=Message)
    if user_id is None:
        event.from_user = None
    else:
        tg_user = MagicMock()
        tg_user.id = user_id
        tg_user.username = "tester"
        tg_user.first_name = "Test"
        event.from_user = tg_user
    event.answer = AsyncMock()
    return event


# ---------------------------------------------------------------------------
# (a) fail-closed: DB exception during the runtime-allowlist check must deny,
#     not silently allow.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_db_allowlist_exception_denies_access_fail_closed():
    """An unknown (non-env-allowed) user whose DB allowlist check raises must
    be denied — never fail-open just because the DB had a hiccup.
    """
    db = AsyncMock()
    db.is_allowed_in_db = AsyncMock(side_effect=RuntimeError("db unavailable"))
    db.get_user = AsyncMock()
    db.create_user = AsyncMock()

    mw = AuthMiddleware(db)
    handler = AsyncMock(return_value="SHOULD_NOT_RUN")
    data: dict = {}
    event = _make_message(UNKNOWN_ID)

    result = await mw(handler, event, data)

    assert result is None
    handler.assert_not_awaited()
    event.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_db_allowlist_exception_denies_callback_fail_closed():
    """Same fail-closed guarantee on the CallbackQuery path."""
    db = AsyncMock()
    db.is_allowed_in_db = AsyncMock(side_effect=RuntimeError("db unavailable"))

    mw = AuthMiddleware(db)
    handler = AsyncMock(return_value="SHOULD_NOT_RUN")
    data: dict = {}

    event = MagicMock(spec=CallbackQuery)
    tg_user = MagicMock()
    tg_user.id = UNKNOWN_ID
    tg_user.username = "intruder"
    event.from_user = tg_user
    event.answer = AsyncMock()

    result = await mw(handler, event, data)

    assert result is None
    handler.assert_not_awaited()
    event.answer.assert_awaited_once_with("Доступ запрещён", show_alert=True)


# ---------------------------------------------------------------------------
# (b) event without from_user -> None, handler never called.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_message_without_from_user_returns_none():
    db = AsyncMock()
    mw = AuthMiddleware(db)
    handler = AsyncMock(return_value="SHOULD_NOT_RUN")
    data: dict = {}
    event = _make_message(None)

    result = await mw(handler, event, data)

    assert result is None
    handler.assert_not_awaited()
    # No user to identify -> no rejection message can be sent either.
    event.answer.assert_not_called()


@pytest.mark.asyncio
async def test_callback_without_from_user_returns_none():
    db = AsyncMock()
    mw = AuthMiddleware(db)
    handler = AsyncMock(return_value="SHOULD_NOT_RUN")
    data: dict = {}

    event = MagicMock(spec=CallbackQuery)
    event.from_user = None
    event.answer = AsyncMock()

    result = await mw(handler, event, data)

    assert result is None
    handler.assert_not_awaited()
    event.answer.assert_not_called()


# ---------------------------------------------------------------------------
# (c) RateLimitMiddleware: 31st request in the window is rejected.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_31st_request_in_window_is_rejected():
    mw = RateLimitMiddleware()
    handler = AsyncMock(return_value="OK")
    user_id = 42

    assert MAX_REQUESTS_PER_MINUTE == 30

    for _ in range(MAX_REQUESTS_PER_MINUTE):
        event = _make_message(user_id)
        result = await mw(handler, event, {})
        assert result == "OK"

    handler.reset_mock()
    event = _make_message(user_id)
    result = await mw(handler, event, {})

    assert result is None
    handler.assert_not_awaited()
    event.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_within_limit_is_allowed():
    mw = RateLimitMiddleware()
    handler = AsyncMock(return_value="OK")
    event = _make_message(43)

    result = await mw(handler, event, {})

    assert result == "OK"
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_rate_limit_is_per_user():
    """One user hitting the limit must not affect another user."""
    mw = RateLimitMiddleware()
    handler = AsyncMock(return_value="OK")

    for _ in range(MAX_REQUESTS_PER_MINUTE):
        await mw(handler, _make_message(100), {})

    handler.reset_mock()
    result = await mw(handler, _make_message(200), {})

    assert result == "OK"
    handler.assert_awaited_once()


# ---------------------------------------------------------------------------
# PERF-09/BUG-17b: stale-entry cleanup keyed off the newest timestamp.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cleanup_evicts_users_whose_newest_request_expired():
    mw = RateLimitMiddleware(max_requests=30, window_seconds=60)
    handler = AsyncMock(return_value="OK")

    # Seed > 1000 users with a timestamp well outside the window so the
    # cleanup threshold triggers and the old-condition bug (`not reqs`,
    # which can never be true here) would leave everything in place.
    stale_ts = 0.0  # time.time() epoch start — always < any real window_start
    for uid in range(1001):
        mw._user_requests[uid] = [stale_ts]

    event = _make_message(99999)
    result = await mw(handler, event, {})

    assert result == "OK"
    # Every seeded stale entry must have been evicted; only the just-recorded
    # request for 99999 remains.
    assert 0 not in mw._user_requests
    assert 1000 not in mw._user_requests
    assert 99999 in mw._user_requests


@pytest.mark.asyncio
async def test_cleanup_keeps_users_with_recent_requests():
    import time

    mw = RateLimitMiddleware(max_requests=30, window_seconds=60)
    handler = AsyncMock(return_value="OK")

    now = time.time()
    for uid in range(1001):
        mw._user_requests[uid] = [now]

    event = _make_message(99999)
    await mw(handler, event, {})

    # Recent entries must survive the cleanup pass.
    assert 0 in mw._user_requests
    assert 1000 in mw._user_requests
