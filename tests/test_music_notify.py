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


# ------------------------------- live shape: slskd gives each FILE a batchId
# Measured 2026-09-24 on the live instance: one album directory with 17 files
# enqueued together carried 17 different batchIds (10 files -> 10 ids, 7 -> 7).
# Grouping by batchId turned every track into its own "album" with its own
# settle timer, i.e. the per-track spam came back. The album folder is the key.
def _per_file(n: int, state: str, **kwargs) -> SlskdTransfer:
    directory = kwargs.get("directory", ALBUM_DIR)
    return _t(n, state, batch_id=f"batch-{directory}-{n}", **kwargs)


def test_batch_key_is_the_album_folder_not_the_batch_id():
    a = _t(1, "Queued, Remotely", batch_id="b-1")
    b = _t(2, "Queued, Remotely", batch_id="b-2", username="peer2",
           directory="@@share\\Music/Artist - Album (2023) [MP3 320]\\")
    c = _t(3, "Queued, Remotely", batch_id="b-3", directory=ALBUM_DIR.upper() + "\\Disc 2")
    assert music_notify.batch_key(a) == music_notify.batch_key(b) == music_notify.batch_key(c)
    for disc in ("CD1", "cd 2", "Disc 1", "Disk2", "DVD1", "диск 3"):
        assert music_notify.album_title(f"x\\Band - LP\\{disc}") == "Band — LP"
        d = _t(1, "Queued, Remotely", directory=f"x\\Band - LP\\{disc}")
        assert music_notify.batch_key(d) == music_notify.batch_key(
            _t(1, "Queued, Remotely", directory="y/Band - LP/"))
    assert music_notify.batch_key(a) != music_notify.batch_key(
        _t(1, "Queued, Remotely", directory="x\\Band - LP"))


@pytest.mark.asyncio
async def test_live_shape_per_file_batch_ids_give_one_message(db):
    slskd, clock, sent = FakeSlskd(), Clock(), []
    watcher = _watcher(db, slskd, clock, sent, poll_interval=60)
    await _prime(watcher, clock)

    slskd.transfers = [_per_file(n, "Queued, Remotely") for n in range(1, 13)]
    await watcher.poll()
    # the files finish at uneven times over ~10 minutes
    for n in range(1, 13):
        clock.advance(50)
        slskd.transfers[n - 1] = _per_file(n, "Completed, Succeeded", ended=clock.now)
        await watcher.poll()
    assert sent == []

    await _poll_for(watcher, clock, 600)

    assert len(sent) == 1
    assert sent[0].startswith("🎵 <b>Artist — Album (2023)</b>")
    assert "12/12 скачано" in sent[0]


@pytest.mark.asyncio
async def test_disc_folders_with_per_file_batch_ids_give_one_summary(db):
    slskd, clock, sent = FakeSlskd(), Clock(), []
    watcher = _watcher(db, slskd, clock, sent)
    await _prime(watcher, clock)

    slskd.transfers = (
        [_per_file(n, "Completed, Succeeded", ended=clock.now, directory=ALBUM_DIR + "\\CD1")
         for n in range(1, 5)]
        + [_per_file(n, "Completed, Succeeded", ended=clock.now, directory=ALBUM_DIR + "\\CD2")
           for n in range(1, 4)]
    )
    await watcher.poll()
    clock.advance(301)
    await watcher.poll()

    assert len(sent) == 1
    assert "7/7 скачано" in sent[0]


@pytest.mark.asyncio
async def test_same_album_from_two_peers_gives_one_summary(db):
    """Lidarr switches peers: peer1 fails a track, peer2 delivers it."""
    slskd, clock, sent = FakeSlskd(), Clock(), []
    watcher = _watcher(db, slskd, clock, sent)
    await _prime(watcher, clock)

    other_dir = "@@share\\Music\\Artist - Album (2023) [MP3 320]"
    slskd.transfers = [_per_file(n, "Completed, Succeeded", ended=clock.now) for n in range(1, 6)]
    slskd.transfers.append(_per_file(6, "Completed, Rejected", ended=clock.now))
    await watcher.poll()
    clock.advance(120)
    slskd.transfers.append(_per_file(6, "Completed, Succeeded", ended=clock.now,
                                     directory=other_dir, username="peer2"))
    await watcher.poll()
    clock.advance(301)
    await watcher.poll()

    assert len(sent) == 1
    assert "6/6 скачано" in sent[0]
    assert "не удалось" not in sent[0]


