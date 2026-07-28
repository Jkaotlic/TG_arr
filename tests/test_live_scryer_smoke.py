"""Integration smoke test against a LIVE Scryer instance.

Skipped unless `SCRYER_LIVE_URL`, `SCRYER_LIVE_USERNAME` and
`SCRYER_LIVE_PASSWORD` are set, so the normal suite stays hermetic:

    SCRYER_LIVE_URL=http://192.168.0.95:8088 \
    SCRYER_LIVE_USERNAME=admin \
    SCRYER_LIVE_PASSWORD=... \
    python -m pytest tests/test_live_scryer_smoke.py -v

What it proves that the mocked tests cannot: the GraphQL documents in
`bot/clients/scryer.py` actually match the deployed schema (a renamed or
mistyped field is a 200-with-errors response, which `execute()` raises on), and
the payload shapes still map cleanly onto the bot's models.

It is deliberately read-only — no title is added, nothing is queued.
"""

import os

import pytest
import pytest_asyncio

from bot.clients.scryer import ScryerClient
from bot.models import ContentType

LIVE_URL = os.getenv("SCRYER_LIVE_URL")
LIVE_USER = os.getenv("SCRYER_LIVE_USERNAME")
LIVE_PASSWORD = os.getenv("SCRYER_LIVE_PASSWORD")

pytestmark = [
    pytest.mark.skipif(
        not (LIVE_URL and LIVE_USER and LIVE_PASSWORD),
        reason="live Scryer credentials not configured (SCRYER_LIVE_URL/USERNAME/PASSWORD)",
    ),
    # One event loop for the whole module so the client below can be shared.
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def client():
    """ONE client for the whole module — deliberately.

    Scryer rate-limits the `login` mutation (brute-force protection), so a
    per-test client that logs in each time gets locked out partway through the
    run. This mirrors production, where `registry.get_scryer()` hands out a
    process-wide singleton that logs in once and reuses the 24h token.
    """
    c = ScryerClient(LIVE_URL, LIVE_USER, LIVE_PASSWORD)
    try:
        yield c
    finally:
        await c.close()


async def test_login_returns_a_usable_token(client):
    token = await client.login()
    assert token
    assert client._token_is_fresh()


async def test_system_health_reports_a_ready_service(client):
    health = await client.system_health()
    assert health.service_ready is True
    assert health.version
    assert health.total_titles >= 0
    # Indexer stats come from Prowlarr's sync; an empty list would mean the
    # bot's "why is nothing found" diagnostics have nothing to show.
    assert isinstance(health.indexers, list)


async def test_titles_returns_the_catalog(client):
    items, total, _has_more = await client.get_titles(limit=5)
    assert total >= 0
    for title in items:
        assert title.scryer_id
        assert title.title


async def test_titles_can_be_filtered_by_facet(client):
    for content_type in (ContentType.MOVIE, ContentType.SERIES, ContentType.ANIME):
        items, _total, _more = await client.get_titles(content_type, limit=3)
        for title in items:
            got = getattr(title, "facet", None)
            if got is not None:  # movies ride MovieInfo, which has no facet
                assert got == content_type.scryer_facet


async def test_search_metadata_multi_answers_all_three_facets(client):
    result = await client.search_metadata_multi("Dune", limit=3)
    assert set(result) == {ContentType.MOVIE, ContentType.SERIES, ContentType.ANIME}
    movies = result[ContentType.MOVIE]
    assert movies, "expected at least one movie match for 'Dune'"
    assert movies[0].title
    assert movies[0].metadata_id


async def test_search_metadata_for_the_anime_facet(client):
    items = await client.search_metadata("Frieren", ContentType.ANIME, limit=3)
    assert items
    assert items[0].facet == "ANIME"


async def test_quality_profiles_and_root_folders(client):
    profiles = await client.get_quality_profiles()
    assert profiles, "Scryer must expose at least one quality profile"
    assert all(p.id and p.name for p in profiles)

    folders = await client.get_root_folders(ContentType.MOVIE)
    assert folders, "Scryer must expose at least one movie root folder"
    # Ids must be callback-safe (no ':' — aiogram's CallbackData separator).
    assert all(":" not in str(f.id) for f in folders)


async def test_libraries_expose_a_default_per_facet(client):
    for content_type in (ContentType.MOVIE, ContentType.SERIES, ContentType.ANIME):
        library_id = await client.default_library_id(content_type)
        assert library_id, f"no default library for {content_type.value}"


async def test_search_releases_against_a_real_title(client):
    """The heaviest call: Scryer fans out to every routed indexer."""
    items, _total, _more = await client.get_titles(ContentType.MOVIE, limit=1)
    if not items:
        pytest.skip("catalog is empty — nothing to search releases for")

    releases = await client.search_releases(items[0].scryer_id, limit=5, timeout=180.0)
    # An empty list is a legitimate outcome (indexers down / nothing matches);
    # what must hold is the shape of whatever did come back.
    for release in releases:
        assert release.title
        assert release.scryer_title_id == items[0].scryer_id
        if release.candidate_token:
            assert release.queue_scope is not None
        # The bot must never surface a raw credential-bearing URL as the guid.
        assert "apikey" not in release.guid


async def test_download_queue_and_calendar_are_readable(client):
    queue = await client.get_download_queue()
    for item in queue:
        assert item.id
        assert 0 <= item.progress_percent <= 100

    calendar = await client.get_calendar("2026-07-01", "2026-08-31")
    for entry in calendar:
        assert entry.title_name


async def test_wanted_items_are_readable(client):
    items, total, _more = await client.get_wanted("MISSING", limit=5)
    assert total >= 0
    for item in items:
        assert item.title_id


async def test_check_connection_reports_the_version(client):
    ok, version, elapsed_ms = await client.check_connection()
    assert ok is True
    assert version
    assert elapsed_ms is not None
