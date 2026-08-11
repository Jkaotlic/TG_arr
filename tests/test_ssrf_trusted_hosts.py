"""Fix: SSRF download-URL guard must trust the user's OWN configured services.

A self-hosted single-household stack runs Prowlarr/*arr/qBit on a private LAN
(e.g. 192.168.x), and a grab URL legitimately points at Prowlarr's own
download proxy — so a grab download URL legitimately has a private IP.
Configured service hosts must be allowed while other internal hosts stay
blocked.

Rollback 2026-08-10 (Task 10, fix round 1): rewritten to use Prowlarr's
configured host, not Scryer's — conftest's `_default_env` no longer sets
SCRYER_URL at all (Scryer is gone), so the original `localhost:8088`
assertions were asserting against a host nothing configures anymore. They
were failing, not because the SSRF guard broke, but because the fixture
they depended on was deleted out from under them.
"""

import pytest

from bot.services.add_service import _validate_download_url

# conftest's _default_env sets PROWLARR_URL=http://localhost:9696 for every test.
_TRUSTED_HOST = "localhost"
_TRUSTED_PORT = 9696


@pytest.mark.asyncio
async def test_allows_configured_service_host_even_on_private_lan():
    # "localhost:9696" (Prowlarr, per conftest) is a trusted service host — a
    # downloadUrl pointing there must be allowed.
    assert await _validate_download_url(
        f"http://{_TRUSTED_HOST}:{_TRUSTED_PORT}/2/download?apikey=x&link=y"
    ) is True


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
    # "localhost" is a trusted service host (Prowlarr's, on :9696), but :6379
    # (e.g. a Redis instance on the same LAN box) is NOT one of the
    # configured service ports and must be blocked.
    assert await _validate_download_url(f"http://{_TRUSTED_HOST}:6379/x") is False
    assert await _validate_download_url(f"http://{_TRUSTED_HOST}:22/x") is False


@pytest.mark.asyncio
async def test_same_trusted_host_correct_port_is_allowed():
    # The exact configured (host, port) pair stays trusted.
    assert await _validate_download_url(
        f"http://{_TRUSTED_HOST}:{_TRUSTED_PORT}/download?apikey=x"
    ) is True