@pytest.mark.asyncio
async def test_two_albums_with_per_file_batch_ids_share_one_message(db):
    slskd, clock, sent = FakeSlskd(), Clock(), []
    watcher = _watcher(db, slskd, clock, sent)
    await _prime(watcher, clock)

    other = "music\\Band - Record (1999) [MP3]"
    slskd.transfers = [_per_file(n, "Completed, Succeeded", ended=clock.now) for n in range(1, 4)]
    slskd.transfers += [_per_file(n, "Completed, Succeeded", ended=clock.now, directory=other)
                        for n in range(1, 3)]
    await watcher.poll()
    clock.advance(301)
    await watcher.poll()

    assert len(sent) == 1
    assert sent[0].count("\n") == 2  # header + exactly two album lines
    assert "Artist — Album (2023)</b>: 3/3" in sent[0]
    assert "Band — Record (1999)</b>: 2/2" in sent[0]


@pytest.mark.asyncio
async def test_a_new_grab_of_an_announced_album_is_announced_again_once(db):
    """Keying by folder must not swallow a later re-grab of the same album."""
    slskd, clock, sent = FakeSlskd(), Clock(), []
    watcher = _watcher(db, slskd, clock, sent)
    await _prime(watcher, clock)

    slskd.transfers = [_per_file(n, "Completed, Succeeded", ended=clock.now) for n in range(1, 3)]
    slskd.transfers.append(_per_file(3, "Completed, Errored", ended=clock.now))
    await watcher.poll()
    clock.advance(301)
    await watcher.poll()
    assert len(sent) == 1 and "2/3 скачано, 1 не удалось" in sent[0]

    clock.advance(600)
    slskd.transfers.append(_per_file(3, "Completed, Succeeded", ended=clock.now,
                                     directory="@@share\\Artist - Album (2023)", username="peer2"))
    for _ in range(3):
        await watcher.poll()
        clock.advance(301)

    assert len(sent) == 2
    assert "3/3 скачано" in sent[1]


# ------------------------------------ re-key of v5 rows (per-file batch keys)
async def _write_v5_rows(db: Database, rows: list[tuple[str, str, str, str, Optional[str]]]) -> None:
    """Rows as the v5 code wrote them: one batch per file, keyed by batchId.

    ``(transfer_id, batch_key, track_key, state, notified_at)``.
    """
    stamp = (T0 - timedelta(minutes=1)).isoformat()  # last seen by the v5 bot
    title = music_notify.album_title(ALBUM_DIR)
    async with db._write_lock:
        for tid, bkey, tkey, state, notified in rows:
            await db.conn.execute(
                "INSERT INTO music_transfer (transfer_id, batch_key, track_key, state, updated_at) "
                "VALUES (?, ?, ?, ?, ?)", (tid, bkey, tkey, state, stamp))
            await db.conn.execute(
                "INSERT INTO music_batch (batch_key, title, first_seen_at, last_change_at, notified_at) "
                "VALUES (?, ?, ?, ?, ?)", (bkey, title, stamp, stamp, notified))
        await db.conn.execute("PRAGMA user_version = 5")
        await db.conn.commit()


async def _reopen_as_v5(tmp_path, rows) -> Database:
    path = str(tmp_path / "v5.db")
    seed = Database(path)
    await seed.connect()
    await seed.kv_set("slskd_watermark", (T0 - timedelta(minutes=1)).isoformat())
    await _write_v5_rows(seed, rows)
    await seed.close()
    database = Database(path)
    await database.connect()
    return database


def _v5_row(n: int, state: str, notified: Optional[str]) -> tuple[str, str, str, str, Optional[str]]:
    t = _per_file(n, state)
    return (music_notify.transfer_id(t), t.batch_id, f"{t.directory}\\{t.filename}", state, notified)


