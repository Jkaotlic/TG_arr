"""«В библиотеке» notifications: priming, restart and dedupe (audit 2026-09-23).

NOTIF-04: the "primed" flag was set even when the first poll failed, so a
bot restart while Radarr/Sonarr slept announced up to 50 old imports at once.
NOTIF-07: a movie produced two messages (qBittorrent + library), a season
pack N+1.
"""

from unittest.mock import AsyncMock

import pytest

from bot.db import Database
from bot.services.library_watcher import LibraryWatcher
from bot.services.notification_service import NotificationService


def _rec(rid: int, title: str, date: str, download_id: str = "") -> dict:
    return {"id": rid, "eventType": "downloadFolderImported", "sourceTitle": title,
            "date": date, "downloadId": download_id}


OLD_HISTORY = [_rec(i, f"Old Movie {i}", f"2026-09-0{i}T10:00:00Z", f"D{i}") for i in range(1, 6)]


def _services():
    radarr, sonarr = AsyncMock(), AsyncMock()
    radarr.get_history.return_value = []
    sonarr.get_history.return_value = []
    return radarr, sonarr


def _collector():
    sent: list[str] = []

    async def notify(text: str, category: str = "library") -> None:
        sent.append(text)

    return sent, notify


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "bot.db"))
    await database.connect()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_failed_first_poll_then_wake_does_not_replay_history():
    sent, notify = _collector()
    radarr, sonarr = _services()
    radarr.get_history.side_effect = ConnectionError("radarr asleep")
    watcher = LibraryWatcher(radarr, sonarr, notify=notify)

    await watcher.poll()  # radarr down: nothing primed for it
    radarr.get_history.side_effect = None
    radarr.get_history.return_value = list(OLD_HISTORY)
    await watcher.poll()  # radarr wakes: silent seed

    assert sent == []

    radarr.get_history.return_value = list(OLD_HISTORY) + [
        _rec(9, "New Movie", "2026-09-24T10:00:00Z", "D9")
    ]
    await watcher.poll()
    assert len(sent) == 1 and "New Movie" in sent[0]


@pytest.mark.asyncio
async def test_restart_announces_only_imports_newer_than_the_persisted_watermark(db):
    sent, notify = _collector()
    radarr, sonarr = _services()
    radarr.get_history.return_value = list(OLD_HISTORY)
    first = LibraryWatcher(radarr, sonarr, notify=notify, db=db)
    await first.poll()
    assert sent == []

    # bot is down; an import lands meanwhile. With a persisted watermark the
    # restarted watcher reads `/history/since` — records at/after it.
    radarr.get_history_since.return_value = [
        OLD_HISTORY[-1], _rec(9, "Landed While Down", "2026-09-24T10:00:00Z", "D9"),
    ]
    restarted = LibraryWatcher(radarr, sonarr, notify=notify, db=db)
    await restarted.poll()

    assert len(sent) == 1
    assert "Landed While Down" in sent[0]


@pytest.mark.asyncio
async def test_first_ever_run_with_a_database_is_silent(db):
    sent, notify = _collector()
    radarr, sonarr = _services()
    radarr.get_history.return_value = list(OLD_HISTORY)
    watcher = LibraryWatcher(radarr, sonarr, notify=notify, db=db)

    await watcher.poll()
    await watcher.poll()

    assert sent == []


@pytest.mark.asyncio
async def test_a_season_pack_is_one_message():
    sent, notify = _collector()
    radarr, sonarr = _services()
    watcher = LibraryWatcher(radarr, sonarr, notify=notify)
    await watcher.poll()  # seed

    sonarr.get_history.return_value = [
        _rec(100 + e, "Show.S01.1080p.WEB-DL", f"2026-09-24T10:00:{e:02d}Z", "PACK1")
        for e in range(1, 11)
    ]
    await watcher.poll()
    assert sent == []  # LOGIC-21: пак может расти
    await watcher.poll()

    assert len(sent) == 1
    assert "Show.S01.1080p.WEB-DL" in sent[0]
    assert "10 серий" in sent[0]


