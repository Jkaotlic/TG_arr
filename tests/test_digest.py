"""Morning digest (improvements round 2, E3).

Once per local day at DIGEST_HOUR (catch-up if the bot was down then, until
23:00): what landed in the library, music albums, what is stuck, what airs
today. Only non-empty sections; nothing to say → no message at all. Each
user's muted categories are left out; «🌅 Утренняя сводка: выкл» opts out.
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from bot.db import Database
from bot.models import SlskdTransfer, TorrentInfo, TorrentState, UserPreferences
from bot.services.digest import DigestData, DigestService, render_digest

MSK = timezone(timedelta(hours=3))


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "bot.db"))
    await database.connect()
    yield database
    await database.close()


def _local(hour: int, minute: int = 0, day: int = 25) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=MSK)


class Clock:
    def __init__(self, when: datetime) -> None:
        self.now = when

    def __call__(self) -> datetime:
        return self.now.astimezone(timezone.utc)


def _import(rid: int, did: str, when: datetime, *, movie: str | None = None, series: str | None = None) -> dict:
    rec: dict[str, Any] = {"id": rid, "downloadId": did, "sourceTitle": f"Rel.{rid}",
                           "date": when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
    if movie:
        rec["movie"] = {"title": movie}
    if series:
        rec["series"] = {"title": series}
    return rec


def _clients():
    radarr, sonarr, qbit, slskd = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
    radarr.get_history.return_value = []
    sonarr.get_history.return_value = []
    radarr.get_queue.return_value = []
    sonarr.get_queue.return_value = []
    sonarr.get_calendar.return_value = []
    qbit.get_torrents.return_value = []
    slskd.get_transfers.return_value = []
    return radarr, sonarr, qbit, slskd


def _service(db, clock, clients, sent, users=None, hour=9):
    radarr, sonarr, qbit, slskd = clients

    async def recipients():
        return users if users is not None else [(111, UserPreferences())]

    async def send(uid: int, text: str) -> None:
        sent.append((uid, text))

    async def get_slskd():
        return slskd

    return DigestService(
        db, radarr=radarr, sonarr=sonarr, qbittorrent=qbit, get_slskd=get_slskd,
        recipients=recipients, send=send, clock=clock, tz=MSK, hour=hour,
    )


# ------------------------------------------------------------------ render
def test_empty_day_renders_nothing():
    assert render_digest(DigestData(), UserPreferences()) is None


def test_render_sections_and_overflow():
    data = DigestData(
        library=[f"Title {i}" for i in range(7)],
        music_albums=2,
        stuck=[("library", "торренты без движения: 1")],
        airing=["Show — S02E05"],
    )
    text = render_digest(data, UserPreferences())
    assert text == (
        "🌅 <b>Сводка за сутки</b>\n\n"
        "📚 В библиотеку: 7\n• Title 0\n• Title 1\n• Title 2\n• Title 3\n• Title 4\n• и ещё 2\n\n"
        "🎵 Музыка: 2 альбома\n\n"
        "⚠️ Застряло:\n• торренты без движения: 1\n\n"
        "📅 Сегодня выходит:\n• Show — S02E05"
    )


def test_muted_categories_are_left_out():
    data = DigestData(library=["A"], music_albums=1)
    prefs = UserPreferences(notify_library=False, notify_music="off")
    assert render_digest(data, prefs) is None
    assert "Музыка" not in (render_digest(data, UserPreferences(notify_music="off")) or "")


def test_titles_are_escaped():
    text = render_digest(DigestData(library=["<b>x</b>"]), UserPreferences())
    assert "&lt;b&gt;x&lt;/b&gt;" in (text or "")


# ------------------------------------------------------------------ collect
@pytest.mark.asyncio
async def test_collect_content(db):
    clock = Clock(_local(9, 5))
    clients = radarr, sonarr, qbit, slskd = _clients()
    now_utc = clock()
    radarr.get_history.return_value = [
        _import(1, "A", _local(8), movie="Dune"),
        _import(2, "OLD", _local(8, day=24) - timedelta(hours=2), movie="Old"),
    ]
    sonarr.get_history.return_value = [
        _import(3, "P", _local(2), series="Show"),
        _import(4, "P", _local(2), series="Show"),  # one season pack = one entry
    ]
    qbit.get_torrents.return_value = [
        TorrentInfo(hash="s", name="stuck", progress=0.2, state=TorrentState.STALLED,
                    added_on=now_utc - timedelta(days=2)),
        TorrentInfo(hash="f", name="fresh", progress=0.2, state=TorrentState.STALLED,
                    added_on=now_utc - timedelta(hours=2)),
    ]
    slskd.get_transfers.return_value = [
        SlskdTransfer(username="peer1", state="Queued, Remotely", requested_at=now_utc - timedelta(days=3)),
        SlskdTransfer(username="peer1", state="Queued, Remotely", requested_at=now_utc - timedelta(hours=1)),
    ]
    radarr.get_queue.return_value = [{"title": "x", "trackedDownloadStatus": "warning"}]
    sonarr.get_calendar.return_value = [
        {"series_title": "Show", "season": 2, "episode": 5, "air_date": "2026-09-25T18:00:00Z"},
        {"series_title": "Late", "season": 1, "episode": 1, "air_date": "2026-09-25T22:30:00Z"},  # 01:30 MSK tomorrow
    ]
    await db.music_record([("t1", "album:a", "k", "Completed, Succeeded", "Album")], now_utc - timedelta(hours=3))
    await db.music_mark_notified(["album:a"], now_utc - timedelta(hours=3))

    data = await _service(db, clock, clients, []).collect()
    assert data.library == ["Dune", "Show"]
    assert data.music_albums == 1
    assert data.stuck == [
        ("library", "торренты без движения больше суток: 1"),
        ("music", "музыка в очереди у пиров больше суток: 1"),
        ("library", "Radarr: 1 с предупреждением"),
    ]
    assert data.airing == ["Show — S02E05"]
    assert radarr.get_history.call_args.kwargs.get("include_details") is True


# ------------------------------------------------------------------ schedule
@pytest.mark.asyncio
async def test_empty_day_sends_nothing_but_counts_as_done(db):
    sent: list = []
    clock = Clock(_local(9, 1))
    svc = _service(db, clock, _clients(), sent)
    await svc.maybe_send()
    assert sent == []
    assert await db.kv_get("digest_last_date") == "2026-09-25"


@pytest.mark.asyncio
async def test_once_per_day_and_not_before_the_hour(db):
    sent: list = []
    clock = Clock(_local(8, 59))
    clients = _clients()
    clients[0].get_history.return_value = [_import(1, "A", _local(8), movie="Dune")]
    svc = _service(db, clock, clients, sent)
    await svc.maybe_send()
    assert sent == []  # before DIGEST_HOUR
    clock.now = _local(9, 0)
    await svc.maybe_send()
    clock.now = _local(9, 30)
    await svc.maybe_send()
    assert len(sent) == 1 and "Dune" in sent[0][1]


@pytest.mark.asyncio
async def test_catch_up_after_downtime_but_not_late_evening(db):
    sent: list = []
    clients = _clients()
    clients[0].get_history.return_value = [_import(1, "A", _local(8), movie="Dune")]
    clock = Clock(_local(15))  # bot was down at 09:00
    await _service(db, clock, clients, sent).maybe_send()
    assert len(sent) == 1

    sent.clear()
    clock.now = _local(23, 10, day=26)  # next day, started only at 23:10
    await _service(db, clock, clients, sent).maybe_send()
    assert sent == []


@pytest.mark.asyncio
async def test_timezone_boundary_uses_local_day(db):
    """02:30 MSK is still the previous day in UTC — the local date counts."""
    sent: list = []
    clients = _clients()
    clients[0].get_history.return_value = [_import(1, "A", _local(1), movie="Dune")]
    await db.kv_set("digest_last_date", "2026-09-24")
    clock = Clock(_local(2, 30))
    await _service(db, clock, clients, sent, hour=2).maybe_send()
    assert len(sent) == 1
    assert await db.kv_get("digest_last_date") == "2026-09-25"


@pytest.mark.asyncio
async def test_muted_and_opted_out_users(db):
    sent: list = []
    clients = _clients()
    clients[0].get_history.return_value = [_import(1, "A", _local(8), movie="Dune")]
    users = [
        (111, UserPreferences()),
        (222, UserPreferences(notify_library=False)),  # only section muted → nothing to say
        (333, UserPreferences(digest_enabled=False)),
    ]
    await _service(db, Clock(_local(9)), clients, sent, users=users).maybe_send()
    assert [uid for uid, _ in sent] == [111]


# ------------------------------------------------------------------ settings
def test_settings_toggle_and_callback_length():
    from bot.ui.callbacks import SettingCB
    from bot.ui.keyboards import Keyboards

    for enabled in (True, False):
        kb = Keyboards.settings_menu(prefs=UserPreferences(digest_enabled=enabled))
        labels = {b.text: b.callback_data for row in kb.inline_keyboard for b in row}
        label = f"🌅 Утренняя сводка: {'вкл' if enabled else 'выкл'}"
        assert label in labels
        data = labels[label]
        assert data is not None and len(data.encode()) <= 64
        assert SettingCB.unpack(data).value == ("0" if enabled else "1")


@pytest.mark.asyncio
async def test_settings_handler_toggles_digest(db):
    from unittest.mock import MagicMock, patch

    from bot.handlers import settings as settings_handlers
    from bot.models import User
    from bot.ui.callbacks import SettingCB

    user = User(tg_id=111)
    await db.create_user(user)
    cb = MagicMock()
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    with patch.object(settings_handlers, "accessible_message", return_value=cb.message), \
         patch.object(settings_handlers, "_render_settings_menu", AsyncMock(return_value=("t", None))):
        await settings_handlers.handle_set_notify(
            cb, SettingCB(key="digest_enabled", value="0"), user, db,
        )
    stored = await db.get_user(111)
    assert stored is not None and stored.preferences.digest_enabled is False
    assert "выкл" in cb.answer.call_args.args[0]


def test_digest_hour_config(monkeypatch):
    from bot.config import Settings

    monkeypatch.setenv("DIGEST_HOUR", "")
    assert Settings().digest_hour == 9
    monkeypatch.setenv("DIGEST_HOUR", "7")
    assert Settings().digest_hour == 7


@pytest.mark.asyncio
async def test_recipient_preferences_defaults_for_unknown_users(db, monkeypatch):
    from unittest.mock import MagicMock

    from bot import main
    from bot.models import User

    await db.create_user(User(tg_id=111, preferences=UserPreferences(digest_enabled=False)))
    monkeypatch.setattr(main, "get_settings", lambda: MagicMock(allowed_tg_ids=[111, 222], admin_tg_ids=[]))
    result = await main.recipient_preferences(db)
    assert [(uid, p.digest_enabled) for uid, p in result] == [(111, False), (222, True)]


@pytest.mark.asyncio
async def test_failed_collect_does_not_burn_the_day(db):
    """Log review 2026-10-10: the day was marked sent BEFORE collecting, so a
    collect/recipients failure silently skipped that day's digest."""
    sent: list = []
    clock = Clock(_local(9, 1))
    clients = _clients()
    clients[0].get_history.return_value = [_import(1, "A", _local(8), movie="Dune")]
    svc = _service(db, clock, clients, sent)
    real_collect = svc.collect
    calls = 0

    async def flaky_collect():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("radarr hiccup")
        return await real_collect()

    svc.collect = flaky_collect  # type: ignore[method-assign]
    assert await svc.maybe_send() == 0
    assert await db.kv_get("digest_last_date") is None

    clock.now = _local(9, 16)  # next _DIGEST_CHECK_S tick
    assert await svc.maybe_send() == 1
    assert len(sent) == 1 and "Dune" in sent[0][1]
    assert await db.kv_get("digest_last_date") == "2026-09-25"
