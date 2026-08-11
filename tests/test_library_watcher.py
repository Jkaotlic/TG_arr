""""It's in the library now" notifications (2026-07-29, reworked 2026-07-30,
rewired for *arr 2026-08-10 — Tasks 14/15).

Until now the only completion notice came from qBittorrent, i.e. "the torrent
finished" — not "it was imported and you can watch it". Music never got one
at all, because slskd is a separate download client qBittorrent knows nothing
about.

Rollback 2026-08-10: imports now come from Radarr's and Sonarr's own history
journal (`ArrBaseClient.get_history`, `GET {prefix}/history?eventType=
downloadFolderImported`) instead of the previous backend's `importHistory`.
Live-verified response shape (2026-08-10, read-only probe against the real
Radarr/Sonarr): `GET /api/v3/history?eventType=3&pageSize=N` returns
`{"records": [...]}`, each record carrying at least `id` (int, service-local),
`sourceTitle`, `date` (ISO string), `eventType` (the string
"downloadFolderImported" in the body, unlike the numeric `3` the query param
needs — see `ArrBaseClient._HISTORY_EVENT_TYPE_CODES`).

The two principles that made polling necessary in the first place are
unchanged:
- **Imports come from the history journal**, not from the active download
  queue. A finished import leaves the queue, so diffing the queue only
  catches an import if a poll happens to land inside that window — on the
  live instance it never did, and every real import went unannounced (audit
  2026-07-30, BUG-01).
- **Music comes from terminal transfer states**, not from a key disappearing.
  "It vanished from the active list" also describes a cancelled transfer, and
  a slskd restart makes *everything* vanish at once (BUG-02).

A third, newly relevant for the history-journal source: a (re)start must not
replay *arr's existing history as if it just landed — `poll_once()` itself
reports a fresh watcher's first read verbatim (that is the raw detection
primitive, and what task-14's own brief tests), but `poll()` — what
`bot.main._library_watch` actually drives — silently seeds on its first cycle
instead of announcing, so a process restart doesn't re-blast the last
`_HISTORY_PAGE_SIZE` imports per service at every user.
"""

from unittest.mock import AsyncMock

import pytest

from bot.models import SlskdTransfer
from bot.services.library_watcher import LibraryWatcher


def _imported(**overrides) -> dict:
    """One `downloadFolderImported` history record, live-shaped."""
    data = dict(
        id=1, eventType="downloadFolderImported", sourceTitle="Dune 2021 2160p",
        date="2026-08-11T07:06:02Z",
    )
    data.update(overrides)
    return data


def _transfer(**overrides) -> SlskdTransfer:
    data = dict(username="peer", filename="01 - Battery.flac", state="InProgress",
                size=100, transferred=50)
    data.update(overrides)
    return SlskdTransfer(**data)


def _services(radarr_history=None, sonarr_history=None):
    radarr, sonarr = AsyncMock(), AsyncMock()
    radarr.get_history.return_value = list(radarr_history or [])
    sonarr.get_history.return_value = list(sonarr_history or [])
    return radarr, sonarr


# --------------------------------------------------------------- poll_once
@pytest.mark.asyncio
async def test_library_watcher_reports_imported_media():
    """The watcher fires when media actually lands, not when it is queued."""
    radarr, sonarr = _services(radarr_history=[
        {"id": 1, "eventType": "downloadFolderImported", "sourceTitle": "Dune 2021 2160p"},
    ])

    watcher = LibraryWatcher(radarr, sonarr)
    landed = await watcher.poll_once()

    assert [item["sourceTitle"] for item in landed] == ["Dune 2021 2160p"]


@pytest.mark.asyncio
async def test_library_watcher_does_not_re_report_the_same_import():
    radarr, sonarr = _services(radarr_history=[
        {"id": 1, "eventType": "downloadFolderImported", "sourceTitle": "Dune"},
    ])

    watcher = LibraryWatcher(radarr, sonarr)
    await watcher.poll_once()
    second = await watcher.poll_once()

    assert second == []


@pytest.mark.asyncio
async def test_seen_ids_are_scoped_per_service():
    """Radarr and Sonarr both hand out their own autoincrementing `id` — the
    same integer legitimately means two different records, so the dedup key
    must include which service it came from."""
    radarr, sonarr = _services(
        radarr_history=[_imported(id=1, sourceTitle="Movie")],
        sonarr_history=[_imported(id=1, sourceTitle="Episode")],
    )

    watcher = LibraryWatcher(radarr, sonarr)
    landed = await watcher.poll_once()

    assert {item["sourceTitle"] for item in landed} == {"Movie", "Episode"}


