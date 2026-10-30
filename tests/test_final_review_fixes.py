"""Финальное ревью ветки аудита 2026-10-30: точечные правки шести находок.

Находки 1 и 2 (сообщения поиска о сбое Radarr/Sonarr) проверены сквозными тестами в
tests/e2e/test_search_flows.py.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.db import Database

T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
HASH = "a" * 40


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "bot.db"))
    await database.connect()
    yield database
    await database.close()


# --------------------------------------------------- находка 3: причина в карточке запуска
async def test_warmup_timeout_leaves_a_readable_reason(monkeypatch):
    """`str(TimeoutError())` пуст: в карточке админа стояло «Radarr: ❌ » без причины."""
    from bot import main
    from bot.clients import registry

    hanging = MagicMock()
    hanging.check_connection = AsyncMock(side_effect=TimeoutError())
    monkeypatch.setattr(registry, "get_radarr", AsyncMock(return_value=hanging))
    for name in ("get_sonarr", "get_prowlarr", "get_lidarr", "get_slskd"):
        monkeypatch.setattr(registry, name, AsyncMock(return_value=None))

    summary = await main._warm_up_clients(MagicMock())

    assert summary["radarr"] == ("error", "TimeoutError")
    card = main.build_startup_message(warmup=summary, admin_ids=[1], allowed_ids=[1], version=None)
    assert "Radarr: ❌ TimeoutError" in card


# --------------------------------------------------- находка 4: остановка не ждёт Emby
class _Pack:
    """Sonarr, отдающий один пак с downloadId и данными для поиска в Emby."""

    def __init__(self) -> None:
        self.records: list[dict] = []

    async def get_history_since(self, *args, **kwargs):
        return self.records

    async def get_history(self, *args, **kwargs):
        return self.records


async def test_flush_does_not_wait_for_a_hung_emby(db, monkeypatch):
    from bot import main
    from bot.models import ContentType  # noqa: F401
    from bot.services.library_watcher import LibraryWatcher

    monkeypatch.setattr(main, "_last_sent_at", {})
    monkeypatch.setattr(main, "get_settings", lambda: MagicMock(allowed_tg_ids=[111], admin_tg_ids=[]))
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=9))

    async def hanging_link(provider_id: str):
        await asyncio.sleep(60)

    from bot.services.live_status import LiveStatusService
    from bot.ui.keyboards import Keyboards

    live = LiveStatusService(
        db, AsyncMock(), emby_link=hanging_link, make_markup=Keyboards.watch_in_emby,
    )

    async def notify(text: str, category: str, **extra) -> None:
        await main.deliver_library_notice(bot, db, text, category, MagicMock(), live=live, **extra)

    watcher = LibraryWatcher(MagicMock(), MagicMock(), notify=notify, db=db, live=live)
    record = {
        "id": 1, "sourceTitle": "Пак", "downloadId": "PACK", "date": "2026-10-30T12:01:00Z",
        "series": {"tvdbId": 42},
    }
    watcher._held[("sonarr", "PACK")] = _held([record])

    await asyncio.wait_for(watcher.flush_held(), 2)

    assert bot.send_message.await_count == 1


def _held(records):
    from bot.services.library_watcher import _HeldImport

    held = _HeldImport(first_poll=1)
    held.records.extend(records)
    return held


# --------------------------------------------------- находка 6: перепроверка не объявляется заново
def _service(clock):
    from bot.services.notification_service import NotificationService

    qbit, sender = AsyncMock(), AsyncMock(return_value=True)
    service = NotificationService(qbit, sender, clock=clock)
    service.subscribe_user(111)
    return service, qbit, sender


def _data(rid: int, torrents: dict, *, full: bool = False) -> dict:
    return {"rid": rid, "torrents": torrents, "torrents_removed": [], "full_update": full}


def _old_done() -> dict:
    return {
        "name": "Film", "progress": 1.0, "state": "uploading",
        "completion_on": int((T0 - timedelta(days=3)).timestamp()),
    }


async def _step(service, qbit, clock, rid: int, delta: dict) -> None:
    clock.now += timedelta(seconds=60)
    qbit.get_maindata.return_value = _data(rid, {HASH: delta})
    await service._check_for_completions()


class _Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


async def test_recheck_of_a_finished_torrent_is_not_announced_again():
    clock = _Clock()
    service, qbit, sender = _service(clock)
    qbit.get_maindata.return_value = _data(1, {HASH: _old_done()}, full=True)
    await service._initial_sync()

    await _step(service, qbit, clock, 2, {"progress": 0.3, "state": "checkingUP"})
    await _step(service, qbit, clock, 3, {"progress": 0.8, "state": "checkingUP"})
    await _step(service, qbit, clock, 4, {"progress": 1.0, "state": "uploading"})

    sender.assert_not_awaited()


async def test_first_seen_torrent_in_checking_still_notifies_when_it_finishes():
    clock = _Clock()
    service, qbit, sender = _service(clock)
    qbit.get_maindata.return_value = _data(1, {}, full=True)
    await service._initial_sync()

    await _step(service, qbit, clock, 2, {"name": "Film", "progress": 0.2, "state": "checkingDL"})
    await _step(service, qbit, clock, 3, {"progress": 1.0, "state": "uploading"})

    assert sender.await_count == 1


async def test_torrent_redownloaded_after_a_failed_recheck_notifies_again():
    clock = _Clock()
    service, qbit, sender = _service(clock)
    qbit.get_maindata.return_value = _data(1, {HASH: _old_done()}, full=True)
    await service._initial_sync()

    await _step(service, qbit, clock, 2, {"progress": 0.3, "state": "checkingUP"})
    await _step(service, qbit, clock, 3, {"progress": 0.3, "state": "downloading"})
    await _step(service, qbit, clock, 4, {"progress": 1.0, "state": "uploading"})

    assert sender.await_count == 1


# --------------------------------------------------- перепроверка при старте бота
def _hashes(count: int) -> list[str]:
    return [f"{i:040x}" for i in range(1, count + 1)]


def _raw(state: str, progress: float, completed_on: datetime | None, name: str = "t") -> dict:
    return {
        "name": name, "state": state, "progress": progress, "category": "movies-web", "tags": "",
        "completion_on": int(completed_on.timestamp()) if completed_on else -1,
    }


def _snapshot(torrents: dict, rid: int = 1) -> dict:
    return {"rid": rid, "torrents": torrents, "torrents_removed": [], "full_update": True}


async def _start_in_recheck(state: str, progress: float, completed_on: datetime | None, *, count: int = 3):
    clock = _Clock()
    service, qbit, sender = _service(clock)
    hashes = _hashes(count)
    qbit.get_maindata.return_value = _snapshot({h: _raw(state, progress, completed_on) for h in hashes})
    await service._initial_sync()
    clock.now += timedelta(seconds=60)
    qbit.get_maindata.return_value = {
        "rid": 2, "full_update": False, "torrents_removed": [],
        "torrents": {h: {"state": "uploading", "progress": 1.0} for h in hashes},
    }
    await service._check_for_completions()
    return service, sender


@pytest.mark.parametrize(("state", "progress"), [("checkingResumeData", 0.0), ("checkingUP", 0.5)])
async def test_bot_started_during_a_recheck_does_not_announce_old_torrents(state, progress):
    """Старт бота вместе с qBit: раздачи в проверке уже заканчивались раньше."""
    _service_, sender = await _start_in_recheck(state, progress, T0 - timedelta(days=30), count=50)

    sender.assert_not_awaited()


async def test_never_finished_torrent_in_recheck_at_start_still_notifies():
    _service_, sender = await _start_in_recheck("checkingDL", 0.2, None)

    assert sender.await_count == 3


async def test_first_poll_after_failed_initial_sync_in_recheck_stays_silent():
    clock = _Clock()
    service, qbit, sender = _service(clock)
    qbit.get_maindata.side_effect = ConnectionError("qbit down")
    await service._initial_sync()
    hashes = _hashes(4)
    qbit.get_maindata.side_effect = None
    qbit.get_maindata.return_value = _snapshot(
        {h: _raw("checkingUP", 0.5, T0 - timedelta(days=30)) for h in hashes}
    )
    clock.now += timedelta(seconds=60)
    await service._check_for_completions()
    qbit.get_maindata.return_value = {
        "rid": 3, "full_update": False, "torrents_removed": [],
        "torrents": {h: {"state": "uploading", "progress": 1.0} for h in hashes},
    }
    await service._check_for_completions()

    sender.assert_not_awaited()


# --------------------------------------------------- потолок: сводка вместо потока сообщений
async def _cycle_with_completions(count: int, *, names=None, subscribers=(111,), exclude=None, send=None):
    from bot.services.notification_service import NotificationService

    clock = _Clock()
    qbit = AsyncMock()
    sender = send if send is not None else AsyncMock(return_value=True)
    service = NotificationService(qbit, sender, clock=clock, exclude_chats=exclude)
    for user_id in subscribers:
        service.subscribe_user(user_id)
    hashes = _hashes(count)
    names = names or [f"Film {i}" for i in range(count)]
    qbit.get_maindata.return_value = _snapshot(
        {h: {**_raw("downloading", 0.5, None), "name": n} for h, n in zip(hashes, names)}
    )
    await service._initial_sync()
    clock.now += timedelta(seconds=60)
    qbit.get_maindata.return_value = {
        "rid": 2, "full_update": False, "torrents_removed": [],
        "torrents": {h: {"state": "uploading", "progress": 1.0} for h in hashes},
    }
    await service._check_for_completions()
    return sender


async def test_five_completions_in_a_cycle_go_one_by_one():
    sender = await _cycle_with_completions(5)

    assert sender.await_count == 5
    assert all("Загрузка завершена" in call.args[1] for call in sender.await_args_list)


async def test_six_completions_in_a_cycle_become_one_summary():
    sender = await _cycle_with_completions(6)

    assert sender.await_count == 1
    chat, text = sender.await_args.args
    assert chat == 111
    assert text.startswith("✅ Завершено 6 загрузок: ")
    assert "Film 0, Film 1, Film 2 и ещё 3" in text


async def test_summary_escapes_names_and_stays_within_telegram_limit():
    names = ["<b>" + "x" * 500 + "</b>"] * 50
    sender = await _cycle_with_completions(50, names=names)

    text = sender.await_args.args[1]
    assert sender.await_count == 1
    assert "<b>x" not in text and "&lt;b&gt;" in text
    assert len(text) < 1000


async def test_living_message_exclusions_apply_before_counting():
    """Чат 111 уже ведёт 3 живых сообщения: ему остаются 4 отдельных, чату 222 сводка."""
    hashes = _hashes(7)
    live = set(hashes[:3])

    async def exclude(torrent_hash: str) -> set[int]:
        return {111} if torrent_hash in live else set()

    sender = await _cycle_with_completions(7, subscribers=(111, 222), exclude=exclude)

    by_chat: dict[int, list[str]] = {}
    for call in sender.await_args_list:
        by_chat.setdefault(call.args[0], []).append(call.args[1])
    assert len(by_chat[111]) == 4 and all("Загрузка завершена" in t for t in by_chat[111])
    assert len(by_chat[222]) == 1 and by_chat[222][0].startswith("✅ Завершено 7 загрузок")


async def test_summary_delivery_failure_is_logged_and_does_not_block_other_chats():
    from structlog.testing import capture_logs

    calls: list[int] = []

    async def send(chat: int, text: str):
        calls.append(chat)
        if chat == 111:
            raise RuntimeError("telegram down")
        return True

    with capture_logs() as logs:
        await _cycle_with_completions(6, subscribers=(111, 222), send=send)

    assert calls == [111, 222]
    assert any(e["event"] == "notification_send_raised" for e in logs)

    with capture_logs() as logs:
        await _cycle_with_completions(6, send=AsyncMock(return_value=False))
    assert any(e["event"] == "notification_not_delivered" for e in logs)
