"""Ссылка из индексера уходит в TorrServer, который живёт в той же локальной
сети и выполнит запрос за нас. Гайка SEC-16 была написана и покрыта тестами, но
стриминговый контур шёл мимо неё до 2026-08-12 — единственная в кодовой базе
передача URL наружу без проверки.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.clients.torrserver import TorrServerError
from bot.models import TorrServerTorrent
from bot.services.torrserver_service import TorrServerService


def _service(*, source_hosts=None) -> TorrServerService:
    torrent = TorrServerTorrent(hash="abc", title="t")
    client = MagicMock()
    client.add_torrent = AsyncMock(return_value=torrent)
    client.get_torrent = AsyncMock(return_value=torrent)
    client.get_source_hosts = AsyncMock(return_value=source_hosts or set())
    client.stream_url = MagicMock(return_value="http://ts/stream")
    return TorrServerService(client, hook=None, metadata_timeout=0.0, poll_interval=0.0)


@pytest.mark.asyncio
async def test_private_host_not_among_trusted_is_rejected():
    service = _service()

    with pytest.raises(TorrServerError):
        await service.add_and_publish("http://192.168.1.1/admin/reboot", "evil")

    service.client.add_torrent.assert_not_awaited()


@pytest.mark.asyncio
async def test_torznab_source_host_is_allowed():
    service = _service(source_hosts={("192.168.0.95", 9696)})

    await service.add_and_publish("http://192.168.0.95:9696/2/download?apikey=x", "ok")

    service.client.add_torrent.assert_awaited_once()


@pytest.mark.asyncio
async def test_torznab_trust_is_scoped_to_the_port():
    """SEC-01: доверие к паре (хост, порт), а не к хосту целиком — иначе оно
    вырождается в «доверять любому порту на этом IP»."""
    service = _service(source_hosts={("192.168.0.95", 9696)})

    with pytest.raises(TorrServerError):
        await service.add_and_publish("http://192.168.0.95:22/x", "evil")

    service.client.add_torrent.assert_not_awaited()


@pytest.mark.asyncio
async def test_magnet_is_allowed():
    service = _service()

    await service.add_and_publish("magnet:?xt=urn:btih:aabbccdd", "ok")

    service.client.add_torrent.assert_awaited_once()


@pytest.mark.asyncio
async def test_source_host_lookup_failure_does_not_block_a_public_link():
    """Настройки TorrServer недоступны — легальная публичная ссылка всё равно
    добавляется. Деградация в «доверять только конфигу», не в «пропустить всё»
    и не в «запретить всё»."""
    service = _service()
    service.client.get_source_hosts = AsyncMock(side_effect=TorrServerError("нет связи"))

    # Публичный IP-литерал, а не имя: путь резолвинга не задействован, тест не
    # зависит от живого DNS.
    await service.add_and_publish("http://1.2.3.4/dl.torrent", "ok")

    service.client.add_torrent.assert_awaited_once()


@pytest.mark.asyncio
async def test_source_host_lookup_failure_still_blocks_a_private_link():
    service = _service()
    service.client.get_source_hosts = AsyncMock(side_effect=TorrServerError("нет связи"))

    with pytest.raises(TorrServerError):
        await service.add_and_publish("http://192.168.1.1/admin", "evil")

    service.client.add_torrent.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejection_logs_a_masked_url(caplog):
    """Отказ пишется в лог замаскированным — passkey в логах контейнера не нужен."""
    service = _service()
    secret_segment = "0123456789abcdef0123"
    link = f"http://192.168.1.1/download/1/{secret_segment}/file.torrent"

    with caplog.at_level(logging.WARNING):
        with pytest.raises(TorrServerError):
            await service.add_and_publish(link, "evil")

    assert secret_segment not in caplog.text


@pytest.mark.asyncio
async def test_configured_torrserver_host_is_trusted(monkeypatch):
    """SEC-01-совместимо: доверяется пара (хост, порт) из TORRSERVER_URL."""
    from bot.config import get_settings
    from bot.services.url_guard import _validate_download_url

    monkeypatch.setenv("TORRSERVER_URL", "http://192.168.0.95:8090")
    monkeypatch.setenv("TORRSERVER_USERNAME", "u")
    monkeypatch.setenv("TORRSERVER_PASSWORD", "p")
    get_settings.cache_clear()

    assert await _validate_download_url("http://192.168.0.95:8090/x") is True
    assert await _validate_download_url("http://192.168.0.95:8091/x") is False
