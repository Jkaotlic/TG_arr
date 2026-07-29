"""Integration smoke tests against the LIVE music stack (2026-07-29).

Skipped unless the corresponding env vars are set, so the normal suite stays
hermetic. Each backend is independent — configure only what you want to check:

    LIDARR_LIVE_URL=... LIDARR_LIVE_API_KEY=... \
    SLSKD_LIVE_URL=...  SLSKD_LIVE_API_KEY=... \
    NAVIDROME_LIVE_URL=... NAVIDROME_LIVE_USERNAME=... NAVIDROME_LIVE_PASSWORD=... \
    python -m pytest tests/test_live_music_smoke.py -v

What these catch that the mocked tests can't: an API that moved under us.
Scryer already has this (tests/test_live_scryer_smoke.py) and it earned its
keep immediately — it found a GraphQL field that didn't exist.

Read-only: nothing is enqueued, added or deleted.
"""

import os

import pytest
import pytest_asyncio

from bot.clients.lidarr import LidarrClient
from bot.clients.navidrome import NavidromeClient
from bot.clients.slskd import SlskdClient

LIDARR_URL = os.getenv("LIDARR_LIVE_URL")
LIDARR_KEY = os.getenv("LIDARR_LIVE_API_KEY")
SLSKD_URL = os.getenv("SLSKD_LIVE_URL")
SLSKD_KEY = os.getenv("SLSKD_LIVE_API_KEY")
NAVIDROME_URL = os.getenv("NAVIDROME_LIVE_URL")
NAVIDROME_USER = os.getenv("NAVIDROME_LIVE_USERNAME")
NAVIDROME_PASSWORD = os.getenv("NAVIDROME_LIVE_PASSWORD")

pytestmark = pytest.mark.asyncio(loop_scope="module")

needs_lidarr = pytest.mark.skipif(
    not (LIDARR_URL and LIDARR_KEY), reason="LIDARR_LIVE_URL/API_KEY not configured"
)
needs_slskd = pytest.mark.skipif(
    not (SLSKD_URL and SLSKD_KEY), reason="SLSKD_LIVE_URL/API_KEY not configured"
)
needs_navidrome = pytest.mark.skipif(
    not (NAVIDROME_URL and NAVIDROME_USER and NAVIDROME_PASSWORD),
    reason="NAVIDROME_LIVE_* not configured",
)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def lidarr():
    client = LidarrClient(LIDARR_URL, LIDARR_KEY)
    try:
        yield client
    finally:
        await client.close()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def slskd():
    client = SlskdClient(SLSKD_URL, SLSKD_KEY, search_timeout=25.0)
    try:
        yield client
    finally:
        await client.close()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def navidrome():
    client = NavidromeClient(NAVIDROME_URL, NAVIDROME_USER, NAVIDROME_PASSWORD)
    try:
        yield client
    finally:
        await client.close()


# ------------------------------------------------------------------- Lidarr
@needs_lidarr
async def test_lidarr_is_reachable(lidarr):
    ok, version, elapsed = await lidarr.check_connection()
    assert ok is True
    assert version
    assert elapsed is not None


@needs_lidarr
async def test_lidarr_artist_lookup_returns_usable_records(lidarr):
    artists = await lidarr.lookup_artist("Metallica")
    assert artists
    assert artists[0].name
    assert artists[0].mb_id, "an artist without a MusicBrainz id cannot be added"


@needs_lidarr
async def test_lidarr_exposes_profiles_and_root_folders(lidarr):
    """Adding an artist needs all three — a missing one breaks /music silently."""
    profiles = await lidarr.get_quality_profiles()
    metadata = await lidarr.get_metadata_profiles()
    folders = await lidarr.get_root_folders()
    assert profiles and metadata and folders
    assert all(p.id and p.name for p in profiles)
    assert all(f.path for f in folders)


# -------------------------------------------------------------------- slskd
@needs_slskd
async def test_slskd_is_connected_to_soulseek(slskd):
    """A running slskd that is logged out cannot download anything."""
    ok, version, elapsed = await slskd.check_connection()
    assert ok is True, "slskd is up but not logged in to Soulseek"
    assert version
    assert elapsed is not None


@needs_slskd
async def test_slskd_search_returns_grouped_audio(slskd):
    results = await slskd.search("Metallica Master of Puppets", limit=5)
    if not results:
        pytest.skip("no Soulseek peers answered — nothing to assert on")

    top = results[0]
    assert top.username
    assert top.track_count > 0
    assert top.total_size > 0
    # Grouping is the point: every file in a result must be audio.
    assert all(f.extension for f in top.files)


@needs_slskd
async def test_slskd_transfers_are_readable(slskd):
    for transfer in await slskd.get_active_transfers():
        assert transfer.username
        assert 0 <= transfer.progress_percent <= 100


# ---------------------------------------------------------------- Navidrome
@needs_navidrome
async def test_navidrome_is_reachable(navidrome):
    ok, version, elapsed = await navidrome.check_connection()
    assert ok is True
    assert version
    assert elapsed is not None


@needs_navidrome
async def test_navidrome_library_is_searchable(navidrome):
    """Subsonic reports failures inside a 200, so an empty result could mean
    'wrong credentials' — check against something the library actually has."""
    body = await navidrome._subsonic("getAlbumList2", {"type": "alphabeticalByArtist", "size": 5})
    albums = ((body.get("albumList2") or {}).get("album")) or []
    if not albums:
        pytest.skip("Navidrome library is empty")

    first = albums[0]
    assert await navidrome.has_artist(first["artist"]) is True
    assert await navidrome.has_album(first["artist"], first["name"]) is True


@needs_navidrome
async def test_navidrome_does_not_invent_matches(navidrome):
    assert await navidrome.has_album("Nonexistent Band 12345", "No Such Album") is False
