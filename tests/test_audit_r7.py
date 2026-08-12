"""Round-7 audit regressions (2026-07-30), repaired for the 2026-08-10 *arr
rollback (Tasks 14/15).

Every test here failed before its fix. See analysis/audit-2026-07-30.md.

Repair notes (Task 15): several sections tested Scryer-specific mechanics
(GraphQL login de-duplication, its `importHistory`/`systemHealth` shapes, its
composite grab-fallback flow, its `scoringLog`-derived language verdict) that
have no *arr equivalent, or whose *arr-equivalent regression coverage was
consolidated into the sibling test files that now own that surface, rather
than duplicated here. See the task-14/15 report for the itemised list of what
moved where and why.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# ---------------------------------------------------------------- BUG-04
# Repaired: the original pair drove this through ScryerClient's GraphQL
# mutation/query split. The underlying guarantee — a non-idempotent write
# must never be silently retried, because a second attempt could repeat a
# side effect (queuing the same release twice) — is now `_post_no_retry`
# (no @retry decorator, by construction) vs. `get`/`_request` (tenacity
# retry). *arr's own grab_release/push_release rely on exactly this split
# (see ArrBaseClient.grab_release's docstring). Driven through the shared
# BaseAPIClient directly rather than any one *arr client, since the
# guarantee lives at that layer.
@pytest.mark.asyncio
async def test_post_no_retry_does_not_retry_a_timed_out_mutation():
    """BUG-04: a timed-out mutation that actually succeeded must not be
    retried — the previous backend's plugin queued a duplicate download when
    a timed-out request that had, in fact, already been accepted was resent.
    """
    from bot.clients.base import BaseAPIClient, ServiceConnectionError

    client = BaseAPIClient("http://svc", "key", "svc")
    calls = [0]

    async def request(**kw):
        calls[0] += 1
        raise httpx.TimeoutException("too slow")

    http = AsyncMock()
    http.request = AsyncMock(side_effect=request)

    with patch.object(client, "_get_client", AsyncMock(return_value=http)):
        with pytest.raises(ServiceConnectionError):
            await client._post_no_retry("/api/v3/release", json_data={"guid": "g"})

    assert calls[0] == 1, f"mutation was sent {calls[0]} times"


@pytest.mark.asyncio
async def test_get_keeps_its_retries_on_transient_timeout():
    """Reads are safe to retry — a flaky Wi-Fi hop recovering on the second
    attempt must not surface as a hard failure."""
    from bot.clients.base import BaseAPIClient

    client = BaseAPIClient("http://svc", "key", "svc")
    calls = [0]
    payload = {"ok": True}

    async def request(**kw):
        calls[0] += 1
        if calls[0] == 1:
            raise httpx.TimeoutException("too slow")
        response = MagicMock()
        response.status_code = 200
        response.text = json.dumps(payload)
        response.json.return_value = payload
        return response

    http = AsyncMock()
    http.request = AsyncMock(side_effect=request)

    with patch.object(client, "_get_client", AsyncMock(return_value=http)):
        result = await client.get("/anything")

    assert result == payload
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


# ---------------------------------------------------------------- SEC-01
@pytest.mark.asyncio
async def test_deluser_does_not_claim_success_for_an_env_allowlisted_user():
    """SEC-01: authorization is env-allowlist OR db-allowlist, so removing the
    db row does nothing for a user listed in ALLOWED_TG_IDS — yet the bot
    replied "доступ отозван". An admin would believe access was revoked while
    the user kept full access until someone edited .env and restarted.
    """
    from bot.handlers import users

    message = AsyncMock()
    message.text = "/deluser 777"
    message.from_user = MagicMock(id=1)
    db = AsyncMock()

    settings = MagicMock()
    settings.is_user_allowed.return_value = True  # 777 is in ALLOWED_TG_IDS

    with patch.object(users, "get_settings", return_value=settings):
        await users.cmd_deluser(message, db=db, is_admin=True)

    reply = message.answer.await_args.args[0]
    assert "ALLOWED_TG_IDS" in reply, reply
    assert "отозван</code>" not in reply


@pytest.mark.asyncio
async def test_deluser_confirms_plainly_for_a_db_granted_user():
    from bot.handlers import users

    message = AsyncMock()
    message.text = "/deluser 777"
    message.from_user = MagicMock(id=1)
    db = AsyncMock()

    settings = MagicMock()
    settings.is_user_allowed.return_value = False

    with patch.object(users, "get_settings", return_value=settings):
        await users.cmd_deluser(message, db=db, is_admin=True)

    db.remove_allowed_user.assert_awaited_once_with(777)
    reply = message.answer.await_args.args[0]
    assert "отозв" in reply
    assert "ALLOWED_TG_IDS" not in reply


@pytest.mark.asyncio
async def test_adduser_notes_a_user_who_already_has_env_access():
    from bot.handlers import users

    message = AsyncMock()
    message.text = "/adduser 777"
    message.from_user = MagicMock(id=1)
    db = AsyncMock()

    settings = MagicMock()
    settings.is_user_allowed.return_value = True

    with patch.object(users, "get_settings", return_value=settings):
        await users.cmd_adduser(message, db=db, is_admin=True)

    reply = message.answer.await_args.args[0]
    assert "уже" in reply.lower(), reply


# ---------------------------------------------------------------- PARSE-01
@pytest.mark.parametrize(
    "name",
    [
        # Verbatim from the live searchReleases output — Russian trackers name
        # a BluRay remux "BDRemux", which the parser did not recognise as a
        # source at all, so the card showed "Источник: —" for exactly the
        # releases this stack keeps getting.
        "Майкл / Michael [2026, США, Великобритания, биография, музыка драма UHD BDRemux 2160p]",
        "Фильм / Movie [2024, BDRip 1080p]",
        "Series.S01.BD-Remux.2160p",
    ],
)
def test_bluray_remux_is_recognised_as_a_source(name):
    from bot.clients.prowlarr import ProwlarrClient

    parse_quality = ProwlarrClient("http://prowlarr", "key")._parse_quality

    assert parse_quality(name).source == "BluRay", name


def test_a_web_dl_is_still_a_web_dl():
    """The BDRemux clause must not swallow other sources."""
    from bot.clients.prowlarr import ProwlarrClient

    parse_quality = ProwlarrClient("http://prowlarr", "key")._parse_quality

    q = parse_quality("Michael.2026.2160p.iT.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-BYNDR")

    assert q.source == "WEB-DL"
    assert q.is_remux is False
