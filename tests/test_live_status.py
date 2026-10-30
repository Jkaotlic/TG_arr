"""One living status message per bot-initiated grab (improvements round 2, E1).

The grab confirmation itself is edited in place as the download moves:
waiting → linked to the torrent (by the guid the bot pushed, via *arr's
"grabbed" history) → progress (10 % buckets, ≥60 s apart) → «Скачано,
импортирую…» → «в библиотеке». The separate qBit/library messages are
suppressed for the grabber's chat only.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

from bot.db import Database
from bot.models import TorrentInfo, TorrentState
from bot.services import live_status
from bot.services.library_watcher import LibraryWatcher
from bot.services.live_status import LiveStatusService

HASH = "ABCDEF0123456789ABCDEF0123456789ABCDEF01"
GUID = "https://indexer.example.com/release/42"
T0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeEditor:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int, str, Any]] = []
        self.outcome = "ok"

    async def __call__(self, chat_id: int, message_id: int, text: str, markup: Any = None) -> str:
        if self.outcome != "ok":
            return self.outcome
        self.calls.append((chat_id, message_id, text, markup))
        return "ok"

    @property
    def texts(self) -> list[str]:
        return [c[2] for c in self.calls]


class FakeArr:
    def __init__(self, id_field: str = "movieId") -> None:
        self.id_field = id_field
        self.grabbed: list[dict] = []
        self.imported: list[dict] = []
        self.queue: list[dict] = []
        self.history_calls: list[str] = []

    async def get_history(self, event_type: str = "downloadFolderImported", page_size: int = 50, **_: Any):
        self.history_calls.append(event_type)
        return list(self.grabbed if event_type == "grabbed" else self.imported)

    async def get_history_since(self, since: datetime, event_type: str = "downloadFolderImported", **_: Any):
        self.history_calls.append(event_type)
        return list(self.grabbed if event_type == "grabbed" else self.imported)

    async def get_queue(self) -> list[dict]:
        return list(self.queue)


class FakeQbit:
    def __init__(self) -> None:
        self.torrents: dict[str, TorrentInfo] = {}
        self.calls = 0

    async def get_torrent(self, full_hash: str) -> Optional[TorrentInfo]:
        self.calls += 1
        return self.torrents.get(full_hash.lower())

    async def get_torrents(self, **_: Any) -> list[TorrentInfo]:
        self.calls += 1
        return list(self.torrents.values())

    def put(self, progress: float, state: TorrentState = TorrentState.DOWNLOADING, **kw: Any) -> None:
        self.torrents[HASH.lower()] = TorrentInfo(
            hash=HASH.lower(), name=kw.pop("name", "Movie.2020.1080p"), progress=progress,
            state=state, download_speed=kw.pop("speed", 12_900_000), eta=kw.pop("eta", 480), **kw,
        )


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "bot.db"))
    await database.connect()
    yield database
    await database.close()


def _grabbed(movie_id: int = 7, guid: str = GUID, download_id: str = HASH, date: str = "2026-09-24T12:00:05Z") -> dict:
    return {"id": 900, "eventType": "grabbed", "movieId": movie_id, "downloadId": download_id,
            "sourceTitle": "Movie.2020.1080p", "date": date, "data": {"guid": guid}}


def _imported(download_id: str = HASH, rid: int = 1000, movie_id: int = 7) -> dict:
    return {"id": rid, "eventType": "downloadFolderImported", "movieId": movie_id,
            "downloadId": download_id, "sourceTitle": "Movie.2020.1080p", "date": "2026-09-24T13:00:00Z"}


async def _register(db, clock, *, arr="radarr", guid: Optional[str] = GUID, kind="movie", chat=111, msg=5):
    return await live_status.register_grab(
        db, chat_id=chat, message_id=msg, kind=kind, title="Movie (2020)",
        release="Movie.2020.1080p", arr=arr, arr_id=7, guid=guid, provider_id="tmdb.603",
        now=clock(),
    )


def _service(db, clock, editor, radarr=None, sonarr=None, qbit=None, **kw):
    return LiveStatusService(
        db, editor, radarr=radarr or FakeArr(), sonarr=sonarr or FakeArr("seriesId"),
        qbittorrent=qbit or FakeQbit(), clock=clock, **kw,
    )


# ------------------------------------------------------------------ render
def test_initial_text_is_the_waiting_message():
    text = live_status.initial_text("Movie <2020>", "Rel & ease")
    assert text == "⏳ <b>Movie &lt;2020&gt;</b>\nРелиз: <i>Rel &amp; ease</i>\nЖду загрузку…"


def test_progress_line_format():
    t = TorrentInfo(hash="h", name="n", progress=0.43, download_speed=12_900_000, eta=480)
    assert live_status.progress_line(t) == "📥 43% · 12.3 МБ/с · осталось ~8 мин"
    unknown = TorrentInfo(hash="h", name="n", progress=0.1, download_speed=0, eta=8640000)
    assert live_status.progress_line(unknown) == "📥 10%"


# ------------------------------------------------------------------ lifecycle
@pytest.mark.asyncio
async def test_full_lifecycle_edits_one_message_in_place(db):
    clock, editor, radarr, qbit = Clock(), FakeEditor(), FakeArr(), FakeQbit()
    svc = _service(db, clock, editor, radarr=radarr, qbit=qbit)
    await _register(db, clock)

    # not yet in *arr history: nothing to do, no edit
    await svc.tick()
    assert editor.calls == []

    radarr.grabbed = [_grabbed(guid="https://other.example.com/1", download_id="0" * 40), _grabbed()]
    qbit.put(0.05)
    clock.advance(61)
    await svc.tick()
    assert editor.texts[-1].endswith("📥 5% · 12.3 МБ/с · осталось ~8 мин")
    assert (await db.live_rows())[0]["download_id"] == HASH

    # same bucket → no edit; next bucket but <60 s → no edit
    qbit.put(0.08)
    clock.advance(30)
    await svc.tick()
    qbit.put(0.25)
    clock.advance(10)
    await svc.tick()
    assert len(editor.calls) == 1
    clock.advance(25)
    await svc.tick()
    assert len(editor.calls) == 2 and "📥 25%" in editor.texts[-1]

    qbit.put(1.0, TorrentState.SEEDING)
    await svc.tick()  # state change: not throttled
    assert editor.texts[-1].endswith("✅ Скачано, импортирую…")

    chats = await svc.claim_import("radarr", HASH, [_imported()])
    assert chats == {111}
    assert editor.texts[-1].startswith("✅ <b>Movie (2020)</b> — в библиотеке")
    assert all(c[:2] == (111, 5) for c in editor.calls)

    # a second claim (next poll of the same pack) still suppresses, no edit
    before = len(editor.calls)
    assert await svc.claim_import("radarr", HASH.lower(), [_imported(rid=1001)]) == {111}
    assert len(editor.calls) == before
    assert (await db.live_rows())[0]["state"] == "imported"


@pytest.mark.asyncio
async def test_outside_bot_grab_is_not_claimed_and_still_announced(db):
    clock, editor = Clock(), FakeEditor()
    svc = _service(db, clock, editor)
    await _register(db, clock)
    radarr = FakeArr()
    radarr.imported = [_imported(download_id="F" * 40, movie_id=99)]
    sent: list[tuple[str, dict]] = []

    async def notify(text: str, category: str, **extra: Any) -> None:
        sent.append((text, extra))

    watcher = LibraryWatcher(radarr, FakeArr("seriesId"), notify=notify, live=svc)
    watcher._primed_services.update({"radarr", "sonarr"})
    await watcher.poll()
    assert len(sent) == 1 and "в библиотеке" in sent[0][0]
    assert not sent[0][1].get("exclude")
    assert editor.calls == []


@pytest.mark.asyncio
async def test_watcher_excludes_the_grabber_chat_from_the_library_notice(db):
    clock, editor, qbit = Clock(), FakeEditor(), FakeQbit()
    radarr = FakeArr()
    radarr.grabbed = [_grabbed()]
    qbit.put(1.0, TorrentState.SEEDING)
    svc = _service(db, clock, editor, radarr=radarr, qbit=qbit)
    await _register(db, clock)
    await svc.tick()
    sent: list[tuple[str, dict]] = []

    async def notify(text: str, category: str, **extra: Any) -> None:
        sent.append((text, extra))

    radarr.imported = [_imported()]
    watcher = LibraryWatcher(radarr, FakeArr("seriesId"), notify=notify, live=svc)
    watcher._primed_services.update({"radarr", "sonarr"})
    await watcher.poll()
    assert len(sent) == 1 and sent[0][1]["exclude"] == frozenset({111})
    assert "в библиотеке" in editor.texts[-1]


@pytest.mark.asyncio
async def test_restart_resumes_editing_the_same_message(db):
    clock, editor, radarr, qbit = Clock(), FakeEditor(), FakeArr(), FakeQbit()
    radarr.grabbed = [_grabbed()]
    qbit.put(0.3)
    first = _service(db, clock, editor, radarr=radarr, qbit=qbit)
    await _register(db, clock)
    clock.advance(61)
    await first.tick()
    assert len(editor.calls) == 1

    # "restart": a brand-new service over the same database
    second = _service(db, clock, editor, radarr=radarr, qbit=qbit)
    qbit.put(0.6)
    clock.advance(61)
    await second.tick()
    assert len(editor.calls) == 2 and "📥 60%" in editor.texts[-1]
    assert {c[:2] for c in editor.calls} == {(111, 5)}


@pytest.mark.asyncio
async def test_unlinked_after_ten_minutes_stops_tracking_honestly(db):
    clock, editor = Clock(), FakeEditor()
    svc = _service(db, clock, editor)
    await _register(db, clock)
    clock.advance(601)
    await svc.tick()
    assert "не отслеживается" in editor.texts[-1]
    clock.advance(60)
    await svc.tick()
    assert len(editor.calls) == 1
    # not claimed: the regular notice will go to the grabber too
    assert await svc.claim_import("radarr", HASH, [_imported()]) == set()


@pytest.mark.asyncio
async def test_queue_is_the_fallback_link(db):
    clock, editor, radarr, qbit = Clock(), FakeEditor(), FakeArr(), FakeQbit()
    radarr.queue = [{"title": "movie.2020.1080p", "downloadId": HASH, "movieId": 7}]
    qbit.put(0.2)
    svc = _service(db, clock, editor, radarr=radarr, qbit=qbit)
    await _register(db, clock)
    clock.advance(61)
    await svc.tick()
    assert (await db.live_rows())[0]["download_id"] == HASH


@pytest.mark.asyncio
async def test_stalled_two_hours_then_recovers(db):
    clock, editor, radarr, qbit = Clock(), FakeEditor(), FakeArr(), FakeQbit()
    radarr.grabbed = [_grabbed()]
    qbit.put(0.4, TorrentState.STALLED, speed=0, eta=8640000)
    svc = _service(db, clock, editor, radarr=radarr, qbit=qbit)
    await _register(db, clock)
    clock.advance(61)
    await svc.tick()
    n = len(editor.calls)
    clock.advance(3600)
    await svc.tick()
    assert len(editor.calls) == n  # stalled 1 h: quiet
    clock.advance(3601)
    await svc.tick()
    assert editor.texts[-1].startswith("⚠️") and "Застряло: нет сидов" in editor.texts[-1]
    qbit.put(0.45)
    clock.advance(30)
    await svc.tick()
    assert "📥 45%" in editor.texts[-1]


@pytest.mark.asyncio
async def test_removed_without_import_is_reported_missing(db):
    clock, editor, radarr, qbit = Clock(), FakeEditor(), FakeArr(), FakeQbit()
    radarr.grabbed = [_grabbed()]
    qbit.put(0.4)
    svc = _service(db, clock, editor, radarr=radarr, qbit=qbit)
    await _register(db, clock)
    clock.advance(61)
    await svc.tick()
    qbit.torrents.clear()
    clock.advance(61)
    await svc.tick()
    assert editor.texts[-1].startswith("❌") and "Загрузка пропала из очереди" in editor.texts[-1]
    assert (await db.live_rows())[0]["state"] == "missing"


@pytest.mark.asyncio
async def test_gone_from_qbit_but_imported_is_not_missing(db):
    clock, editor, radarr, qbit = Clock(), FakeEditor(), FakeArr(), FakeQbit()
    radarr.grabbed = [_grabbed()]
    qbit.put(0.4)
    svc = _service(db, clock, editor, radarr=radarr, qbit=qbit)
    await _register(db, clock)
    clock.advance(61)
    await svc.tick()
    qbit.torrents.clear()
    radarr.imported = [_imported()]
    clock.advance(61)
    await svc.tick()
    assert "в библиотеке" in editor.texts[-1]


@pytest.mark.asyncio
async def test_edit_errors(db):
    clock, editor, radarr, qbit = Clock(), FakeEditor(), FakeArr(), FakeQbit()
    radarr.grabbed = [_grabbed()]
    qbit.put(0.3)
    svc = _service(db, clock, editor, radarr=radarr, qbit=qbit)
    await _register(db, clock)
    clock.advance(61)
    editor.outcome = "retry"  # flood control: skip this tick, state unchanged
    await svc.tick()
    assert (await db.live_rows())[0]["progress"] is None
    editor.outcome = "ok"
    await svc.tick()
    assert "📥 30%" in editor.texts[-1]

    editor.outcome = "lost"  # message deleted: stop silently
    qbit.put(1.0, TorrentState.SEEDING)
    await svc.tick()
    assert (await db.live_rows())[0]["state"] == "lost"
    assert await svc.claim_import("radarr", HASH, [_imported()]) == set()


@pytest.mark.asyncio
async def test_direct_qbit_grab_links_by_tag_and_name(db):
    clock, editor, qbit = Clock(), FakeEditor(), FakeQbit()
    qbit.put(0.5, tags=["tgarr-direct"])
    svc = _service(db, clock, editor, qbit=qbit)
    await _register(db, clock, arr="qbit", guid=None)
    clock.advance(61)
    await svc.tick()
    assert (await db.live_rows())[0]["download_id"] == HASH
    assert await svc.owner_chats(HASH.lower()) == {111}
    qbit.put(1.0, TorrentState.SEEDING, tags=["tgarr-direct"])
    await svc.tick()
    assert editor.texts[-1].startswith("✅ <b>Movie (2020)</b> — скачано")


@pytest.mark.asyncio
async def test_idle_tick_makes_no_calls(db):
    clock, editor, radarr, qbit = Clock(), FakeEditor(), FakeArr(), FakeQbit()
    svc = _service(db, clock, editor, radarr=radarr, qbit=qbit)
    await svc.tick()
    assert qbit.calls == 0 and radarr.history_calls == []


@pytest.mark.asyncio
async def test_maintenance_prunes_old_terminal_rows(db):
    clock = Clock()
    clock.now = datetime.now(timezone.utc) - timedelta(days=8)
    await _register(db, clock)
    rows = await db.live_rows()
    await db.live_update(rows[0]["key"], state="imported", updated_at=clock().isoformat())
    stats = await db.run_maintenance()
    assert stats["live_status"] == 1
    assert await db.live_rows() == []


@pytest.mark.asyncio
async def test_notification_service_skips_the_owner_chat_for_direct_torrent():
    from unittest.mock import AsyncMock

    from bot.services.notification_service import NotificationService

    sent: list[int] = []

    async def send(uid: int, text: str) -> bool:
        sent.append(uid)
        return True

    async def owners(h: str) -> set[int]:
        return {111} if h == HASH.lower() else set()

    svc = NotificationService(AsyncMock(), send, exclude_chats=owners)
    svc.subscribe_user(111)
    svc.subscribe_user(222)
    await svc._notify_completion(TorrentInfo(hash=HASH.lower(), name="n", progress=1.0))
    assert sent == [222]


# ------------------------------------------------------------------ Telegram editor (bot.main)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (None, "ok"),
        ("not_modified", "ok"),
        ("not_found", "lost"),
        ("forbidden", "lost"),
        ("flood", "retry"),
        ("network", "retry"),
    ],
)
async def test_edit_living_message_maps_telegram_errors(monkeypatch, error, expected):
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.exceptions import (
        TelegramBadRequest,
        TelegramForbiddenError,
        TelegramNetworkError,
        TelegramRetryAfter,
    )

    from bot import main

    monkeypatch.setattr(main, "_last_sent_at", {})
    monkeypatch.setattr(main, "_edit_not_before", {})
    errors = {
        "not_modified": TelegramBadRequest(
            method=MagicMock(), message="Bad Request: message is not modified: specified new message content",
        ),
        "not_found": TelegramBadRequest(method=MagicMock(), message="Bad Request: message to edit not found"),
        "forbidden": TelegramForbiddenError(method=MagicMock(), message="Forbidden: bot was blocked by the user"),
        "flood": TelegramRetryAfter(method=MagicMock(), message="Flood control", retry_after=5),
        "network": TelegramNetworkError(method=MagicMock(), message="timeout"),
    }
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(side_effect=errors.get(error) if error else None)
    outcome = await main.edit_living_message(bot, 111, 5, "text", None)
    assert outcome == expected
    kwargs = bot.edit_message_text.call_args.kwargs
    assert kwargs["chat_id"] == 111 and kwargs["message_id"] == 5


@pytest.mark.asyncio
async def test_deliver_library_notice_skips_excluded_chats(db, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    import structlog

    from bot import main

    monkeypatch.setattr(main, "_last_sent_at", {})
    monkeypatch.setattr(main, "get_settings", lambda: MagicMock(allowed_tg_ids=[111, 222], admin_tg_ids=[]))
    bot = MagicMock()
    bot.send_message = AsyncMock()
    await main.deliver_library_notice(
        bot, db, "✅ <b>X</b> — в библиотеке.", "library", structlog.get_logger(),
        exclude=frozenset({111}),
    )
    assert [c.args[0] for c in bot.send_message.call_args_list] == [222]


# ------------------------------------------------------------------ grab handler
def _grab_session():
    from bot.models import ContentType, MovieInfo, SearchResult, SearchSession

    release = SearchResult(guid=GUID, origin="arr", indexer="idx", indexer_id=1,
                           title="Movie.2020.1080p", size=1)
    return SearchSession(
        user_id=111, query="movie", content_type=ContentType.MOVIE, selected_result=release,
        selected_content=MovieInfo(tmdb_id=603, title="Movie", year=2020, radarr_id=7),
    )


def _grab_mocks():
    from unittest.mock import AsyncMock, MagicMock

    from bot.models import ActionLog, ActionType, ContentType

    add_service = MagicMock()
    add_service.grab_release = AsyncMock(return_value=(
        True, ActionLog(user_id=0, action_type=ActionType.GRAB, content_type=ContentType.MOVIE),
    ))
    add_service.radarr.set_movie_monitored = AsyncMock(return_value=True)
    message = MagicMock()
    message.edit_text = AsyncMock()
    message.chat.id = 111
    message.message_id = 5
    return add_service, message


@pytest.mark.asyncio
async def test_successful_grab_becomes_the_living_message(db):
    from unittest.mock import MagicMock

    from bot.handlers.search import grab as grab_mod
    from bot.models import User

    add_service, message = _grab_mocks()
    await db.create_user(User(tg_id=111))
    await grab_mod._execute_grab(message, _grab_session(), User(tg_id=111), db, MagicMock(), add_service)

    assert message.edit_text.call_args.args[0] == (
        "⏳ <b>Movie (2020)</b>\nРелиз: <i>Movie.2020.1080p</i>\nЖду загрузку…"
    )
    [row] = await db.live_rows()
    assert (row["chat_id"], row["message_id"], row["arr"], row["arr_id"], row["guid"], row["state"]) == (
        111, 5, "radarr", 7, GUID, "waiting",
    )
    assert row["provider_id"] == "tmdb.603"


@pytest.mark.asyncio
async def test_muted_library_keeps_the_plain_confirmation(db):
    from unittest.mock import MagicMock

    from bot.handlers.search import grab as grab_mod
    from bot.models import User, UserPreferences

    add_service, message = _grab_mocks()
    user = User(tg_id=111, preferences=UserPreferences(notify_library=False))
    await db.create_user(user)
    await grab_mod._execute_grab(message, _grab_session(), user, db, MagicMock(), add_service)

    assert "Жду загрузку" not in message.edit_text.call_args.args[0]
    assert await db.live_rows() == []


@pytest.mark.asyncio
async def test_force_grab_tracks_the_direct_torrent(db):
    from unittest.mock import MagicMock

    from bot.handlers.search import grab as grab_mod
    from bot.models import User

    add_service, message = _grab_mocks()
    await db.create_user(User(tg_id=111))
    await grab_mod._execute_grab(
        message, _grab_session(), User(tg_id=111), db, MagicMock(), add_service, force_download=True,
    )
    [row] = await db.live_rows()
    assert row["arr"] == "qbit" and row["provider_id"] is None
