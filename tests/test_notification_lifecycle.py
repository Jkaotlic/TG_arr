"""TEST-05: NotificationService lifecycle coverage (start/stop idempotency,
initial sync, disappearing torrents, partial broadcast failure) plus the
OBS-03 (honest success/failure logging) and PERF-02 (adaptive, filtered
polling) behavior changes that ride along with it."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.models import TorrentInfo, TorrentState
from bot.services.notification_service import NotificationService


def _torrent(hash="a" * 40, name="Movie.2024", progress=0.5, state=TorrentState.DOWNLOADING):
    return TorrentInfo(
        hash=hash,
        name=name,
        progress=progress,
        state=state,
        added_on=datetime.now(timezone.utc),
    )


def _make_service(qbit=None, sender=None):
    qbit = qbit or AsyncMock()
    sender = sender or AsyncMock()
    return NotificationService(qbit, sender), qbit, sender


# --- start/stop lifecycle -----------------------------------------------


@pytest.mark.asyncio
async def test_double_start_is_idempotent_single_monitor_task():
    svc, qbit, _ = _make_service()
    qbit.get_torrents.return_value = []
    await svc.start()
    task1 = svc._monitor_task
    await svc.start()  # second call must be a no-op, not spawn a second task
    assert svc._monitor_task is task1
    await svc.stop()


@pytest.mark.asyncio
async def test_stop_cancels_monitor_task():
    svc, qbit, _ = _make_service()
    qbit.get_torrents.return_value = []
    await svc.start()
    task = svc._monitor_task
    await svc.stop()
    assert task.cancelled() or task.done()
    assert svc._monitor_task is None


@pytest.mark.asyncio
async def test_stop_without_start_does_not_raise():
    svc, _, _ = _make_service()
    await svc.stop()  # must be a safe no-op


# --- initial sync ----------------------------------------------------------


@pytest.mark.asyncio
async def test_initial_sync_marks_existing_torrents_notified_without_notifying():
    """Torrents already present at startup must not trigger a notification —
    only downloads that complete *during* this run should notify."""
    svc, qbit, sender = _make_service()
    qbit.get_torrents.return_value = [_torrent(progress=1.0, state=TorrentState.COMPLETED)]

    await svc._initial_sync()

    assert svc._tracked_torrents["a" * 40]["notified"] is True
    sender.assert_not_awaited()


# --- disappearing torrents ------------------------------------------------


@pytest.mark.asyncio
async def test_disappeared_torrent_removed_from_tracked():
    svc, qbit, _ = _make_service()
    svc._tracked_torrents["b" * 40] = {
        "completed": False, "notified": False, "name": "Old", "added_on": None,
    }
    qbit.get_torrents.return_value = []  # torrent is gone from qBit entirely
    qbit.get_torrent.return_value = None  # confirmed gone, not just filtered out

    await svc._check_for_completions()

    assert "b" * 40 not in svc._tracked_torrents


# --- partial broadcast failure --------------------------------------------


@pytest.mark.asyncio
async def test_notify_completion_failure_for_one_user_does_not_block_others():
    svc, qbit, sender = _make_service()
    svc.subscribe_user(1)
    svc.subscribe_user(2)

    calls = []

    async def flaky_sender(user_id, message):
        calls.append(user_id)
        if user_id == 1:
            raise RuntimeError("blocked bot")

    svc.send_notification = flaky_sender

    await svc._notify_completion(_torrent())

    assert set(calls) == {1, 2}


# --- OBS-03: send_notification wrapper is bool-returning, no false success


@pytest.mark.asyncio
async def test_notify_completion_only_logs_success_on_true(caplog):
    """The wrapper built in main.py returns bool; _notify_completion must only
    treat a user as notified when the wrapper reports True."""
    svc, qbit, _ = _make_service()
    svc.subscribe_user(1)

    async def failing_sender(user_id, message):
        return False  # simulates main.py wrapper swallowing a send error

    svc.send_notification = failing_sender

    # Must not raise, and must not silently claim success.
    await svc._notify_completion(_torrent())


# --- PERF-02: filtered polling + adaptive interval -------------------------


@pytest.mark.asyncio
async def test_check_for_completions_polls_only_downloading_filter():
    from bot.models import TorrentFilter

    svc, qbit, _ = _make_service()
    qbit.get_torrents.return_value = []

    await svc._check_for_completions()

    assert qbit.get_torrents.await_args.kwargs.get("filter_type") == TorrentFilter.DOWNLOADING


@pytest.mark.asyncio
async def test_completion_detected_when_torrent_disappears_from_downloading_filter():
    """PERF-02: instead of polling the full list, completion is detected when
    a previously-downloading torrent no longer shows up under the DOWNLOADING
    filter; get_torrent(hash) confirms it (vs. having been removed)."""
    svc, qbit, sender = _make_service()
    svc.subscribe_user(1)
    h = "c" * 40
    svc._tracked_torrents[h] = {
        "completed": False, "notified": False, "name": "Show.S01E01", "added_on": None,
    }

    qbit.get_torrents.return_value = []  # no longer downloading
    qbit.get_torrent.return_value = _torrent(hash=h, progress=1.0, state=TorrentState.COMPLETED)

    await svc._check_for_completions()

    sender.assert_awaited_once()
    # Once complete+notified there's no further state to track (it can't
    # re-enter the DOWNLOADING filter) — dropped to avoid unbounded growth.
    assert h not in svc._tracked_torrents


@pytest.mark.asyncio
async def test_get_stats_completed_counter_survives_tracked_torrent_eviction():
    """get_stats()['completed_torrents'] must reflect completions-this-run
    even though completed entries are evicted from _tracked_torrents right
    after notifying (see PERF-02 memory-growth fix)."""
    svc, qbit, sender = _make_service()
    svc.subscribe_user(1)
    h = "d" * 40
    svc._tracked_torrents[h] = {"completed": False, "notified": False, "name": "X", "added_on": None}
    qbit.get_torrents.return_value = []
    qbit.get_torrent.return_value = _torrent(hash=h, progress=1.0, state=TorrentState.COMPLETED)

    await svc._check_for_completions()

    assert svc.get_stats()["completed_torrents"] == 1
    assert svc.get_stats()["tracked_torrents"] == 0


@pytest.mark.asyncio
async def test_get_poll_interval_adapts_to_active_downloads():
    svc, qbit, _ = _make_service()

    qbit.get_torrents.return_value = [_torrent()]
    assert await svc._get_poll_interval() == svc.settings.notify_check_interval

    qbit.get_torrents.return_value = []
    assert await svc._get_poll_interval() == svc._idle_check_interval


# --- OBS-06: component contextvar + backoff on monitor loop errors --------


@pytest.mark.asyncio
async def test_monitor_loop_binds_component_contextvar():
    import structlog

    svc, qbit, _ = _make_service()
    qbit.get_torrents.return_value = []

    bound = {}
    orig_bind = structlog.contextvars.bind_contextvars

    def spy_bind(**kw):
        bound.update(kw)
        return orig_bind(**kw)

    structlog.contextvars.bind_contextvars = spy_bind
    try:
        await svc.start()
        await asyncio.sleep(0)  # let the monitor task actually start running
        await svc.stop()
    finally:
        structlog.contextvars.bind_contextvars = orig_bind

    assert bound.get("component") == "notification_service"


# --- bot/main.py: DB-04/BUG-15 startup subscription + Task F run_maintenance
# integration in _periodic_cleanup. Task F's Database.run_maintenance() is
# not implemented yet (owned by a different task) — mocked here per the
# fix-plan's interface contract.


@pytest.mark.asyncio
async def test_on_startup_subscribes_db_allowed_users():
    from bot.main import on_startup

    db = AsyncMock()
    db.cleanup_old_sessions.return_value = 0
    db.cleanup_old_searches.return_value = 0
    db.list_allowed_users.return_value = [555, 666]

    notification_service = MagicMock()
    notification_service.start = AsyncMock()

    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=MagicMock(username="testbot", id=1))

    await on_startup(bot, db, notification_service)

    subscribed = {c.args[0] for c in notification_service.subscribe_user.call_args_list}
    # env allowlist from conftest is {123456789, 987654321} ∪ admin {123456789}
    assert {555, 666}.issubset(subscribed)
    notification_service.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_periodic_cleanup_calls_run_maintenance_with_backup_every_4th_cycle():
    """Task F interface: Database.run_maintenance(backup: bool) -> dict[str,int].
    _periodic_cleanup must call it instead of the three separate cleanup_*
    methods, and pass backup=True only on the 4th cycle."""
    from bot.main import _periodic_cleanup

    db = AsyncMock()
    db.run_maintenance.return_value = {"sessions_removed": 0, "searches_removed": 0}
    logger = MagicMock()

    call_count = 0

    async def fake_sleep(_interval):
        nonlocal call_count
        call_count += 1
        if call_count > 4:
            raise asyncio.CancelledError()

    orig_sleep = asyncio.sleep
    asyncio.sleep = fake_sleep
    try:
        # _periodic_cleanup catches CancelledError internally (break) and
        # returns cleanly — it does not propagate.
        await _periodic_cleanup(db, logger)
    finally:
        asyncio.sleep = orig_sleep

    assert db.run_maintenance.call_count == 4
    backup_flags = [c.kwargs.get("backup") for c in db.run_maintenance.call_args_list]
    assert backup_flags == [False, False, False, True]
