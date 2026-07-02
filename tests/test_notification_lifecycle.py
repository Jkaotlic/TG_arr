"""TEST-05: NotificationService lifecycle coverage (start/stop idempotency,
initial sync, disappearing torrents, partial broadcast failure) plus the
OBS-03 (honest success/failure logging) and PERF-02 full (sync/maindata
delta-protocol, adaptive polling) behavior changes that ride along with it."""

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


def _raw(name="Movie.2024", progress=0.5, state="downloading", **extra):
    """Raw per-torrent dict as it appears inside maindata's "torrents" map
    (qBittorrent's native field names — state is the raw string, not the
    normalized TorrentState enum)."""
    return {"name": name, "progress": progress, "state": state, **extra}


def _maindata(rid=1, torrents=None, removed=None, full_update=False):
    """Build a get_maindata()-shaped response (already normalized the way
    QBittorrentClient.get_maindata returns it)."""
    return {
        "rid": rid,
        "torrents": torrents or {},
        "torrents_removed": removed or [],
        "full_update": full_update,
    }


def _make_service(qbit=None, sender=None):
    qbit = qbit or AsyncMock()
    sender = sender or AsyncMock()
    return NotificationService(qbit, sender), qbit, sender


# --- start/stop lifecycle -----------------------------------------------


@pytest.mark.asyncio
async def test_double_start_is_idempotent_single_monitor_task():
    svc, qbit, _ = _make_service()
    qbit.get_maindata.return_value = _maindata(full_update=True)
    await svc.start()
    task1 = svc._monitor_task
    await svc.start()  # second call must be a no-op, not spawn a second task
    assert svc._monitor_task is task1
    await svc.stop()


@pytest.mark.asyncio
async def test_stop_cancels_monitor_task():
    svc, qbit, _ = _make_service()
    qbit.get_maindata.return_value = _maindata(full_update=True)
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
    qbit.get_maindata.return_value = _maindata(
        full_update=True,
        torrents={"a" * 40: _raw(progress=1.0, state="uploading")},
    )

    await svc._initial_sync()

    assert svc._tracked_torrents["a" * 40]["notified"] is True
    assert svc._tracked_torrents["a" * 40]["completed"] is True
    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_initial_sync_forces_rid_zero_and_full_snapshot():
    """_initial_sync must always start from rid=0 (a full snapshot), even if
    the service somehow already had a non-zero rid (e.g. re-init)."""
    svc, qbit, _ = _make_service()
    svc._rid = 99
    qbit.get_maindata.return_value = _maindata(rid=5, full_update=True, torrents={})

    await svc._initial_sync()

    qbit.get_maindata.assert_awaited_once_with(0)
    assert svc._rid == 5


# --- disappearing torrents ------------------------------------------------


@pytest.mark.asyncio
async def test_disappeared_torrent_removed_from_tracked():
    svc, qbit, _ = _make_service()
    svc._tracked_torrents["b" * 40] = {
        "completed": False, "notified": False, "name": "Old", "added_on": None,
    }
    svc._torrents_raw["b" * 40] = _raw(name="Old", progress=0.5, state="downloading")
    # torrent is gone from qBit entirely -> reported via torrents_removed
    qbit.get_maindata.return_value = _maindata(removed=["b" * 40])

    await svc._check_for_completions()

    assert "b" * 40 not in svc._tracked_torrents
    assert "b" * 40 not in svc._torrents_raw


# --- PERF-02 full: _merge_maindata (delta merge + full_update rebuild) ----


def test_merge_maindata_partial_update_does_not_lose_prior_fields():
    """A delta only carries the fields that changed — merging it must update
    those fields in place while preserving everything else already known
    about that hash (e.g. name, size) instead of clobbering the row."""
    svc, _, _ = _make_service()
    h = "e" * 40
    svc._torrents_raw[h] = {
        "name": "Movie.2024", "progress": 0.4, "state": "downloading",
        "size": 5_000_000_000, "dlspeed": 100,
    }

    svc._merge_maindata({
        "rid": 2,
        "torrents": {h: {"progress": 0.6, "dlspeed": 200}},
        "torrents_removed": [],
        "full_update": False,
    })

    row = svc._torrents_raw[h]
    assert row["progress"] == 0.6
    assert row["dlspeed"] == 200
    # Untouched fields survive the partial merge.
    assert row["name"] == "Movie.2024"
    assert row["size"] == 5_000_000_000
    assert row["state"] == "downloading"
    assert svc._rid == 2


def test_merge_maindata_partial_update_adds_new_hash():
    """A delta can introduce a brand-new hash (torrent just added) — merge
    must create it rather than requiring it to pre-exist."""
    svc, _, _ = _make_service()

    svc._merge_maindata({
        "rid": 1,
        "torrents": {"f" * 40: {"name": "New", "progress": 0.0, "state": "downloading"}},
        "torrents_removed": [],
        "full_update": False,
    })

    assert svc._torrents_raw["f" * 40]["name"] == "New"


def test_merge_maindata_removed_hash_dropped_from_mirror():
    svc, _, _ = _make_service()
    h = "g" * 40
    svc._torrents_raw[h] = {"name": "Gone", "progress": 0.5, "state": "downloading"}

    svc._merge_maindata({
        "rid": 3, "torrents": {}, "torrents_removed": [h], "full_update": False,
    })

    assert h not in svc._torrents_raw


