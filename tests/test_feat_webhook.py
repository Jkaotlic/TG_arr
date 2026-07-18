"""Feature #8: webhook-driven notifications — arr 'on import' payload parsing."""

import pytest
from unittest.mock import MagicMock


def test_parse_arr_event_radarr_download():
    from bot.webhook import parse_arr_event

    msg = parse_arr_event({"eventType": "Download", "movie": {"title": "Inception", "year": 2010}})
    assert msg is not None
    assert "Inception" in msg and "2010" in msg


def test_parse_arr_event_sonarr_download_with_episode():
    from bot.webhook import parse_arr_event

    msg = parse_arr_event({
        "eventType": "Download",
        "series": {"title": "Breaking Bad"},
        "episodes": [{"seasonNumber": 1, "episodeNumber": 2}],
    })
    assert msg is not None
    assert "Breaking Bad" in msg and "S01E02" in msg


def test_parse_arr_event_test_event_acks():
    from bot.webhook import parse_arr_event

    msg = parse_arr_event({"eventType": "Test", "instanceName": "Radarr"})
    assert msg is not None and "Radarr" in msg


def test_parse_arr_event_escapes_title():
    from bot.webhook import parse_arr_event

    msg = parse_arr_event({"eventType": "Download", "movie": {"title": "Tom & Jerry"}})
    assert msg is not None and "Tom &amp; Jerry" in msg and "Tom & Jerry" not in msg


def test_parse_arr_event_ignores_irrelevant_and_garbage():
    from bot.webhook import parse_arr_event

    assert parse_arr_event({"eventType": "Grab", "movie": {"title": "X"}}) is None
    assert parse_arr_event({"eventType": "Health"}) is None
    assert parse_arr_event({"eventType": "Download"}) is None  # no movie/series body
    assert parse_arr_event(None) is None


@pytest.mark.asyncio
async def test_webhook_app_calls_notify_on_import():
    from aiohttp.test_utils import TestClient, TestServer

    from bot.webhook import build_webhook_app

    calls: list[str] = []

    async def notify(message: str) -> None:
        calls.append(message)

    app = build_webhook_app(notify)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/webhook", json={"eventType": "Download", "movie": {"title": "Dune", "year": 2021}})
        assert resp.status == 200
        # irrelevant event does not notify
        resp2 = await client.post("/webhook/radarr", json={"eventType": "Grab"})
        assert resp2.status == 200

    assert len(calls) == 1 and "Dune" in calls[0]


# --- SEC-02/BUG-08: shared-secret auth -------------------------------------
#
# Matching rule (documented here and in bot/webhook.py): when a token is
# configured, a request is accepted if EITHER the `?token=` query parameter
# OR the last path segment of `/webhook/<token>` equals the configured
# token. `/webhook/{service}` (e.g. `/webhook/radarr`) keeps working as a
# *arr instance label in the un-authenticated (no token configured) case;
# once a token is configured, `/webhook/<service>` no longer authenticates
# unless `<service>` happens to equal the token — operators who want both
# a service label AND auth should use `/webhook/<token>?service=radarr` or
# simply `?token=<token>` on `/webhook`.


@pytest.mark.asyncio
async def test_webhook_with_correct_query_token_accepted():
    from aiohttp.test_utils import TestClient, TestServer

    from bot.webhook import build_webhook_app

    calls: list[str] = []

    async def notify(message: str) -> None:
        calls.append(message)

    app = build_webhook_app(notify, token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/webhook?token=s3cret",
            json={"eventType": "Download", "movie": {"title": "Dune", "year": 2021}},
        )
        assert resp.status == 200
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_webhook_with_correct_path_token_accepted():
    from aiohttp.test_utils import TestClient, TestServer

    from bot.webhook import build_webhook_app

    calls: list[str] = []

    async def notify(message: str) -> None:
        calls.append(message)

    app = build_webhook_app(notify, token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/webhook/s3cret",
            json={"eventType": "Download", "movie": {"title": "Dune", "year": 2021}},
        )
        assert resp.status == 200
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_webhook_path_token_is_never_logged(monkeypatch):
    """SEC-01: path authentication must not put its secret into Docker logs."""
    from aiohttp.test_utils import TestClient, TestServer

    import bot.webhook as webhook

    logged = MagicMock()
    monkeypatch.setattr(webhook, "logger", logged)

    async def notify(_: str) -> None:
        return None

    secret = "do-not-log-this-secret"
    app = webhook.build_webhook_app(notify, token=secret)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            f"/webhook/{secret}",
            json={"eventType": "Download", "movie": {"title": "Dune"}},
        )

    assert response.status == 200
    assert secret not in str(logged.mock_calls)


@pytest.mark.asyncio
async def test_webhook_without_token_rejected_403():
    from aiohttp.test_utils import TestClient, TestServer

    from bot.webhook import build_webhook_app

    calls: list[str] = []

    async def notify(message: str) -> None:
        calls.append(message)

    app = build_webhook_app(notify, token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/webhook", json={"eventType": "Download", "movie": {"title": "Dune"}})
        assert resp.status == 403
    assert calls == []


@pytest.mark.asyncio
async def test_webhook_with_wrong_token_rejected_403():
    from aiohttp.test_utils import TestClient, TestServer

    from bot.webhook import build_webhook_app

    calls: list[str] = []

    async def notify(message: str) -> None:
        calls.append(message)

    app = build_webhook_app(notify, token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/webhook/radarr?token=nope", json={"eventType": "Download"})
        assert resp.status == 403
    assert calls == []


@pytest.mark.asyncio
async def test_webhook_no_configured_token_still_works_unauthenticated():
    """Backward compat: WEBHOOK_TOKEN unset -> no auth is enforced (a startup
    warning is emitted separately via the Settings model_validator)."""
    from aiohttp.test_utils import TestClient, TestServer

    from bot.webhook import build_webhook_app

    calls: list[str] = []

    async def notify(message: str) -> None:
        calls.append(message)

    app = build_webhook_app(notify, token=None)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/webhook", json={"eventType": "Download", "movie": {"title": "Dune"}})
        assert resp.status == 200
    assert len(calls) == 1


# --- TEST-06: error paths ----------------------------------------------


@pytest.mark.asyncio
async def test_webhook_invalid_json_returns_400():
    from aiohttp.test_utils import TestClient, TestServer

    from bot.webhook import build_webhook_app

    async def notify(message: str) -> None:
        pass

    app = build_webhook_app(notify)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/webhook", data="not json", headers={"Content-Type": "application/json"})
        assert resp.status == 400


@pytest.mark.asyncio
async def test_webhook_notify_exception_still_returns_200():
    from aiohttp.test_utils import TestClient, TestServer

    from bot.webhook import build_webhook_app

    async def notify(message: str) -> None:
        raise RuntimeError("telegram down")

    app = build_webhook_app(notify)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/webhook", json={"eventType": "Download", "movie": {"title": "Dune"}})
        assert resp.status == 200


# --- LOGIC-18b: episode ranges -------------------------------------------


def test_parse_arr_event_sonarr_episode_range_for_season_pack():
    from bot.webhook import parse_arr_event

    msg = parse_arr_event({
        "eventType": "Download",
        "series": {"title": "Breaking Bad"},
        "episodes": [
            {"seasonNumber": 1, "episodeNumber": 1},
            {"seasonNumber": 1, "episodeNumber": 2},
            {"seasonNumber": 1, "episodeNumber": 10},
        ],
    })
    assert msg is not None
    assert "S01E01-E10" in msg
    assert "Breaking Bad" in msg
