"""Live smoke tests against the real Radarr/Sonarr/Prowlarr/Lidarr.

Skipped unless RUN_LIVE_TESTS=1. These are the tests that catch what mocks
cannot: a field the API does not actually return, an auth mode that differs
per service, an indexer that answers empty.

Precedent: the July migration's live smoke against Scryer caught a release
query asking for fields absent from the deployed schema, which every
mock-based test had passed. This module is that same discipline applied to
the Radarr/Sonarr/Prowlarr/Lidarr rollback.

Credentials come from the repo-root `.env` (gitignored) via
`bot.config.get_settings()` — the same path the running bot uses. Run with:

    RUN_LIVE_TESTS=1 python -m pytest tests/test_live_arr_smoke.py -v

Read-only, deliberately: no add_movie/add_series/add_artist, no
push_release/grab, no monitor toggles. This is a production media stack with
a real library — nothing here may mutate it.

All tests share ONE event loop (`loop_scope="module"`). The clients come from
`bot.clients.registry`'s process-wide singletons, and each wraps a pooled
httpx.AsyncClient bound to the loop that was running when it was first built;
pytest-asyncio's default per-test event loop would hand a second test a
client whose loop had already been torn down by the first. The Scryer-era
live smoke (`git show f845bf3:tests/test_live_scryer_smoke.py`) hit the same
issue and fixed it the same way.
"""

import os

import pytest
import pytest_asyncio

from bot.models import ContentType

