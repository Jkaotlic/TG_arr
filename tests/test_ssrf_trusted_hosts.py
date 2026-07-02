"""Fix: SSRF download-URL guard must trust the user's OWN configured services.

A self-hosted single-household stack runs Prowlarr/*arr/qBit on a private LAN
(e.g. 192.168.x). Prowlarr proxies every downloadUrl through itself, so the
grab download URL is a private IP — the SSRF guard was rejecting every real
grab. Configured service hosts must be allowed while other internal hosts stay
blocked.
"""

import pytest

from bot.services.add_service import _validate_download_url


@pytest.mark.asyncio
async def test_allows_configured_service_host_even_on_private_lan():
    # conftest configures PROWLARR/RADARR/SONARR at http://localhost:PORT,
    # so "localhost" is a trusted service host — a downloadUrl pointing there
    # (Prowlarr's proxy) must be allowed.
    assert await _validate_download_url("http://localhost:9696/2/download?apikey=x&link=y") is True


@pytest.mark.asyncio
async def test_still_blocks_unconfigured_internal_hosts():
    # An internal host that is NOT one of the configured services stays blocked
    # (real SSRF protection preserved).
    assert await _validate_download_url("http://192.168.1.1/evil") is False
    assert await _validate_download_url("http://10.0.0.5/x") is False
    assert await _validate_download_url("http://127.0.0.1:8080/admin") is False


@pytest.mark.asyncio
async def test_magnet_and_scheme_rules_unchanged():
    assert await _validate_download_url("magnet:?xt=urn:btih:aabbccdd") is True
    assert await _validate_download_url("ftp://example.com/") is False


# ---------------------------------------------------------------------------
# SEC-01: trust must be scoped to (host, port), not host alone
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_trusted_host_wrong_port_is_blocked():
    # conftest configures RADARR at http://localhost:7878 — localhost is a
    # trusted service host, but :6379 (e.g. a Redis instance on the same LAN
    # box) is NOT one of the configured service ports and must be blocked.
    assert await _validate_download_url("http://localhost:6379/x") is False
    assert await _validate_download_url("http://localhost:22/x") is False


@pytest.mark.asyncio
async def test_same_trusted_host_correct_port_is_allowed():
    # The exact configured (host, port) pairs stay trusted.
    assert await _validate_download_url("http://localhost:9696/download?apikey=x") is True
    assert await _validate_download_url("http://localhost:7878/download?apikey=x") is True
    assert await _validate_download_url("http://localhost:8989/download?apikey=x") is True
