"""Music download notifications: one summary per album, not one per track.

Audit 2026-09-23, NOTIF-01/02/03/05: 185 of 188 notifications in ten days
were about single slskd files (peak 59 messages/hour). The watcher now keeps
per-transfer state in SQLite, aggregates by batch (album) and sends one
summary once the batch has settled.

These tests drive the real LibraryWatcher against a real (temp) Database and
a fake slskd client, with a controllable clock.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from bot.clients.base import ServiceConnectionError
from bot.db import Database
from bot.models import SlskdTransfer
from bot.services import music_notify
from bot.services.library_watcher import LibraryWatcher

T0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
ALBUM_DIR = "music_readonly\\Artist - Album (2023) [FLAC]"
BATCH = "934fac87-0000-0000-0000-000000000001"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class FakeSlskd:
    def __init__(self) -> None:
        self.transfers: list[SlskdTransfer] = []
        self.fail = False

    async def get_transfers(self, *, include_completed: bool = False) -> list[SlskdTransfer]:
        if self.fail:
            raise ServiceConnectionError("Не удалось подключиться к slskd")
        return list(self.transfers)


class _NoHistory:
    async def get_history(self, **_kwargs):
        return []


def _t(n: int, state: str, *, ended: Optional[datetime] = None, directory: str = ALBUM_DIR,
       batch_id: str = BATCH, tid: Optional[str] = None, username: str = "peer1") -> SlskdTransfer:
    name = f"{n:02d} - Track {n}.flac"
    return SlskdTransfer(
        username=username,
        filename=name,
        state=state,
        size=100,
        transferred=100 if "Succeeded" in state else 0,
        id=tid or f"{directory}-{n}",
        batch_id=batch_id,
        directory=directory,
        full_filename=f"{directory}\\{name}",
        ended_at=ended if ended is not None else (T0 if "Completed" in state else None),
    )


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "bot.db"))
    await database.connect()
    yield database
    await database.close()


def _watcher(db: Database, slskd: FakeSlskd, clock: Clock, sent: list[str],
             poll_interval: float = 3600) -> LibraryWatcher:
    """Most tests jump the clock between sparse polls; they declare a long
    poll interval so those jumps are not read as unobserved time. Tests of
    the unobserved-gap rule pass the real 60 s."""
    async def notify(text: str, category: str = "library") -> None:
        sent.append(text)

    async def get_slskd():
        return slskd

    return LibraryWatcher(
        _NoHistory(), _NoHistory(), notify=notify, get_slskd=get_slskd, db=db,
        clock=clock, music_settle_seconds=300, music_stall_seconds=3600,
        poll_interval_seconds=poll_interval,
    )


async def _poll_for(watcher: LibraryWatcher, clock: Clock, seconds: int) -> None:
    """Poll every 60 s like production does."""
    for _ in range(seconds // 60):
        clock.advance(60)
        await watcher.poll()


async def _prime(watcher: LibraryWatcher, clock: Clock) -> None:
    """First-ever poll against an empty slskd: sets the watermark."""
    await watcher.poll()
    clock.advance(60)


# ------------------------------------------------------------------ pure bits
def test_album_title_is_normalised_from_the_directory():
    assert music_notify.album_title(ALBUM_DIR) == "Artist — Album (2023)"
    assert music_notify.album_title("x\\Artist - Album (2023) [FLAC]\\CD2") == "Artist — Album (2023)"
    assert music_notify.album_title("") == "Музыка"


def test_best_state_wins_for_a_track():
    assert music_notify.track_outcome(["Completed, Errored", "Completed, Succeeded"]) == "succeeded"
    assert music_notify.track_outcome(["Completed, Rejected", "Queued, Remotely"]) == "active"
    assert music_notify.track_outcome(["Completed, Cancelled", "Completed, TimedOut"]) == "failed"
    assert music_notify.track_outcome(["Completed, Cancelled"]) == "cancelled"


# --------------------------------------------------------------- aggregation
@pytest.mark.asyncio
async def test_twelve_tracks_trickling_in_give_one_message_after_settle(db):
    slskd, clock, sent = FakeSlskd(), Clock(), []
    watcher = _watcher(db, slskd, clock, sent)
    await _prime(watcher, clock)

    slskd.transfers = [_t(n, "Queued, Remotely") for n in range(1, 13)]
    await watcher.poll()
    for n in range(1, 13):
        clock.advance(60)
        slskd.transfers[n - 1] = _t(n, "Completed, Succeeded", ended=clock.now)
        await watcher.poll()
    assert sent == []  # still settling

    clock.advance(301)
    await watcher.poll()
    clock.advance(60)
    await watcher.poll()

    assert len(sent) == 1
    assert "Artist — Album (2023)" in sent[0]
    assert "12/12 скачано" in sent[0]


@pytest.mark.asyncio
async def test_error_then_success_for_the_same_track_reads_as_downloaded(db):
    slskd, clock, sent = FakeSlskd(), Clock(), []
    watcher = _watcher(db, slskd, clock, sent)
    await _prime(watcher, clock)

    slskd.transfers = [_t(n, "Completed, Succeeded", ended=clock.now) for n in range(1, 12)]
    slskd.transfers.append(_t(12, "Completed, Errored", ended=clock.now))
    await watcher.poll()
    clock.advance(120)
    # slskd retries the file as a new transfer in the same batch
    slskd.transfers.append(_t(12, "Completed, Succeeded", ended=clock.now, tid="retry-12"))
    await watcher.poll()
    clock.advance(301)
    await watcher.poll()

    assert len(sent) == 1
    assert "12/12 скачано" in sent[0]
    assert "не удалось" not in sent[0]
    assert "Errored" not in sent[0]


@pytest.mark.asyncio
async def test_two_directories_with_the_same_basenames_do_not_repeat(db):
    slskd, clock, sent = FakeSlskd(), Clock(), []
    watcher = _watcher(db, slskd, clock, sent)
    await _prime(watcher, clock)

    cd1, cd2 = ALBUM_DIR + "\\CD1", ALBUM_DIR + "\\CD2"
    slskd.transfers = (
        [_t(n, "Completed, Succeeded", ended=clock.now, directory=cd1) for n in range(1, 4)]
        + [_t(n, "Completed, Succeeded", ended=clock.now, directory=cd2) for n in range(1, 4)]
    )
    for _ in range(5):
        await watcher.poll()
        clock.advance(301)

    assert len(sent) == 1
    assert "6/6 скачано" in sent[0]


@pytest.mark.asyncio
async def test_restart_mid_batch_sends_a_single_message(db):
    slskd, clock, sent = FakeSlskd(), Clock(), []
    watcher = _watcher(db, slskd, clock, sent)
    await _prime(watcher, clock)

    slskd.transfers = [_t(n, "Completed, Succeeded", ended=clock.now) for n in range(1, 6)]
    slskd.transfers += [_t(n, "InProgress") for n in range(6, 11)]
    await watcher.poll()
    clock.advance(60)

    # process restart: fresh watcher, same database
    restarted = _watcher(db, slskd, clock, sent)
    await restarted.poll()
    slskd.transfers = [_t(n, "Completed, Succeeded", ended=clock.now) for n in range(1, 11)]
    await restarted.poll()
    clock.advance(301)
    await restarted.poll()
    clock.advance(60)
    await restarted.poll()

    assert len(sent) == 1
    assert "10/10 скачано" in sent[0]


@pytest.mark.asyncio
async def test_an_all_cancelled_batch_is_silent(db):
    slskd, clock, sent = FakeSlskd(), Clock(), []
    watcher = _watcher(db, slskd, clock, sent)
    await _prime(watcher, clock)

    slskd.transfers = [_t(n, "Completed, Cancelled", ended=clock.now) for n in range(1, 5)]
    await watcher.poll()
    clock.advance(301)
    await watcher.poll()
    clock.advance(3601)
    await watcher.poll()

    assert sent == []


@pytest.mark.asyncio
async def test_rejected_and_timed_out_count_as_failures(db):
    slskd, clock, sent = FakeSlskd(), Clock(), []
    watcher = _watcher(db, slskd, clock, sent)
    await _prime(watcher, clock)

    slskd.transfers = [_t(n, "Completed, Succeeded", ended=clock.now) for n in range(1, 11)]
    slskd.transfers.append(_t(11, "Completed, Rejected", ended=clock.now))
    slskd.transfers.append(_t(12, "Completed, TimedOut", ended=clock.now))
    await watcher.poll()
    clock.advance(301)
    await watcher.poll()

    assert len(sent) == 1
    assert "10/12 скачано, 2 не удалось" in sent[0]
    assert "11 - Track 11.flac" in sent[0]
    assert "12 - Track 12.flac" in sent[0]


@pytest.mark.asyncio
async def test_first_ever_run_with_existing_history_is_silent(db):
    slskd, clock, sent = FakeSlskd(), Clock(), []
    slskd.transfers = [
        _t(n, "Completed, Succeeded", ended=T0 - timedelta(days=3)) for n in range(1, 13)
    ] + [_t(n, "Completed, Rejected", ended=T0 - timedelta(days=2), directory="x\\Other - LP")
         for n in range(1, 4)]
    watcher = _watcher(db, slskd, clock, sent)

    for _ in range(3):
        await watcher.poll()
        clock.advance(3601)

    assert sent == []


@pytest.mark.asyncio
async def test_a_stalled_batch_gets_a_partial_summary_once(db):
    slskd, clock, sent = FakeSlskd(), Clock(), []
    watcher = _watcher(db, slskd, clock, sent)
    await _prime(watcher, clock)

    slskd.transfers = [_t(n, "Completed, Succeeded", ended=clock.now) for n in range(1, 11)]
    slskd.transfers += [_t(11, "Queued, Remotely"), _t(12, "Queued, Remotely")]
    await watcher.poll()
    clock.advance(301)
    await watcher.poll()
    assert sent == []  # queued tracks remain: not settled yet

    clock.advance(3600)
    await watcher.poll()
    clock.advance(3600)
    await watcher.poll()

    assert len(sent) == 1
    assert "10/12 скачано" in sent[0]
    assert "ещё 2 в очереди" in sent[0]


@pytest.mark.asyncio
async def test_several_ready_batches_share_one_message(db):
    slskd, clock, sent = FakeSlskd(), Clock(), []
    watcher = _watcher(db, slskd, clock, sent)
    await _prime(watcher, clock)

    other = "music\\Band - Record (1999) [MP3]"
    slskd.transfers = [_t(n, "Completed, Succeeded", ended=clock.now) for n in range(1, 4)]
    slskd.transfers += [_t(n, "Completed, Succeeded", ended=clock.now, directory=other,
                           batch_id="batch-2") for n in range(1, 3)]
    await watcher.poll()
    clock.advance(301)
    await watcher.poll()

    assert len(sent) == 1
    assert sent[0].startswith("🎵 Музыка:")
    assert "Artist — Album (2023)" in sent[0] and "Band — Record (1999)" in sent[0]


@pytest.mark.asyncio
async def test_slskd_outage_does_not_move_the_watermark(db):
    """A failed poll must not let later history slip in as news, nor lose it."""
    slskd, clock, sent = FakeSlskd(), Clock(), []
    watcher = _watcher(db, slskd, clock, sent)
    await _prime(watcher, clock)

    slskd.fail = True
    await watcher.poll()
    clock.advance(600)
    slskd.fail = False
    # a transfer that finished during the outage is still news
    slskd.transfers = [_t(1, "Completed, Succeeded", ended=clock.now - timedelta(seconds=300))]
    await watcher.poll()
    clock.advance(301)
    await watcher.poll()

    assert len(sent) == 1
    assert "1/1 скачано" in sent[0]


@pytest.mark.asyncio
async def test_titles_are_escaped(db):
    slskd, clock, sent = FakeSlskd(), Clock(), []
    watcher = _watcher(db, slskd, clock, sent)
    await _prime(watcher, clock)

    slskd.transfers = [_t(1, "Completed, Succeeded", ended=clock.now, directory="x\\Tom & Jerry <OST>")]
    await watcher.poll()
    clock.advance(301)
    await watcher.poll()

    assert "&lt;OST&gt;" in sent[0] and "&amp;" in sent[0]


# ------------------------------------------------------------------- formatting
def test_failure_names_are_capped_at_three():
    summary = music_notify.BatchSummary(
        title="A — B", succeeded=5, failed_names=["1.flac", "2.flac", "3.flac", "4.flac", "5.flac"],
        active=0,
    )
    line = music_notify.format_batch_line(summary)
    assert "5/10 скачано, 5 не удалось (1.flac, 2.flac, 3.flac и ещё 2)" in line


def test_the_message_is_capped():
    summaries = [
        music_notify.BatchSummary(title=f"Artist {i} — " + "x" * 200, succeeded=1, failed_names=[], active=0)
        for i in range(60)
    ]
    text = music_notify.format_message(summaries)
    assert text is not None
    assert len(text) <= 4000


# ------------------------------------------------------------------ maintenance
@pytest.mark.asyncio
async def test_maintenance_drops_batches_notified_over_a_week_ago(db):
    slskd, clock, sent = FakeSlskd(), Clock(), []
    watcher = _watcher(db, slskd, clock, sent)
    await _prime(watcher, clock)
    slskd.transfers = [_t(1, "Completed, Succeeded", ended=clock.now)]
    await watcher.poll()
    clock.advance(301)
    await watcher.poll()
    assert len(sent) == 1

    old = (T0 - timedelta(days=8)).isoformat()
    async with db._write_lock:
        await db.conn.execute("UPDATE music_batch SET notified_at = ?", (old,))
        await db.conn.commit()

    result = await db.run_maintenance(backup=False)

    assert result["music_batches"] == 1
    async with db.conn.execute("SELECT COUNT(*) FROM music_transfer") as cur:
        assert (await cur.fetchone())[0] == 0


# -------------------------------------------- review round 1 (unobserved time)
@pytest.mark.asyncio
async def test_slskd_down_for_hours_mid_album_does_not_send_a_stalled_summary(db):
    """Hours slskd was unreachable are not "quiet time" — nobody watched them."""
    slskd, clock, sent = FakeSlskd(), Clock(), []
    watcher = _watcher(db, slskd, clock, sent, poll_interval=60)
    await _prime(watcher, clock)
    slskd.transfers = [_t(n, "Completed, Succeeded", ended=clock.now) for n in range(1, 4)]
    slskd.transfers += [_t(n, "InProgress") for n in range(4, 13)]
    await watcher.poll()

    slskd.fail = True
    for _ in range(5 * 60):
        clock.advance(60)
        await watcher.poll()
    slskd.fail = False
    await watcher.poll()  # wake
    assert sent == []

    clock.advance(60)
    slskd.transfers = [_t(n, "Completed, Succeeded", ended=clock.now) for n in range(1, 13)]
    await watcher.poll()
    await _poll_for(watcher, clock, 360)

    assert len(sent) == 1
    assert "12/12 скачано" in sent[0]


@pytest.mark.asyncio
async def test_bot_down_for_hours_mid_album_does_not_send_a_stalled_summary(db):
    slskd, clock, sent = FakeSlskd(), Clock(), []
    watcher = _watcher(db, slskd, clock, sent, poll_interval=60)
    await _prime(watcher, clock)
    slskd.transfers = [_t(n, "Completed, Succeeded", ended=clock.now) for n in range(1, 4)]
    slskd.transfers += [_t(n, "InProgress") for n in range(4, 13)]
    await watcher.poll()

    clock.advance(5 * 3600)  # the bot itself was down
    restarted = _watcher(db, slskd, clock, sent, poll_interval=60)
    await restarted.poll()
    await _poll_for(restarted, clock, 600)  # still downloading, observed

    assert sent == []


@pytest.mark.asyncio
async def test_first_ever_run_with_a_stuck_remote_queue_is_silent(db):
    """The live instance has 16 'Queued, Remotely' transfers that never move."""
    slskd, clock, sent = FakeSlskd(), Clock(), []
    slskd.transfers = [
        _t(n, "Completed, Succeeded", ended=T0 - timedelta(days=3)) for n in range(1, 11)
    ] + [_t(11, "Queued, Remotely"), _t(12, "Queued, Remotely")]
    watcher = _watcher(db, slskd, clock, sent)

    for _ in range(3):
        await watcher.poll()
        clock.advance(3601)

    assert sent == []


@pytest.mark.asyncio
async def test_maintenance_keeps_a_batch_that_still_has_live_transfers(db):
    """Deleting it would re-announce the same stuck queue every week."""
    slskd, clock, sent = FakeSlskd(), Clock(), []
    watcher = _watcher(db, slskd, clock, sent)
    await _prime(watcher, clock)
    slskd.transfers = [_t(1, "Queued, Remotely")]
    await watcher.poll()
    clock.advance(3601)
    await watcher.poll()
    assert len(sent) == 1  # the stalled summary, once

    async with db._write_lock:
        await db.conn.execute(
            "UPDATE music_batch SET notified_at = ?", ((T0 - timedelta(days=9)).isoformat(),)
        )
        await db.conn.commit()
    assert await db.cleanup_music_notify() == 0

    await watcher.poll()
    clock.advance(3601)
    await watcher.poll()
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_a_small_clock_skew_between_slskd_and_the_bot_is_tolerated(db):
    """slskd's host clock slightly behind the bot's: a transfer that finished
    between two polls may carry an `endedAt` just before the watermark."""
    slskd, clock, sent = FakeSlskd(), Clock(), []
    watcher = _watcher(db, slskd, clock, sent)
    await _prime(watcher, clock)  # watermark = T0

    slskd.transfers = [_t(1, "Completed, Succeeded", ended=T0 - timedelta(seconds=60))]
    await watcher.poll()
    clock.advance(301)
    await watcher.poll()

    assert len(sent) == 1
    assert "1/1 скачано" in sent[0]