@pytest.mark.asyncio
async def test_v5_seeded_per_file_batches_are_rekeyed_and_stay_silent(tmp_path):
    """The live DB: 16 per-file batches of one album, seeded as announced at
    first run, all still 'Queued, Remotely'. Re-keyed into one album batch that
    inherits the silence; when the queue finally moves: never per-track."""
    seeded = (T0 - timedelta(hours=2)).isoformat()
    database = await _reopen_as_v5(
        tmp_path, [_v5_row(n, "Queued, Remotely", seeded) for n in range(1, 17)])
    try:
        async with database.conn.execute("SELECT DISTINCT batch_key FROM music_transfer") as cur:
            keys = [r[0] for r in await cur.fetchall()]
        assert keys == [music_notify.batch_key(_t(1, "Queued, Remotely"))]
        async with database.conn.execute("SELECT COUNT(*), MIN(notified_at) FROM music_batch") as cur:
            count, notified = await cur.fetchone()
        assert (count, notified) == (1, seeded)

        slskd, clock, sent = FakeSlskd(), Clock(), []
        slskd.transfers = [_per_file(n, "Queued, Remotely") for n in range(1, 17)]
        watcher = _watcher(database, slskd, clock, sent, poll_interval=60)
        await watcher.poll()
        for n in range(1, 17):
            clock.advance(40)
            slskd.transfers[n - 1] = _per_file(n, "Completed, Succeeded", ended=clock.now)
            await watcher.poll()
        await _poll_for(watcher, clock, 3 * 3600)

        assert len(sent) <= 1
        assert not any("1/1" in text for text in sent)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_v5_mixed_per_file_batches_give_exactly_one_album_summary(tmp_path):
    """Some per-file batches already announced, others pending: one summary."""
    announced = (T0 - timedelta(hours=1)).isoformat()
    rows = [_v5_row(n, "Completed, Succeeded", announced) for n in range(1, 4)]
    rows += [_v5_row(n, "InProgress", None) for n in range(4, 9)]
    database = await _reopen_as_v5(tmp_path, rows)
    try:
        slskd, clock, sent = FakeSlskd(), Clock(), []
        slskd.transfers = [_per_file(n, "Completed, Succeeded", ended=T0 - timedelta(hours=1))
                           for n in range(1, 4)]
        slskd.transfers += [_per_file(n, "InProgress") for n in range(4, 9)]
        watcher = _watcher(database, slskd, clock, sent, poll_interval=60)
        await watcher.poll()
        for n in range(4, 9):
            clock.advance(60)
            slskd.transfers[n - 1] = _per_file(n, "Completed, Succeeded", ended=clock.now)
            await watcher.poll()
        await _poll_for(watcher, clock, 3 * 3600)

        assert len(sent) == 1
        assert "8/8 скачано" in sent[0]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_v6_rekey_is_idempotent(tmp_path):
    seeded = (T0 - timedelta(hours=2)).isoformat()
    database = await _reopen_as_v5(
        tmp_path, [_v5_row(n, "Queued, Remotely", seeded) for n in range(1, 4)])
    try:
        assert await database._get_schema_version() >= 6
        await database._migrate_to_v6()  # re-run on migrated data
        async with database.conn.execute("SELECT batch_key, notified_at FROM music_batch") as cur:
            rows = [tuple(r) for r in await cur.fetchall()]
        assert rows == [(music_notify.batch_key(_t(1, "Queued, Remotely")), seeded)]
        async with database.conn.execute("SELECT COUNT(*) FROM music_transfer") as cur:
            assert (await cur.fetchone())[0] == 3
    finally:
        await database.close()


# ------------------------------------------------ review fix round 1 (keys)
def _file(name: str, state: str, *, directory: str = ALBUM_DIR, username: str = "peer1",
          tid: Optional[str] = None, ended: Optional[datetime] = None) -> SlskdTransfer:
    return SlskdTransfer(
        username=username, filename=name, state=state, size=100,
        transferred=100 if "Succeeded" in state else 0,
        id=tid or f"{username}:{directory}\\{name}", batch_id=f"b-{username}-{name}",
        directory=directory, full_filename=f"{directory}\\{name}",
        ended_at=ended if ended is not None else (T0 if "Completed" in state else None),
    )


