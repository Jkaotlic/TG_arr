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

"Read-only" is not the same as "free", though. Most tests here are passive
reads (`check_connection`, `lookup_movie`, `get_quality_profiles`, a plain
`get("/api/v3/movie")`) and cost nothing beyond one HTTP round-trip. One test
— `test_radarr_interactive_search_carries_the_arr_verdict` — is a genuine live
indexer fan-out (`GET /api/v3/release?movieId=`) that spends real, per-24h
tracker query quota, same as an actual user search. It is gated behind its
own `RUN_LIVE_SEARCH_TESTS=1`, on top of `RUN_LIVE_TESTS=1`, precisely so a
routine "did the credentials still work" run doesn't burn quota by accident.
Operational history: a previous backend silently exhausted Prowlarr's indexer
quota, and the only visible symptom was the bot looking broken — searches
came back empty while Prowlarr itself answered fine. Do not run the
quota-consuming test in a loop or a schedule; it exists to catch a shape
regression, not for repeated execution.

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


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_SEARCH_TESTS") != "1",
    reason="performs a real indexer fan-out (GET /api/v3/release) that spends "
           "per-24h tracker query quota, same as a live user search — needs "
           "RUN_LIVE_SEARCH_TESTS=1 on top of RUN_LIVE_TESTS=1, and should not "
           "be run repeatedly (a prior backend silently exhausted this exact "
           "quota and the only symptom was the bot looking broken)",
)
async def test_radarr_interactive_search_carries_the_arr_verdict():
    """Task 9 (2026-08-10): `GET /api/v3/release?movieId=` is the live shape
    the rewritten brief is built on — this pins that the deployed Radarr
    actually returns the verdict fields the brief documented (customFormatScore,
    rejected, rejections, languages, quality.quality.name), not just guid/title/
    size/seeders like a plain indexer search would.

    Read-only in the sense that it never mutates Radarr's library (no add/push/
    grab/monitor) — but it is NOT a passive read like its sibling tests in this
    module. `radarr.get_releases()` triggers a real fan-out to every configured
    indexer via Prowlarr and spends real, per-24h tracker query quota, exactly
    like an interactive user search would. Gated behind its own
    `RUN_LIVE_SEARCH_TESTS=1` (see module docstring) so a routine
    `RUN_LIVE_TESTS=1` credentials-check run does not fire it. Do not run this
    test repeatedly/in a loop — trackers enforce per-24h limits, and this
    project has already had an incident where a backend silently burned
    through Prowlarr's indexer quota with "the bot looks broken" as the only
    symptom.
    """
    from bot.clients.registry import get_radarr

    radarr = await get_radarr()

    # Need a movie already in the Radarr catalog — /release requires a known
    # movieId, unlike a free-text Prowlarr search. Pull one from the library
    # rather than hardcoding an id that may not exist on this instance.
    library = await radarr.get("/api/v3/movie")
    assert isinstance(library, list) and library, (
        "Radarr library is empty — no movieId available to exercise /release against"
    )
    movie_id = library[0]["id"]
    movie_title = library[0].get("title", "?")

    releases = await radarr.get_releases(movie_id)

    assert releases, (
        f"no releases returned for {movie_title!r} (movieId={movie_id}) — "
        "indexers may be down, or this movie has nothing available"
    )

    # Every release must carry the verdict fields, whichever way it went.
    verdict_shapes = {(r.rejected, bool(r.rejections)) for r in releases}
    logger_note = [(r.title, r.custom_format_score, r.rejected, r.rejections[:1]) for r in releases[:3]]
    assert any(isinstance(r.custom_format_score, int) for r in releases), (
        f"customFormatScore missing/non-int across all releases: {logger_note}"
    )
    rejected_with_reasons = [r for r in releases if r.rejected and r.rejections]
    accepted = [r for r in releases if not r.rejected]
    # At least one of the two buckets must be non-empty and internally consistent
    # (a rejected release should carry a human-readable reason).
    assert accepted or rejected_with_reasons, (
        f"neither an accepted nor a reason-carrying rejected release was found: {verdict_shapes}"
    )
    if rejected_with_reasons:
        assert all(isinstance(reason, str) and reason for reason in rejected_with_reasons[0].rejections)
    # guid/indexer_id are what Task 10's native grab needs — confirm they arrive.
    assert all(r.guid for r in releases), "a release with no guid slipped through _parse_release"


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
