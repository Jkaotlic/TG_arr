"""Клиент хука принудительного синка: любая беда хука — это деградация,
а не отказ, поэтому trigger_sync никогда не бросает."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from bot.clients.emby_sync_hook import EmbySyncHookClient


@pytest.fixture
def hook():
    return EmbySyncHookClient("http://hs:8099", "secret-token")


def _response(status_code: int, payload: dict | None = None):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload if payload is not None else {}
    response.text = "" if payload is None else "body"
    return response


@pytest.mark.asyncio
async def test_successful_sync_reports_ok_with_duration(hook):
    http = AsyncMock()
    http.post.return_value = _response(200, {"status": "ok", "duration_s": 4.2})
    with patch.object(hook, "_get_client", new_callable=AsyncMock, return_value=http):
        result = await hook.trigger_sync()

    assert result.status == "ok"
    assert result.duration_s == 4.2
    assert result.error is None


@pytest.mark.asyncio
async def test_token_is_sent_in_header(hook):
    http = AsyncMock()
    http.post.return_value = _response(200, {"status": "ok"})
    with patch.object(hook, "_get_client", new_callable=AsyncMock, return_value=http):
        await hook.trigger_sync()

    assert http.post.await_args.kwargs["headers"]["X-Token"] == "secret-token"
    assert http.post.await_args.args[0] == "/sync"


@pytest.mark.asyncio
async def test_202_means_a_sync_is_already_running(hook):
    http = AsyncMock()
    http.post.return_value = _response(202, {"status": "already_running"})
    with patch.object(hook, "_get_client", new_callable=AsyncMock, return_value=http):
        result = await hook.trigger_sync()

    assert result.status == "already_running"


@pytest.mark.asyncio
async def test_403_is_reported_as_failure_with_reason(hook):
    http = AsyncMock()
    http.post.return_value = _response(403)
    with patch.object(hook, "_get_client", new_callable=AsyncMock, return_value=http):
        result = await hook.trigger_sync()

    assert result.status == "failed"
    assert "403" in result.error


@pytest.mark.asyncio
async def test_connection_error_never_raises(hook):
    http = AsyncMock()
    http.post.side_effect = httpx.ConnectError("no route")
    with patch.object(hook, "_get_client", new_callable=AsyncMock, return_value=http):
        result = await hook.trigger_sync()

    assert result.status == "failed"
    assert result.error


@pytest.mark.asyncio
async def test_timeout_never_raises(hook):
    http = AsyncMock()
    http.post.side_effect = httpx.TimeoutException("slow")
    with patch.object(hook, "_get_client", new_callable=AsyncMock, return_value=http):
        result = await hook.trigger_sync()

    assert result.status == "failed"
    assert "аймаут" in result.error


@pytest.mark.asyncio
async def test_non_json_success_body_is_still_ok(hook):
    response = _response(200)
    response.json.side_effect = ValueError("not json")
    http = AsyncMock()
    http.post.return_value = response
    with patch.object(hook, "_get_client", new_callable=AsyncMock, return_value=http):
        result = await hook.trigger_sync()

    assert result.status == "ok"
    assert result.duration_s is None