def test_generic_album_names_keep_the_artist_folder():
    key = music_notify.album_key
    assert key("x\\Artist\\Greatest Hits") != key("y\\Other\\Greatest Hits")
    assert key("x\\Artist\\Greatest Hits") == key("@@share\\Music\\Artist\\Greatest Hits [FLAC]")
    assert key("x\\Artist\\Live\\CD2") == key("y/Artist/Live/")
    assert music_notify.album_title("x\\Artist\\Greatest Hits") == "Artist — Greatest Hits"
    # an album folder already naming its artist does not take the parent in
    assert music_notify.album_title("x\\Artist\\Artist - LP (2001)") == "Artist — LP (2001)"


def test_loose_files_in_a_share_root_are_never_one_album_across_peers():
    key = music_notify.album_key
    for root in ("music_readonly", "Music", "@@abcde", "@@share\\Music", "share", "Downloads",
                 "complete", ""):
        assert key(root, "peer1") != key(root, "peer2"), root
        assert key(root, "peer1").startswith(music_notify.ALBUM_KEY_PREFIX)
        assert music_notify.album_title(root) == "Музыка"
    assert key("music", "peer1") != key("x\\Artist - LP", "peer1")


@pytest.mark.asyncio
async def test_loose_files_of_two_peers_get_separate_lines(db):
    slskd, clock, sent = FakeSlskd(), Clock(), []
    watcher = _watcher(db, slskd, clock, sent)
    await _prime(watcher, clock)
    slskd.transfers = [
        _file("a.flac", "Completed, Succeeded", directory="music_readonly", ended=clock.now),
        _file("b.flac", "Completed, Succeeded", directory="music_readonly", username="peer2",
              ended=clock.now),
    ]
    await watcher.poll()
    clock.advance(301)
    await watcher.poll()

    assert len(sent) == 1
    assert sent[0].count("1/1 скачано") == 2


def test_dashes_and_format_tags_are_normalised_in_the_key():
    key = music_notify.album_key
    assert key("x\\Artist – Album (2023) (FLAC)") == key("y\\Artist - Album (2023) [FLAC]")
    assert key("x\\Artist — Album (2023) {24bit}") == key(ALBUM_DIR)
    assert key("x\\Artist - Album (2023) (MP3 320)") == key(ALBUM_DIR)
    assert key("x\\Artist - Album (2023) (WEB FLAC 24-96)") == key(ALBUM_DIR)
    assert key("x\\Artist - Album (2024)") != key(ALBUM_DIR)  # the year is not a tag
    assert music_notify.album_title("x\\Artist – Album (2023) (FLAC)") == "Artist — Album (2023)"


def test_disc_folder_variants_collapse_into_the_album():
    for disc in ("Disc 1 - Bonus", "CD.1", "CD-1", "Disc 1 of 2", "cd_2"):
        assert music_notify.album_key(f"{ALBUM_DIR}\\{disc}") == music_notify.album_key(ALBUM_DIR), disc
        assert music_notify.album_title(f"{ALBUM_DIR}\\{disc}") == "Artist — Album (2023)"


def test_track_identity_ignores_extension_punctuation_and_layout():
    tk = music_notify.album_track_key

    def ident(directory: str, name: str) -> str:
        return music_notify.track_identity(tk(directory, name))

    same = [
        (ALBUM_DIR, "01 - Track 1.flac"),
        (ALBUM_DIR, "01 - Track 1.mp3"),
        (ALBUM_DIR, "01. Track 1.flac"),
        (ALBUM_DIR, "1 Track 1.FLAC"),
        (ALBUM_DIR + "\\CD1", "01 - Track 1.flac"),
        (ALBUM_DIR, "1-01 Track 1.flac"),
    ]
    assert len({ident(d, n) for d, n in same}) == 1
    assert ident(ALBUM_DIR + "\\CD2", "01 - Track 1.flac") != ident(ALBUM_DIR, "01 - Track 1.flac")
    assert ident(ALBUM_DIR, "02 - Track 2.flac") != ident(ALBUM_DIR, "01 - Track 1.flac")
    # no track number: the normalised stem is the identity
    assert ident(ALBUM_DIR, "Intro.flac") == ident(ALBUM_DIR, "intro.mp3")
    assert ident(ALBUM_DIR, "Intro.flac") != ident(ALBUM_DIR, "Outro.flac")
    # the display name stays the real file name
    assert music_notify.track_name(tk(ALBUM_DIR, "01. Track 1.flac")) == "01. Track 1.flac"


