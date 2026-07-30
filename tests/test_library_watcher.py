""""It's in the library now" notifications (2026-07-29, reworked 2026-07-30).

Until now the only completion notice came from qBittorrent, i.e. "the torrent
finished" — not "Scryer imported it and you can watch it". Music never got one
at all, because slskd is a separate download client qBittorrent knows nothing
about.

Scryer has no generic webhook (its only notification channel type is
`mediabrowser`), so this polls. The *sources* were wrong in the first cut and
were replaced (audit 2026-07-30): imports come from the permanent
`importHistory` journal rather than the active queue a finished import leaves,
and music outcomes come from terminal transfer states rather than from a key
disappearing. See tests/test_audit_r7.py for those regressions.
"""

from unittest.mock import AsyncMock

import pytest

from bot.models import ContentType, ScryerImportRecord, SlskdTransfer
from bot.services.library_watcher import LibraryWatcher


def _record(**overrides) -> ScryerImportRecord:
    data = dict(
        id="r1", source_title="Apex", title_id="t1", content_type=ContentType.MOVIE,
        status="COMPLETED", decision="IMPORTED",
    )
    data.update(overrides)
    return ScryerImportRecord(**data)


def _transfer(**overrides) -> SlskdTransfer:
    data = dict(username="peer", filename="01 - Battery.flac", state="InProgress",
                size=100, transferred=50)
    data.update(overrides)
    return SlskdTransfer(**data)


def _watcher(sent: list, scryer=None, slskd=None) -> LibraryWatcher:
    async def notify(text: str) -> None:
        sent.append(text)

    return LibraryWatcher(notify=notify, get_scryer=scryer, get_slskd=slskd)


# ------------------------------------------------------------------- imports
@pytest.mark.asyncio
async def test_import_completion_is_announced_once():
    sent: list[str] = []
    scryer = AsyncMock()
    watcher = _watcher(sent, scryer=AsyncMock(return_value=scryer))

    scryer.get_import_history = AsyncMock(return_value=[])
    await watcher.poll()  # seeds an empty journal

    scryer.get_import_history = AsyncMock(return_value=[_record()])
    await watcher.poll()
    await watcher.poll()  # already announced

    assert len(sent) == 1
    assert "Apex" in sent[0]


@pytest.mark.asyncio
async def test_history_present_on_first_sight_is_not_announced():
    """First poll after a restart must not replay history at the user."""
    sent: list[str] = []
    scryer = AsyncMock()
    scryer.get_import_history = AsyncMock(return_value=[_record()])
    watcher = _watcher(sent, scryer=AsyncMock(return_value=scryer))

    await watcher.poll()

    assert sent == []


@pytest.mark.asyncio
async def test_a_failed_import_is_announced_differently():
    sent: list[str] = []
    scryer = AsyncMock()
    watcher = _watcher(sent, scryer=AsyncMock(return_value=scryer))

    scryer.get_import_history = AsyncMock(return_value=[])
    await watcher.poll()

    scryer.get_import_history = AsyncMock(return_value=[
        _record(status="FAILED", decision="REJECTED", error_message="no matching episode")
    ])
    await watcher.poll()

    assert len(sent) == 1
    assert "no matching episode" in sent[0]


@pytest.mark.asyncio
async def test_a_burst_is_announced_oldest_first():
    """importHistory is newest-first; the user should read events in order."""
    sent: list[str] = []
    scryer = AsyncMock()
    watcher = _watcher(sent, scryer=AsyncMock(return_value=scryer))
    scryer.get_import_history = AsyncMock(return_value=[])
    await watcher.poll()

    scryer.get_import_history = AsyncMock(return_value=[
        _record(id="new", source_title="Second"),
        _record(id="old", source_title="First"),
    ])
    await watcher.poll()

    assert [("First" in sent[0]), ("Second" in sent[1])] == [True, True]


@pytest.mark.asyncio
async def test_the_title_name_is_escaped():
    sent: list[str] = []
    scryer = AsyncMock()
    watcher = _watcher(sent, scryer=AsyncMock(return_value=scryer))

    scryer.get_import_history = AsyncMock(return_value=[])
    await watcher.poll()
    scryer.get_import_history = AsyncMock(
        return_value=[_record(source_title="Tom & Jerry <hd>")]
    )
    await watcher.poll()

    assert "<hd>" not in sent[0]
    assert "&lt;hd&gt;" in sent[0]


# --------------------------------------------------------------------- music
@pytest.mark.asyncio
async def test_finished_soulseek_transfer_is_announced():
    sent: list[str] = []
    slskd = AsyncMock()
    watcher = _watcher(sent, slskd=AsyncMock(return_value=slskd))

    slskd.get_transfers = AsyncMock(return_value=[_transfer()])
    await watcher.poll()

    slskd.get_transfers = AsyncMock(
        return_value=[_transfer(state="Completed, Succeeded", transferred=100)]
    )
    await watcher.poll()

    assert len(sent) == 1
    assert "Battery" in sent[0]


@pytest.mark.asyncio
async def test_an_errored_transfer_is_not_reported_as_success():
    sent: list[str] = []
    slskd = AsyncMock()
    watcher = _watcher(sent, slskd=AsyncMock(return_value=slskd))

    slskd.get_transfers = AsyncMock(return_value=[_transfer()])
    await watcher.poll()
    slskd.get_transfers = AsyncMock(
        return_value=[_transfer(state="Completed, Errored", transferred=10)]
    )
    await watcher.poll()

    assert len(sent) == 1
    assert "ошибк" in sent[0].lower() or "не удалось" in sent[0].lower()


# ------------------------------------------------------------------ failures
@pytest.mark.asyncio
async def test_one_backend_failing_does_not_stop_the_other():
    sent: list[str] = []
    scryer = AsyncMock()
    scryer.get_import_history = AsyncMock(side_effect=RuntimeError("scryer down"))
    slskd = AsyncMock()
    slskd.get_transfers = AsyncMock(return_value=[_transfer()])
    watcher = _watcher(sent, scryer=AsyncMock(return_value=scryer), slskd=AsyncMock(return_value=slskd))

    await watcher.poll()  # must not raise
    slskd.get_transfers = AsyncMock(
        return_value=[_transfer(state="Completed, Succeeded")]
    )
    await watcher.poll()

    assert len(sent) == 1


@pytest.mark.asyncio
async def test_unconfigured_backends_are_skipped():
    sent: list[str] = []
    watcher = _watcher(sent, scryer=AsyncMock(return_value=None), slskd=AsyncMock(return_value=None))

    await watcher.poll()

    assert sent == []


@pytest.mark.asyncio
async def test_a_failing_notify_does_not_lose_later_events():
    """A Telegram blip must not wedge the watcher's state."""
    calls = []

    async def flaky(text: str) -> None:
        calls.append(text)
        if len(calls) == 1:
            raise Exception("telegram down")

    scryer = AsyncMock()
    watcher = LibraryWatcher(notify=flaky, get_scryer=AsyncMock(return_value=scryer))

    scryer.get_import_history = AsyncMock(return_value=[])
    await watcher.poll()
    scryer.get_import_history = AsyncMock(return_value=[_record()])
    await watcher.poll()

    scryer.get_import_history = AsyncMock(
        return_value=[_record(id="r2", source_title="Other"), _record()]
    )
    await watcher.poll()

    assert len(calls) == 2