pytestmark = [
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_TESTS") != "1", reason="live tests need RUN_LIVE_TESTS=1",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest.fixture(autouse=True)
def _use_real_dot_env_credentials(monkeypatch, _default_env):
    """tests/conftest.py's `_default_env` (autouse, applies to every test
    under tests/) points PROWLARR_URL/RADARR_URL/SONARR_URL at fake
    `localhost` addresses with fake API keys, so the hermetic mocked suite
    never touches a real service by accident. That is exactly the override
    this live module must NOT have.

    Requesting `_default_env` as a parameter forces pytest to run it first,
    so these `delenv` calls are guaranteed to run after its `setenv` calls —
    removing the fake env vars lets pydantic-settings fall through to the
    real repo-root `.env` (see bot/config.py's `env_file=".env"`).

    Caught live: without this fixture, every client in this module silently
    talks to http://localhost:7878 etc. instead of the real 192.168.0.95
    hosts, and every test fails with ConnectError — not a live-service
    defect, a test-harness/fixture conflict discovered while wiring this
    module up.
    """
    for var in (
        "PROWLARR_URL", "PROWLARR_API_KEY",
        "RADARR_URL", "RADARR_API_KEY",
        "SONARR_URL", "SONARR_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)

    from bot.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    assert "localhost" not in settings.radarr_url, (
        f"RADARR_URL is still {settings.radarr_url!r} — real .env not picked up "
        "(missing file, or *_URL set in the ambient environment overriding it)"
    )


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _close_registry_clients_after_module():
    """Release the pooled httpx clients once this module is done with them.

    The registry singletons are process-wide by design (see
    bot/clients/registry.py), so nothing closes them automatically — a
    process that only ever runs this module would otherwise leak connections.
    """
    yield
    from bot.clients.registry import close_all

    await close_all()


async def test_all_four_services_answer_check_connection():
    """Live fact (2026-08-10): Lidarr's `_api_prefix` must be `/api/v1` — a
    `/api/v3` probe reports "API DOWN" on this instance. If that ever
    regresses, this is the assertion that catches it: check_connection()
    hits `{_api_prefix}/system/status` and a wrong prefix means `available`
    comes back False.
    """
    from bot.clients.registry import get_lidarr, get_prowlarr, get_radarr, get_sonarr

    for factory in (get_radarr, get_sonarr, get_prowlarr, get_lidarr):
        client = await factory()
        assert client is not None, (
            f"{factory.__name__}() returned None — service not enabled per .env"
        )
        available, version, elapsed_ms = await client.check_connection()
        assert available, f"{client.service_name} is unreachable"
        assert version, f"{client.service_name} returned no version"
        assert elapsed_ms is not None


async def test_radarr_lookup_finds_a_well_known_movie():
    """Exercises Radarr's real /movie/lookup metadata shape (TMDb-backed),
    not a fixture — parses into MovieInfo and checks fields a mock can't
    get wrong on its own.
    """
    from bot.clients.registry import get_radarr

    radarr = await get_radarr()
    movies = await radarr.lookup_movie("Dune 2021")

    assert movies, "Radarr metadata lookup returned nothing for 'Dune 2021'"
    dune = next((m for m in movies if m.year == 2021), None)
    assert dune is not None, f"no 2021 result among Radarr's Dune matches: {[m.year for m in movies]}"

    assert dune.tmdb_id, "MovieInfo.tmdb_id must be populated for a real Radarr lookup hit"
    assert dune.title
    assert dune.overview, "expected a plot summary from Radarr's TMDb-backed lookup"
    assert isinstance(dune.genres, list)


async def test_sonarr_lookup_finds_a_well_known_series_with_series_type():
    """Exercises Sonarr's real /series/lookup shape, parses into SeriesInfo,
    and checks series_type is populated — the field the anime/standard
    routing in this rollback depends on (see ContentType.sonarr_series_type).
    """
    from bot.clients.registry import get_sonarr

    sonarr = await get_sonarr()
    series_list = await sonarr.lookup_series("Breaking Bad")

    assert series_list, "Sonarr metadata lookup returned nothing for 'Breaking Bad'"
    match = next((s for s in series_list if s.title == "Breaking Bad"), None)
    assert match is not None, f"no exact 'Breaking Bad' title among matches: {[s.title for s in series_list]}"

    assert match.tvdb_id, "SeriesInfo.tvdb_id must be populated for a real Sonarr lookup hit"
    assert match.series_type, "series_type must not be empty/None"
    assert match.series_type in ("standard", "anime", "daily"), match.series_type


async def test_prowlarr_returns_releases_for_a_popular_title():
    """Prowlarr fans out to every routed indexer (live fact: 5 reach
    Sonarr/Radarr — Kinozal, Knaben, NoNaMe Club, RuTracker.org, The Pirate
    Bay). Every release must carry something the *arr clients can push —
    either downloadUrl or magnetUrl, not necessarily both.
    """
    from bot.clients.registry import get_prowlarr

    prowlarr = await get_prowlarr()
    results = await prowlarr.search("Dune", content_type=ContentType.MOVIE)

    assert results, "no indexer returned a release for a very popular title"
    missing_both = [r for r in results if not (r.download_url or r.magnet_url)]
    assert not missing_both, (
        f"{len(missing_both)}/{len(results)} releases had neither download_url nor magnet_url: "
        f"{[(r.indexer, r.title) for r in missing_both[:5]]}"
    )


async def test_radarr_quality_profiles_and_root_folders_match_measured_state():
    """Live facts (2026-08-10): root folders G:\\radarr\\Films (id=1) and
    H:\\radarr\\Films (id=2); quality profile id=7 = "4k/1080p".
    """
    from bot.clients.registry import get_radarr

    radarr = await get_radarr()
    profiles = await radarr.get_quality_profiles()
    folders = await radarr.get_root_folders()

    assert profiles, "Radarr must expose at least one quality profile"
    by_id = {p.id: p.name for p in profiles}
    assert by_id.get(7) == "4k/1080p", f"profile id=7 mismatch, got: {by_id}"

    folder_paths = {f.id: f.path for f in folders}
    assert folder_paths.get(1, "").startswith("G:"), f"root folder id=1 mismatch: {folder_paths}"
    assert folder_paths.get(2, "").startswith("H:"), f"root folder id=2 mismatch: {folder_paths}"


async def test_sonarr_quality_profiles_and_root_folders_match_measured_state():
    """Live facts (2026-08-10): root folders G:\\tv-sonarr\\Serials (id=1) and
    H:\\tv-sonarr\\Serials (id=2); quality profile id=7 = "4K Prefer / 1080p
    fallback".
    """
    from bot.clients.registry import get_sonarr

    sonarr = await get_sonarr()
    profiles = await sonarr.get_quality_profiles()
    folders = await sonarr.get_root_folders()

    assert profiles, "Sonarr must expose at least one quality profile"
    by_id = {p.id: p.name for p in profiles}
    assert by_id.get(7) == "4K Prefer / 1080p fallback", f"profile id=7 mismatch, got: {by_id}"

    folder_paths = {f.id: f.path for f in folders}
    assert folder_paths.get(1, "").startswith("G:"), f"root folder id=1 mismatch: {folder_paths}"
    assert folder_paths.get(2, "").startswith("H:"), f"root folder id=2 mismatch: {folder_paths}"
