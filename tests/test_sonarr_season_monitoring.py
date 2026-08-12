"""SonarrClient.set_season_monitoring — применение пресета мониторинга к
сериалу, который уже в библиотеке.

Живой замер 2026-08-12 (Sonarr 4.0.19.2979, протокол —
analysis/2026-08-12-seasonpass-probe.md): POST /api/v3/seasonpass отвечает 202
с пустым телом `{}`, PUT на тот же путь — 405. Значит успех нельзя судить по
содержимому ответа: критерий — отсутствие HTTP-ошибки.

Там же проверено, зачем этот эндпоинт вообще берётся вместо GET-мутация-PUT
объекта сериала: `monitor="future"` переставляет флаги ЭПИЗОДОВ, а не только
сезонов, и вытаскивает сериал из состояния `monitor="none"`, в котором его
оставляет поисковый поток.
"""

from unittest.mock import AsyncMock

import pytest

from bot.clients.base import APIError
from bot.clients.sonarr import SonarrClient


def _client() -> SonarrClient:
    return SonarrClient("http://localhost:8989", "key")


@pytest.mark.asyncio
async def test_sends_seasonpass_payload():
    client = _client()
    client.post = AsyncMock(return_value={})

    ok = await client.set_season_monitoring(42, "firstSeason")

    assert ok is True
    client.post.assert_awaited_once()
    args, kwargs = client.post.call_args
    assert args[0] == "/api/v3/seasonpass"
    assert kwargs["json_data"] == {
        "series": [{"id": 42}],
        "monitoringOptions": {"monitor": "firstSeason"},
    }


@pytest.mark.asyncio
async def test_empty_body_is_success_not_failure():
    """Sonarr отвечает 202 с `{}` — ложное по истинности тело не должно
    читаться как неудача."""
    client = _client()
    client.post = AsyncMock(return_value={})

    assert await client.set_season_monitoring(1, "all") is True


@pytest.mark.asyncio
async def test_api_error_propagates():
    client = _client()
    client.post = AsyncMock(side_effect=APIError("Ошибка Sonarr: 500", status_code=500))

    with pytest.raises(APIError):
        await client.set_season_monitoring(1, "all")
