"""TorrServerClient.get_source_hosts — доверенные хосты из настроек сервера.

То, что оператор сам прописал в TorrServer как torznab-источник, доверенное по
определению: именно оттуда приходят ссылки на раздачи. Без этого SSRF-гайка
заблокировала бы легальную ссылку на Prowlarr, если в TorrServer он записан
адресом, отличным от PROWLARR_URL бота.
"""

from unittest.mock import AsyncMock

import pytest

from bot.clients.torrserver import TorrServerClient


def _client() -> TorrServerClient:
    return TorrServerClient("http://192.168.0.95:8090", "user", "pass")


@pytest.mark.asyncio
async def test_extracts_host_and_explicit_port():
    client = _client()
    client.get_server_settings = AsyncMock(return_value={
        "TorznabUrls": [
            {"Name": "Prowlarr RuTracker", "Host": "http://192.168.0.95:9696/2"},
            {"Name": "Prowlarr Rutor", "Host": "http://192.168.0.95:9696/5"},
        ],
    })

    assert await client.get_source_hosts() == {("192.168.0.95", 9696)}


@pytest.mark.asyncio
async def test_default_ports_by_scheme():
    client = _client()
    client.get_server_settings = AsyncMock(return_value={
        "TorznabUrls": [
            {"Name": "plain", "Host": "http://indexer.lan/1"},
            {"Name": "tls", "Host": "https://indexer.lan/2"},
        ],
    })

    assert await client.get_source_hosts() == {("indexer.lan", 80), ("indexer.lan", 443)}


@pytest.mark.asyncio
async def test_malformed_entries_cost_only_themselves():
    """Та же политика, что у _files_from_payload и _as_int: битая запись стоит
    только себя, а не всего списка."""
    client = _client()
    client.get_server_settings = AsyncMock(return_value={
        "TorznabUrls": [
            "not-a-dict",
            {"Name": "no host"},
            {"Name": "broken brackets", "Host": "http://[oops/1"},
            {"Name": "junk port", "Host": "http://indexer.lan:notaport/1"},
            {"Name": "good", "Host": "http://192.168.0.95:9696/2"},
        ],
    })

    assert await client.get_source_hosts() == {("192.168.0.95", 9696)}


@pytest.mark.asyncio
async def test_no_sources_gives_empty_set():
    client = _client()
    client.get_server_settings = AsyncMock(return_value={})

    assert await client.get_source_hosts() == set()