@pytest.mark.asyncio
async def test_a_track_delivered_in_another_format_by_another_peer_counts_once(db):
    slskd, clock, sent = FakeSlskd(), Clock(), []
    watcher = _watcher(db, slskd, clock, sent)
    await _prime(watcher, clock)
    other = "@@share\\Artist - Album (2023) [MP3]"
    slskd.transfers = [
        _file("01 - Track 1.flac", "Completed, Rejected", ended=clock.now),
        _file("02 - Track 2.flac", "Completed, Errored", directory=ALBUM_DIR + "\\CD1", ended=clock.now),
    ]
    await watcher.poll()
    clock.advance(60)
    slskd.transfers += [
        _file("01. Track 1.mp3", "Completed, Succeeded", directory=other, username="peer2",
              ended=clock.now),
        _file("02 - Track 2.flac", "Completed, Succeeded", directory=other, username="peer2",
              ended=clock.now),
    ]
    await watcher.poll()
    clock.advance(301)
    await watcher.poll()

    assert len(sent) == 1
    assert "2/2 скачано" in sent[0]
    assert "не удалось" not in sent[0]


@pytest.mark.asyncio
async def test_v6_rekey_keeps_loose_files_of_v5_apart(tmp_path):
    """v5 rows carry no peer name: loose files keep one bucket per old batch."""
    seeded = (T0 - timedelta(hours=2)).isoformat()
    rows = [
        ("t-1", "b-1", "music_readonly\\a.flac", "Queued, Remotely", seeded),
        ("t-2", "b-2", "music_readonly\\b.flac", "Queued, Remotely", None),
    ]
    database = await _reopen_as_v5(tmp_path, rows)
    try:
        async with database.conn.execute(
            "SELECT t.transfer_id, b.notified_at FROM music_transfer t "
            "JOIN music_batch b ON b.batch_key = t.batch_key ORDER BY t.transfer_id"
        ) as cur:
            joined = [tuple(r) for r in await cur.fetchall()]
        assert joined == [("t-1", seeded), ("t-2", None)]
        async with database.conn.execute("SELECT COUNT(*) FROM music_batch") as cur:
            assert (await cur.fetchone())[0] == 2
    finally:
        await database.close()


# ------------------------------------------------ review fix round 2
def _ident(directory: str, name: str) -> str:
    return music_notify.track_identity(music_notify.album_track_key(directory, name))


def test_same_number_different_titles_are_different_tracks():
    assert _ident(ALBUM_DIR, "01 - TrackA.flac") != _ident(ALBUM_DIR, "01 - TrackB.flac")
    same = [
        (ALBUM_DIR, "01 - Track X.flac"), (ALBUM_DIR, "01. Track X.flac"),
        (ALBUM_DIR, "01 - Track X.mp3"), (ALBUM_DIR, "01 – track-x!.flac"),
        (ALBUM_DIR + "\\CD1", "01 - Track X.flac"), (ALBUM_DIR, "1-01 - Track X.flac"),
    ]
    assert len({_ident(d, n) for d, n in same}) == 1
    # a bare number is still a track, identified by disc + number alone
    assert _ident(ALBUM_DIR, "01.flac") == _ident(ALBUM_DIR, "01.mp3")
    assert _ident(ALBUM_DIR, "01.flac") != _ident(ALBUM_DIR, "02.flac")


