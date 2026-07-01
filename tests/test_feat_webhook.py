"""Feature #8: webhook-driven notifications — arr 'on import' payload parsing."""

import pytest


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