def test_merge_maindata_full_update_replaces_mirror_outright():
    """full_update=True means the payload is a complete snapshot — stale
    hashes that are no longer present (and weren't explicitly listed in
    torrents_removed) must not survive the rebuild."""
    svc, _, _ = _make_service()
    svc._torrents_raw["stale" + "0" * 36] = {
        "name": "Stale", "progress": 1.0, "state": "uploading",
    }

    svc._merge_maindata({
        "rid": 9,
        "torrents": {"h" * 40: {"name": "Fresh", "progress": 0.1, "state": "downloading"}},
        "torrents_removed": [],
        "full_update": True,
    })

    assert "stale" + "0" * 36 not in svc._torrents_raw
    assert svc._torrents_raw["h" * 40]["name"] == "Fresh"
    assert svc._rid == 9


@pytest.mark.asyncio
async def test_full_update_resync_reevaluates_every_torrent_for_completion():
    """End-to-end: a full_update mid-run (e.g. qBit dropped our rid after a
    WebUI restart) must fully resync the mirror and re-evaluate every
    torrent it contains, not just the ones that "changed" relative to the
    now-discarded previous state."""
    svc, qbit, sender = _make_service()
    svc.subscribe_user(1)
    h = "i" * 40
    # Not previously tracked at all (simulates rid reset losing our history).
    qbit.get_maindata.return_value = _maindata(
        rid=100,
        full_update=True,
        torrents={h: _raw(name="Resynced", progress=1.0, state="uploading")},
    )

    await svc._check_for_completions()

    # First sight of an already-complete torrent outside of _initial_sync is
    # suppressed (consistent with the initial-sync no-spurious-notify rule),
    # but it must be tracked as completed so it doesn't linger.
    assert h not in svc._tracked_torrents or svc._tracked_torrents[h]["completed"] is True
    sender.assert_not_awaited()
    assert svc._rid == 100


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


# --- PERF-02 full: sync/maindata delta polling + adaptive interval --------


@pytest.mark.asyncio
async def test_check_for_completions_polls_maindata_with_current_rid():
    svc, qbit, _ = _make_service()
    svc._rid = 7
    qbit.get_maindata.return_value = _maindata(rid=8)

    await svc._check_for_completions()

    qbit.get_maindata.assert_awaited_once_with(7)
    assert svc._rid == 8


@pytest.mark.asyncio
async def test_completion_detected_when_torrent_flips_to_completed_in_delta():
    """PERF-02 full: completion is detected when a delta update for a
    previously-tracked hash reports progress=1.0 / a completed state."""
    svc, qbit, sender = _make_service()
    svc.subscribe_user(1)
    h = "c" * 40
    svc._tracked_torrents[h] = {
        "completed": False, "notified": False, "name": "Show.S01E01", "added_on": None,
    }
    svc._torrents_raw[h] = _raw(name="Show.S01E01", progress=0.9, state="downloading")

    qbit.get_maindata.return_value = _maindata(
        torrents={h: {"progress": 1.0, "state": "uploading"}},
    )

    await svc._check_for_completions()

    sender.assert_awaited_once()
    # Once complete+notified there's no further state to track — dropped to
    # avoid unbounded growth.
    assert h not in svc._tracked_torrents
    # The raw mirror keeps the torrent (it's still seeding in qBit); only
    # notification tracking is dropped.
    assert svc._torrents_raw[h]["progress"] == 1.0


@pytest.mark.asyncio
async def test_get_stats_completed_counter_survives_tracked_torrent_eviction():
    """get_stats()['completed_torrents'] must reflect completions-this-run
    even though completed entries are evicted from _tracked_torrents right
    after notifying (see PERF-02 memory-growth fix)."""
    svc, qbit, sender = _make_service()
    svc.subscribe_user(1)
    h = "d" * 40
    svc._tracked_torrents[h] = {"completed": False, "notified": False, "name": "X", "added_on": None}
    svc._torrents_raw[h] = _raw(name="X", progress=0.5, state="downloading")
    qbit.get_maindata.return_value = _maindata(
        torrents={h: {"progress": 1.0, "state": "uploading"}},
    )

    await svc._check_for_completions()

    assert svc.get_stats()["completed_torrents"] == 1
    assert svc.get_stats()["tracked_torrents"] == 0


@pytest.mark.asyncio
async def test_get_poll_interval_adapts_to_active_downloads():
    svc, qbit, _ = _make_service()

    svc._torrents_raw["a" * 40] = _raw(state="downloading")
    assert await svc._get_poll_interval() == svc.settings.notify_check_interval

    svc._torrents_raw.clear()
    assert await svc._get_poll_interval() == svc._idle_check_interval


@pytest.mark.asyncio
async def test_get_poll_interval_uses_local_view_without_extra_api_call():
    """The adaptive interval must not perform its own qBittorrent request —
    it should be derivable from the maindata mirror already populated by
    _check_for_completions this cycle."""
    svc, qbit, _ = _make_service()
    svc._torrents_raw["a" * 40] = _raw(state="downloading")

    await svc._get_poll_interval()

    qbit.get_torrents.assert_not_called()
    qbit.get_maindata.assert_not_called()


# --- OBS-06: component contextvar + backoff on monitor loop errors --------


@pytest.mark.asyncio
async def test_monitor_loop_binds_component_contextvar():
    import structlog

    svc, qbit, _ = _make_service()
    qbit.get_maindata.return_value = _maindata(full_update=True)

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
