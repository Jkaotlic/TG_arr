"""Round-7 audit regressions (2026-07-30).

Every test here failed before its fix. See analysis/audit-2026-07-30.md.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from bot.clients.scryer import ScryerClient
from bot.models import ScryerImportRecord
from bot.services.library_watcher import LibraryWatcher


def _collector():
    """An async notify callable plus the list it appends to."""
    sent: list[str] = []

    async def notify(text: str) -> None:
        sent.append(text)

    return sent, notify


def _record(**kw) -> ScryerImportRecord:
    base = dict(
        id="rec-1",
        source_title="Michael 2026 BluRay 1080p DD 5 1 x264-BHDStudio",
        title_id="214f6ef4",
        status="COMPLETED",
        decision="IMPORTED",
    )
    base.update(kw)
    return ScryerImportRecord(**base)


def _watcher(notify, *, records=None, transfers=None):
    scryer = MagicMock()
    scryer.get_import_history = AsyncMock(return_value=list(records or []))
    scryer.get_download_queue = AsyncMock(return_value=[])
    slskd = MagicMock()
    slskd.get_transfers = AsyncMock(return_value=list(transfers or []))
    return LibraryWatcher(
        notify,
        get_scryer=AsyncMock(return_value=scryer),
        get_slskd=AsyncMock(return_value=slskd),
    ), scryer, slskd


# ---------------------------------------------------------------- BUG-01
@pytest.mark.asyncio
async def test_import_that_finished_between_polls_is_announced():
    """BUG-01: the old watcher diffed the *active* queue, so an import that
    completed and left the queue between two polls was never announced — which
    is what actually happened to every real import on the live instance.
    """
    sent, notify = _collector()
    watcher, scryer, _ = _watcher(notify)

    # First poll: nothing in the journal yet, nothing to say.
    await watcher.poll()
    assert sent == []

    # The download completes and is imported entirely between the two polls —
    # it never appears in downloadQueue at all.
    scryer.get_import_history.return_value = [_record()]
    await watcher.poll()

    assert len(sent) == 1
    assert "Michael" in sent[0]


@pytest.mark.asyncio
async def test_rejected_import_is_reported_with_its_reason():
    """A rejected import is the case the user most needs to hear about: the
    download finished and nothing landed. The live journal had several.
    """
    sent, notify = _collector()
    watcher, scryer, _ = _watcher(notify)
    await watcher.poll()

    scryer.get_import_history.return_value = [
        _record(
            id="rec-2",
            source_title="X-Men.97.S02E07.1080p.DSNP.WEB-DL",
            status="FAILED",
            decision="REJECTED",
            skip_reason="POLICY_MISMATCH",
            error_message="0 imported, 1 skipped, 11 rejected",
        )
    ]
    await watcher.poll()

    assert len(sent) == 1
    assert "X-Men" in sent[0]
    assert "POLICY_MISMATCH" in sent[0] or "rejected" in sent[0]


@pytest.mark.asyncio
async def test_unfinished_import_is_not_announced_yet():
    sent, notify = _collector()
    watcher, scryer, _ = _watcher(notify)
    await watcher.poll()

    scryer.get_import_history.return_value = [_record(status="RUNNING", decision=None)]
    await watcher.poll()

    assert sent == []


@pytest.mark.asyncio
async def test_same_record_is_announced_once():
    sent, notify = _collector()
    watcher, scryer, _ = _watcher(notify)
    await watcher.poll()
    scryer.get_import_history.return_value = [_record()]

    await watcher.poll()
    await watcher.poll()
    await watcher.poll()

    assert len(sent) == 1


@pytest.mark.asyncio
async def test_first_poll_after_restart_stays_quiet():
    """State lives in memory; a restart must not replay the whole journal."""
    sent, notify = _collector()
    watcher, _scryer, _ = _watcher(
        notify,
        records=[_record(id=f"old-{i}") for i in range(10)],
    )

    await watcher.poll()

    assert sent == []


# ---------------------------------------------------------------- BUG-03
def _client() -> ScryerClient:
    return ScryerClient("http://scryer:8088", "u", "p")


@pytest.mark.asyncio
async def test_concurrent_callers_trigger_one_login():
    """BUG-03: the token expires once a day, and every caller that notices at
    the same moment used to log in separately — Scryer rate-limits `login`.
    """
    client = _client()
    logins = 0

    async def fake_post(query, variables=None, *, with_auth=True, timeout=None):
        nonlocal logins
        if "mutation Login" in query:
            logins += 1
            await asyncio.sleep(0.01)  # let the other callers pile up on the lock
            return {"data": {"login": {"token": f"tok-{logins}", "expiresAt": None}}}
        return {"data": {"ok": True}}

    with patch.object(client, "_post_graphql", side_effect=fake_post):
        await asyncio.gather(*(client.execute("query Q { ok }") for _ in range(5)))

    assert logins == 1


@pytest.mark.asyncio
async def test_a_rejected_token_still_forces_a_fresh_login():
    """The de-duplication must not swallow a re-login after a rejection: the
    cached token can look fresh and still be refused by the server.
    """
    client = _client()
    logins = 0
    attempts = 0

    async def fake_post(query, variables=None, *, with_auth=True, timeout=None):
        nonlocal logins, attempts
        if "mutation Login" in query:
            logins += 1
            return {"data": {"login": {"token": f"tok-{logins}", "expiresAt": None}}}
        attempts += 1
        if attempts == 1:
            return {"errors": [{"message": "Unauthorized"}]}
        return {"data": {"ok": True}}

    with patch.object(client, "_post_graphql", side_effect=fake_post):
        result = await client.execute("query Q { ok }")

    assert result == {"ok": True}
    assert logins == 2


# ---------------------------------------------------------------- BUG-04
def _counting_http(*, fail_times: int, payload: dict) -> tuple[AsyncMock, list[int]]:
    """A stand-in httpx client that counts requests and can time out first.

    Patched at the transport level on purpose: retries live in a decorator on
    `_request`, so patching `_request` itself would remove the very behaviour
    under test.
    """
    calls = [0]

    async def request(**kw):
        calls[0] += 1
        if calls[0] <= fail_times:
            raise httpx.TimeoutException("too slow")
        response = MagicMock()
        response.status_code = 200
        response.text = json.dumps(payload)
        response.json.return_value = payload
        return response

    http = AsyncMock()
    http.request = AsyncMock(side_effect=request)
    return http, calls


@pytest.mark.asyncio
async def test_mutations_are_not_retried():
    """BUG-04: a timed-out mutation that actually succeeded came back as
    CONFLICT on the retry, so the bot reported failure for its own success.
    """
    client = _client()
    client._token = "tok"
    client._token_expires_at = None
    http, calls = _counting_http(fail_times=99, payload={})

    with patch.object(client, "_get_client", AsyncMock(return_value=http)):
        with pytest.raises(Exception):
            await client.queue_existing_title_download(
                title_id="t1", candidate_token="ct"
            )

    assert calls[0] == 1, f"mutation was sent {calls[0]} times"


@pytest.mark.asyncio
async def test_read_only_queries_keep_their_retries():
    """Retrying a read is free and hides a flaky Wi-Fi hop — keep it."""
    client = _client()
    client._token = "tok"
    client._token_expires_at = None
    http, calls = _counting_http(
        fail_times=1, payload={"data": {"importHistory": []}}
    )

    with patch.object(client, "_get_client", AsyncMock(return_value=http)):
        records = await client.get_import_history(limit=5)

    assert records == []
    assert calls[0] == 2


# ---------------------------------------------------------------- BUG-06
@pytest.mark.asyncio
async def test_non_json_body_does_not_raise_a_decode_error():
    """BUG-06: a proxy answering 200 with an HTML error page surfaced as a raw
    JSONDecodeError instead of a domain error.
    """
    from bot.clients.base import BaseAPIClient

    client = BaseAPIClient("http://svc", "key", "svc")
    response = MagicMock()
    response.status_code = 200
    response.text = "<html>gateway error</html>"
    response.json.side_effect = json.JSONDecodeError("boom", "<html>", 0)

    http = AsyncMock()
    http.request = AsyncMock(return_value=response)

    with patch.object(client, "_get_client", AsyncMock(return_value=http)):
        result = await client.get("/anything")

    assert result == {}


# ---------------------------------------------------------------- LANG-01
def _release(**kw):
    from bot.models import SearchResult

    base = dict(
        guid="g1",
        title="Show.S02E07.1080p.WEB-DL.DDP5.1.ENG.Atmos.ITA.H264-GRP",
        size=3_000_000_000,
        scryer_score=500,
        scryer_allowed=True,
    )
    base.update(kw)
    return SearchResult(**base)


def test_release_card_warns_when_the_policy_found_no_english_audio():
    """LANG-01: the language policy only *penalises* a release without English
    audio — it does not block it — so the release card has to say so. The user
    asked why Italian audio keeps landing despite the profiles being set.
    """
    from bot.ui.formatters import Formatters

    result = _release(policy_codes=["russian_subtitles_bonus"])

    text = Formatters.format_release_details(result)

    assert "англ" in text.lower(), text


def test_release_card_confirms_english_audio_when_the_policy_scored_it():
    from bot.ui.formatters import Formatters

    result = _release(policy_codes=["english_audio_bonus", "russian_subtitles_bonus"])

    text = Formatters.format_release_details(result)

    assert "🔊" in text


def test_release_card_stays_quiet_when_the_policy_said_nothing():
    """No scoring log (e.g. a session from before this change) must not turn
    into a false "no English audio" warning.
    """
    from bot.ui.formatters import Formatters

    text = Formatters.format_release_details(_release())

    assert "англ" not in text.lower()


def test_penalties_are_surfaced_with_their_weight():
    """A -1000 penalty is the difference between "fine" and "last resort"."""
    from bot.ui.formatters import Formatters

    result = _release(policy_codes=["russian_audio_without_english"])

    text = Formatters.format_release_details(result)

    assert "англ" in text.lower()


# ---------------------------------------------------------------- BUG-05
def _catalog_title(name: str, title_id: str):
    t = MagicMock()
    t.title = name
    t.scryer_id = title_id
    t.year = 2013
    t.monitored = True
    t.episodes_total = 0
    t.quality_tier = None
    return t


@pytest.mark.asyncio
async def test_title_command_asks_which_one_when_several_match():
    """BUG-05: `/title Frozen` silently picked the first of Frozen / Frozen
    Fever / Frozen II — and offered a delete button on it.
    """
    from bot.handlers import titles

    message = AsyncMock()
    message.text = "/title Frozen"
    status = AsyncMock()
    message.answer = AsyncMock(return_value=status)

    scryer = AsyncMock()
    scryer.get_titles = AsyncMock(return_value=(
        [
            _catalog_title("Frozen", "id-1"),
            _catalog_title("Frozen Fever", "id-2"),
            _catalog_title("Frozen II", "id-3"),
        ],
        3,
        False,
    ))

    with patch.object(titles, "get_scryer", AsyncMock(return_value=scryer)):
        await titles.cmd_title(message, db_user=MagicMock(), db=AsyncMock())

    text = status.edit_text.await_args.args[0]
    markup = status.edit_text.await_args.kwargs.get("reply_markup")
    assert "Frozen Fever" in str(markup), "the other matches must be offered"
    assert "🗑" not in str(markup), "a delete button must not appear before a title is chosen"
    assert "Frozen" in text


@pytest.mark.asyncio
async def test_title_command_goes_straight_to_the_card_for_one_match():
    from bot.handlers import titles

    message = AsyncMock()
    message.text = "/title Paw Patrol"
    status = AsyncMock()
    message.answer = AsyncMock(return_value=status)

    scryer = AsyncMock()
    scryer.get_titles = AsyncMock(
        return_value=([_catalog_title("Paw Patrol", "id-9")], 1, False)
    )

    with patch.object(titles, "get_scryer", AsyncMock(return_value=scryer)):
        await titles.cmd_title(message, db_user=MagicMock(), db=AsyncMock())

    markup = str(status.edit_text.await_args.kwargs.get("reply_markup"))
    assert "🗑" in markup, "a single match should offer the actions directly"


# ---------------------------------------------------------------- BUG-02
def _transfer(state: str, filename: str = "01 - Track.flac"):
    t = MagicMock()
    t.username = "peer"
    t.filename = filename
    t.state = state
    t.is_errored = "Errored" in state
    return t


@pytest.mark.asyncio
async def test_cancelled_music_download_is_not_reported_as_done():
    """BUG-02: "the key vanished from the active list" was treated as success,
    so a cancelled transfer was announced as downloaded.
    """
    sent, notify = _collector()
    watcher, _s, slskd = _watcher(
        notify,
        transfers=[_transfer("InProgress")],
    )
    await watcher.poll()

    slskd.get_transfers.return_value = [_transfer("Completed, Cancelled")]
    await watcher.poll()

    assert not any("скачано" in m for m in sent)


@pytest.mark.asyncio
async def test_succeeded_music_download_is_reported():
    sent, notify = _collector()
    watcher, _s, slskd = _watcher(
        notify,
        transfers=[_transfer("InProgress")],
    )
    await watcher.poll()

    slskd.get_transfers.return_value = [_transfer("Completed, Succeeded")]
    await watcher.poll()

    assert len(sent) == 1
    assert "скачано" in sent[0]


@pytest.mark.asyncio
async def test_empty_transfer_list_does_not_mass_announce():
    """A slskd restart (or a cleared queue) returns an empty list — that is not
    proof that everything in flight succeeded.
    """
    sent, notify = _collector()
    watcher, _s, slskd = _watcher(
        notify,
        transfers=[_transfer("InProgress", f"track-{i}.flac") for i in range(5)],
    )
    await watcher.poll()

    slskd.get_transfers.return_value = []
    await watcher.poll()

    assert sent == []