@pytest.mark.asyncio
async def test_a_mixed_burst_is_returned_oldest_first():
    """A movie+episode burst across both services reads in the order it
    actually happened, by each record's own `date` — not per-client return
    order."""
    radarr, sonarr = _services(
        radarr_history=[_imported(id=1, sourceTitle="Second", date="2026-08-11T08:00:00Z")],
        sonarr_history=[_imported(id=1, sourceTitle="First", date="2026-08-11T07:00:00Z")],
    )

    watcher = LibraryWatcher(radarr, sonarr)
    landed = await watcher.poll_once()

    assert [item["sourceTitle"] for item in landed] == ["First", "Second"]


@pytest.mark.asyncio
async def test_one_service_failing_does_not_stop_the_other():
    radarr, sonarr = _services(sonarr_history=[_imported(sourceTitle="Ep")])
    radarr.get_history.side_effect = RuntimeError("radarr down")

    watcher = LibraryWatcher(radarr, sonarr)
    landed = await watcher.poll_once()  # must not raise

    assert [item["sourceTitle"] for item in landed] == ["Ep"]


# -------------------------------------------------------------------- poll
@pytest.mark.asyncio
async def test_poll_does_not_replay_pre_existing_history_on_the_first_cycle():
    """A bot restart must not replay *arr's already-known import history as
    new landings. `poll_once()` (above) reports a fresh watcher's first read
    verbatim by design; the anti-spam-on-restart guarantee therefore lives
    one layer up, at `poll()` — what the real background task drives."""
    sent: list[str] = []

    async def notify(text: str) -> None:
        sent.append(text)

    radarr, sonarr = _services(radarr_history=[_imported(id=1, sourceTitle="Old Movie")])
    watcher = LibraryWatcher(radarr, sonarr, notify=notify)

    await watcher.poll()
    assert sent == []

    radarr.get_history.return_value = [
        _imported(id=1, sourceTitle="Old Movie"),
        _imported(id=2, sourceTitle="New Movie", date="2026-08-11T09:00:00Z"),
    ]
    await watcher.poll()

    assert len(sent) == 1
    assert "New Movie" in sent[0]


@pytest.mark.asyncio
async def test_the_title_is_escaped():
    sent: list[str] = []

    async def notify(text: str) -> None:
        sent.append(text)

    radarr, sonarr = _services()
    watcher = LibraryWatcher(radarr, sonarr, notify=notify)
    await watcher.poll()  # seed

    radarr.get_history.return_value = [_imported(sourceTitle="Tom & Jerry <hd>")]
    await watcher.poll()

    assert "<hd>" not in sent[0]
    assert "&lt;hd&gt;" in sent[0]


# --------------------------------------------------------------------- music
@pytest.mark.asyncio
async def test_finished_soulseek_transfer_is_announced():
    sent: list[str] = []

    async def notify(text: str) -> None:
        sent.append(text)

    radarr, sonarr = _services()
    slskd = AsyncMock()
    slskd.get_transfers = AsyncMock(return_value=[_transfer()])
    watcher = LibraryWatcher(radarr, sonarr, notify=notify, get_slskd=AsyncMock(return_value=slskd))

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

    async def notify(text: str) -> None:
        sent.append(text)

    radarr, sonarr = _services()
    slskd = AsyncMock()
    slskd.get_transfers = AsyncMock(return_value=[_transfer()])
    watcher = LibraryWatcher(radarr, sonarr, notify=notify, get_slskd=AsyncMock(return_value=slskd))

    await watcher.poll()
    slskd.get_transfers = AsyncMock(
        return_value=[_transfer(state="Completed, Errored", transferred=10)]
    )
    await watcher.poll()

    assert len(sent) == 1
    assert "ошибк" in sent[0].lower() or "не удалось" in sent[0].lower()


@pytest.mark.asyncio
async def test_a_cancelled_transfer_is_not_reported_as_done():
    """BUG-02: "the key vanished from the active list" was treated as
    success, so a cancelled transfer used to be announced as downloaded."""
    sent: list[str] = []

    async def notify(text: str) -> None:
        sent.append(text)

    radarr, sonarr = _services()
    slskd = AsyncMock()
    slskd.get_transfers = AsyncMock(return_value=[_transfer()])
    watcher = LibraryWatcher(radarr, sonarr, notify=notify, get_slskd=AsyncMock(return_value=slskd))

    await watcher.poll()
    slskd.get_transfers = AsyncMock(return_value=[_transfer(state="Completed, Cancelled")])
    await watcher.poll()

    assert not any("скачано" in m for m in sent)