@pytest.mark.asyncio
async def test_two_tracks_sharing_a_number_count_twice(db):
    slskd, clock, sent = FakeSlskd(), Clock(), []
    watcher = _watcher(db, slskd, clock, sent)
    await _prime(watcher, clock)
    slskd.transfers = [
        _file("01 - TrackA.flac", "Completed, Succeeded", ended=clock.now),
        _file("01 - TrackB.flac", "Completed, Errored", ended=clock.now),
    ]
    await watcher.poll()
    clock.advance(301)
    await watcher.poll()

    assert len(sent) == 1
    assert "1/2 скачано, 1 не удалось (01 - TrackB.flac)" in sent[0]


def test_a_year_first_album_folder_has_no_artist_in_it():
    live = "2012 - Wish Upon A Blackstar (Deluxe Edition)"
    assert music_notify.album_title(f"music\\Artist\\{live}") == (
        "Artist — Wish Upon A Blackstar (Deluxe Edition) (2012)")
    assert music_notify.album_title(f"music_readonly\\{live}") == (
        "Wish Upon A Blackstar (Deluxe Edition) (2012)")
    assert music_notify.album_title("x\\Artist\\[2012] - LP") == "Artist — LP (2012)"
    assert music_notify.album_key(f"x\\Artist\\{live}\\CD2") == music_notify.album_key(
        "@@share\\Artist\\Wish Upon A Blackstar (Deluxe Edition) (2012) [FLAC]")
    assert music_notify.album_key(f"x\\Artist\\{live}") != music_notify.album_key(f"x\\Other\\{live}")


@pytest.mark.asyncio
async def test_v6_rekey_of_the_live_year_first_album_gives_one_row(tmp_path):
    live = "music\\Artist\\2012 - Wish Upon A Blackstar (Deluxe Edition)"
    seeded = (T0 - timedelta(hours=2)).isoformat()
    rows = [(f"t-{n}", f"b-{n}", f"{live}\\{n:02d} - Song {n}.flac", "Queued, Remotely", seeded)
            for n in range(1, 17)]
    database = await _reopen_as_v5(tmp_path, rows)
    try:
        async with database.conn.execute(
            "SELECT batch_key, title, notified_at FROM music_batch") as cur:
            batches = [tuple(r) for r in await cur.fetchall()]
        assert len(batches) == 1
        key, title, notified = batches[0]
        assert key == music_notify.album_key(live)
        assert title == "Artist — Wish Upon A Blackstar (Deluxe Edition) (2012)"
        assert notified == seeded
        async with database.conn.execute(
            "SELECT COUNT(DISTINCT batch_key), COUNT(*) FROM music_transfer") as cur:
            assert tuple(await cur.fetchone()) == (1, 16)
    finally:
        await database.close()


# ------------------------------------------------ review fix round 3
def test_container_folders_are_not_the_artist():
    title = music_notify.album_title
    assert title("music\\Celldweller\\Albums\\2012 - X") == "Celldweller — X (2012)"
    assert title("x\\Artist\\Discography\\Album") == "Artist — Album"
    assert title("x\\Artist\\Дискография\\Album [FLAC]\\CD1") == "Artist — Album"
    assert title("x\\Artist\\Studio Albums\\Album") == "Artist — Album"
    assert title("music_readonly\\Albums\\2012 - X") == "X (2012)"
    assert title("@@share\\Discography\\Album") == "Album"
    # at most three levels up: three skippable folders in a row leave no artist
    assert title("x\\Artist\\FLAC\\Discography\\Albums\\Album") == "Album"
    assert music_notify.album_key("music\\Celldweller\\Albums\\2012 - X") == music_notify.album_key(
        "@@peer\\Celldweller\\X (2012) [FLAC]")