@pytest.mark.asyncio
async def test_different_downloads_stay_separate_messages():
    sent, notify = _collector()
    radarr, sonarr = _services()
    watcher = LibraryWatcher(radarr, sonarr, notify=notify)
    await watcher.poll()

    radarr.get_history.return_value = [
        _rec(1, "Film A", "2026-09-24T10:00:00Z", "DA"),
        _rec(2, "Film B", "2026-09-24T10:01:00Z", "DB"),
    ]
    await watcher.poll()

    assert len(sent) == 2
    assert "серий" not in sent[0]


# ---------------------------------------------------------------- qBittorrent
def _raw(category: str, progress: float) -> dict:
    return {"name": "Some.Release.2160p", "progress": progress,
            "state": "uploading" if progress >= 1 else "downloading", "category": category}


async def _complete_one(category: str) -> AsyncMock:
    qbit, sender = AsyncMock(), AsyncMock(return_value=True)
    svc = NotificationService(qbit, sender)
    svc.subscribe_user(1)
    h = "e" * 40
    svc._tracked_torrents[h] = {"completed": False, "notified": False, "name": "x", "added_on": None}
    svc._torrents_raw[h] = _raw(category, 0.9)
    qbit.get_maindata.return_value = {
        "rid": 2, "torrents": {h: {"progress": 1.0, "state": "uploading"}},
        "torrents_removed": [], "full_update": False,
    }
    await svc._check_for_completions()
    assert h not in svc._tracked_torrents  # resolved either way
    return sender


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["radarr", "tv-sonarr", "sonarr"])
async def test_arr_torrents_get_no_qbit_message(category):
    """Radarr/Sonarr imports are announced by library_watch — one message, not two."""
    sender = await _complete_one(category)
    sender.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["lidarr", "", "manual"])
async def test_other_torrents_still_get_the_qbit_message(category):
    sender = await _complete_one(category)
    sender.assert_awaited_once()


def test_arr_categories_are_configurable(monkeypatch):
    from bot.config import Settings

    monkeypatch.setenv("ARR_TORRENT_CATEGORIES", "films, series")
    assert Settings().arr_torrent_category_set == {"films", "series"}


@pytest.mark.asyncio
async def test_with_a_watermark_the_watcher_asks_only_for_newer_history(db):
    """Log review 2026-10-10: every poll re-downloaded 50 records with full
    movie/series cards (~400 KB a cycle, 1 GB in two weeks). Once a service has
    a watermark, only `/history/since` is read."""
    from datetime import datetime, timezone

    sent, notify = _collector()
    radarr, sonarr = _services()
    radarr.get_history.return_value = list(OLD_HISTORY)
    radarr.get_history_since.return_value = []
    sonarr.get_history_since.return_value = []
    watcher = LibraryWatcher(radarr, sonarr, notify=notify, db=db)
    await watcher.poll()  # first ever run: paged read seeds the watermark
    assert radarr.get_history.await_count == 1

    newest = OLD_HISTORY[-1]
    radarr.get_history_since.return_value = [
        newest, _rec(9, "Fresh Import", "2026-09-24T10:00:00Z", "D9"),
    ]
    await watcher.poll()

    assert radarr.get_history.await_count == 1  # no second paged read
    since = radarr.get_history_since.call_args.args[0]
    assert since == datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)
    assert radarr.get_history_since.call_args.kwargs.get("include_details") is True
    assert len(sent) == 1 and "Fresh Import" in sent[0]  # boundary record not re-announced


@pytest.mark.asyncio
async def test_since_read_failure_is_survived_like_a_paged_one(db):
    sent, notify = _collector()
    radarr, sonarr = _services()
    radarr.get_history.return_value = list(OLD_HISTORY)
    sonarr.get_history_since.return_value = []
    watcher = LibraryWatcher(radarr, sonarr, notify=notify, db=db)
    await watcher.poll()

    radarr.get_history_since.side_effect = ConnectionError("radarr asleep")
    await watcher.poll()  # must not raise
    radarr.get_history_since.side_effect = None
    radarr.get_history_since.return_value = [_rec(9, "After Nap", "2026-09-24T10:00:00Z", "D9")]
    await watcher.poll()

    assert len(sent) == 1 and "After Nap" in sent[0]
