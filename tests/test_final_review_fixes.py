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