@pytest.mark.asyncio
async def test_an_empty_transfer_list_does_not_mass_announce():
    """A slskd restart (or a cleared queue) returns an empty list — that is
    not proof every in-flight transfer succeeded."""
    sent: list[str] = []

    async def notify(text: str) -> None:
        sent.append(text)

    radarr, sonarr = _services()
    slskd = AsyncMock()
    slskd.get_transfers = AsyncMock(
        return_value=[_transfer(filename=f"track-{i}.flac") for i in range(5)]
    )
    watcher = LibraryWatcher(radarr, sonarr, notify=notify, get_slskd=AsyncMock(return_value=slskd))

    await watcher.poll()
    slskd.get_transfers = AsyncMock(return_value=[])
    await watcher.poll()

    assert sent == []


@pytest.mark.asyncio
async def test_unconfigured_slskd_is_skipped():
    radarr, sonarr = _services()
    watcher = LibraryWatcher(radarr, sonarr, get_slskd=None)

    await watcher.poll()  # must not raise


# ------------------------------------------------------------------ failures
@pytest.mark.asyncio
async def test_one_backend_failing_does_not_stop_the_other():
    sent: list[str] = []

    async def notify(text: str) -> None:
        sent.append(text)

    radarr, sonarr = _services()
    radarr.get_history.side_effect = RuntimeError("radarr down")
    slskd = AsyncMock()
    slskd.get_transfers = AsyncMock(return_value=[_transfer()])
    watcher = LibraryWatcher(radarr, sonarr, notify=notify, get_slskd=AsyncMock(return_value=slskd))

    await watcher.poll()  # must not raise
    slskd.get_transfers = AsyncMock(
        return_value=[_transfer(state="Completed, Succeeded")]
    )
    await watcher.poll()

    assert len(sent) == 1


@pytest.mark.asyncio
async def test_a_failing_notify_does_not_lose_later_events():
    """A Telegram blip must not wedge the watcher's state."""
    calls = []

    async def flaky(text: str) -> None:
        calls.append(text)
        if len(calls) == 1:
            raise Exception("telegram down")

    radarr, sonarr = _services()
    watcher = LibraryWatcher(radarr, sonarr, notify=flaky)

    await watcher.poll()  # seed, quiet
    radarr.get_history.return_value = [_imported(id=1, sourceTitle="First")]
    await watcher.poll()

    radarr.get_history.return_value = [
        _imported(id=1, sourceTitle="First"),
        _imported(id=2, sourceTitle="Other", date="2026-08-11T09:00:00Z"),
    ]
    await watcher.poll()

    assert len(calls) == 2


# ------------------------------------------------------------------ delivery
@pytest.mark.asyncio
async def test_delivery_is_logged_with_the_number_of_recipients():
    """Only failures were logged, so silence meant both "delivered" and
    "never sent" — the question was unanswerable after the fact (audit
    2026-07-31)."""
    import structlog
    import structlog.testing

    from bot.main import send_library_notification

    bot = AsyncMock()

    with structlog.testing.capture_logs() as logs:
        await send_library_notification(
            bot, [111, 222], "✅ <b>Apex</b> — в библиотеке.", structlog.get_logger()
        )

    sent = [e for e in logs if e.get("event") == "library_notify_sent"]
    assert len(sent) == 1
    assert sent[0]["recipients"] == 2
    assert sent[0]["failed"] == 0


@pytest.mark.asyncio
async def test_a_recipient_that_rejects_the_message_is_not_counted_as_delivered():
    """A user who blocked the bot must not inflate the delivered count —
    otherwise the log would claim a delivery that never happened."""
    import structlog
    import structlog.testing

    from bot.main import send_library_notification

    bot = AsyncMock()
    bot.send_message = AsyncMock(side_effect=[Exception("bot was blocked"), None])

    with structlog.testing.capture_logs() as logs:
        await send_library_notification(
            bot, [111, 222], "✅ <b>Apex</b> — в библиотеке.", structlog.get_logger()
        )

    sent = [e for e in logs if e.get("event") == "library_notify_sent"]
    assert len(sent) == 1
    assert sent[0]["recipients"] == 1
    assert sent[0]["failed"] == 1
    assert [e["user_id"] for e in logs if e.get("event") == "library_notify_send_failed"] == [111]