@pytest.mark.asyncio
async def test_v6_rekey_of_the_live_layout_with_a_container_folder_gives_one_row(tmp_path):
    # the real path of the live 16-row album (fix round 4)
    live = ("Share\\~ Electronica ~\\Celldweller\\RuTracker\\Albums\\"
            "2012 - Wish Upon A Blackstar (Deluxe Edition)\\CD1")
    seeded = (T0 - timedelta(hours=2)).isoformat()
    rows = [(f"t-{n}", f"b-{n}", f"{live}\\{n:02d} - Song {n}.flac", "Queued, Remotely", seeded)
            for n in range(1, 17)]
    database = await _reopen_as_v5(tmp_path, rows)
    try:
        async with database.conn.execute(
                "SELECT batch_key, title, notified_at FROM music_batch") as cur:
            batches = [tuple(r) for r in await cur.fetchall()]
        assert batches == [(music_notify.album_key(live),
                            "Celldweller — Wish Upon A Blackstar (Deluxe Edition) (2012)", seeded)]
        async with database.conn.execute(
                "SELECT COUNT(DISTINCT batch_key), COUNT(*) FROM music_transfer") as cur:
            assert tuple(await cur.fetchone()) == (1, 16)
    finally:
        await database.close()


# ------------------------------------------------ review fix round 4 (live paths)
# Directories read verbatim from the live slskd transfer list (public artist and
# album names; no peer names).
LIVE_DIRS = [
    ("music_readonly\\Free Flow Flava - ACTION MOVIES (2023) [FLAC]",
     "Free Flow Flava — ACTION MOVIES (2023)"),
    ("music\\[no-mb_albumid]\\Free Flow Flava\\2018 - Ninjah 404 [flac]",
     "Free Flow Flava — Ninjah 404 (2018)"),
    ("Music\\Celldweller\\2012-06-12 - Wish Upon A Blackstar (Deluxe Edition)\\CD1",
     "Celldweller — Wish Upon A Blackstar (Deluxe Edition) (2012)"),
    ("Shared\\navidromeMusic\\Placebo\\2000 - Black Market Music",
     "Placebo — Black Market Music (2000)"),
    ("Share\\~ Electronica ~\\Celldweller\\RuTracker\\Albums\\"
     "2012 - Wish Upon A Blackstar (Deluxe Edition)\\CD1",
     "Celldweller — Wish Upon A Blackstar (Deluxe Edition) (2012)"),
    ("Trump_Best_FLACS\\Apollo Brown - No Pressure, No Diamond. (2026) [WEB 24bit FLAC]",
     "Apollo Brown — No Pressure, No Diamond. (2026)"),
    ("music\\Jane Air\\ALBUM\\[2010] Weekend Warriors\\CD1",
     "Jane Air — Weekend Warriors (2010)"),
    ("music\\Apollo Brown\\No Pressure, No Diamond. (2026) [Album] [Digital Media] [FLAC]",
     "Apollo Brown — No Pressure, No Diamond. (2026)"),
    ("music\\Anix, The\\Album\\(2011) Sleepwalker",
     "Anix, The — Sleepwalker (2011)"),
    ("@@abcde\\Music\\Metallica\\(2016) Hardwired... to Self-Destruct",
     "Metallica — Hardwired... to Self-Destruct (2016)"),
]


@pytest.mark.parametrize(("directory", "title"), LIVE_DIRS)
def test_live_directories_read_as_artist_and_album(directory, title):
    assert music_notify.album_title(directory) == title


def test_live_directories_of_one_album_from_two_peers_share_a_key():
    key = music_notify.album_key
    assert key(LIVE_DIRS[2][0]) == key(LIVE_DIRS[4][0])
    assert key(LIVE_DIRS[5][0]) == key(LIVE_DIRS[7][0])
    assert len({key(d) for d, _ in LIVE_DIRS}) == 8


def test_decorated_and_tracker_folders_are_not_the_artist():
    title = music_notify.album_title
    assert title("x\\Artist\\RuTracker\\Album") == "Artist — Album"
    assert title("x\\Artist\\[some tag]\\Album") == "Artist — Album"
    assert title("x\\Artist\\~ Rock ~\\Album") == "Artist — Album"
    assert title("x\\Artist\\torrents\\Album") == "Artist — Album"
    # an album folder that names its artist never climbs
    assert title("x\\RuTracker\\Artist - Album") == "Artist — Album"
    # a bare leading year needs a dash: "1999 Something" is a title, not a year
    assert title("x\\Prince\\1999 Remastered") == "Prince — 1999 Remastered"
