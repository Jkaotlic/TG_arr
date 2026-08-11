"""Tests for AddService (*arr edition — rollback 2026-08-10, Task 10).

Replaces the Scryer-edition file. The single-candidate-token queue flow this
file used to cover no longer exists — `AddService` no longer talks to Scryer
at all. Two grab paths replace it, chosen by the release's `origin` (which
parser built it) AND `indexer_id`:

- native: `origin == "arr"` and `indexer_id` is set (it came from *arr's
  interactive search) — grab it with `ArrBaseClient.grab_release(guid,
  indexer_id)`.
- push chain: anything else (today: `origin == "prowlarr"` — either a real
  Prowlarr free-text hit, or ANY release built without an explicit origin,
  since the field defaults to "prowlarr") — the restored pre-migration
  release/push -> qBittorrent -> auto-search fallback.

Fix round 1 (2026-08-10 review): `indexer_id` truthiness ALONE used to be the
signal, which was wrong — `ProwlarrClient._normalize_result` also fills
`indexer_id`, with PROWLARR's own (incompatible) numbering. The field that
actually distinguishes the two producers safely was originally named
`source`; see `test_prowlarr_sourced_release_never_takes_the_native_path`
below.

Fix round 2 (2026-08-10 review): renamed `source` -> `origin` (collided with
the unrelated `QualityInfo.source`, the release medium) and its default
flipped from "arr" to "prowlarr" — fail CLOSED, so an untagged release can
never take the native path by omission; see
`test_untagged_release_never_takes_the_native_path` below.

Invariants carried over unchanged: the SSRF guard (SEC-16) gates every URL
handed to a downstream fetcher — *arr's `release/push` call AND every direct
qBittorrent handoff — download URLs are still masked before they reach the
logs, the push-release response is never logged raw (it echoes
`downloadUrl`, which can carry a passkey), and every grab still emits exactly
one terminal `grab_completed` event. Lidarr artist-add is unchanged.
"""

from unittest.mock import AsyncMock, patch

import pytest

from bot.models import ArtistInfo, ContentType, MovieInfo, QualityInfo, SearchResult, SeriesInfo
from bot.services.add_service import (
    AddService,
    _mask_url,
    _safe_push_result,
    _validate_download_url,
)


def _release(**overrides) -> SearchResult:
    # download_url points at conftest's configured PROWLARR_URL (a trusted
    # service host per _trusted_service_hosts()) rather than an arbitrary
    # hostname — SEC-16's `_validate_download_url` now genuinely gates the
    # push_release call (fix round 1), and this default must pass that gate
    # deterministically without depending on this machine's DNS.
    #
    # origin="arr" is explicit (fix round 2: the field now defaults to
    # "prowlarr", fail-closed) — this helper's default `indexer_id=3` is
    # meant to represent a native-path release, so it must say so.
    data = dict(
        guid="g1",
        title="Movie.2024.2160p.WEB-DL",
        indexer="RuTracker",
        origin="arr",
        indexer_id=3,
        download_url="http://localhost:9696/1/download?apikey=SECRET",
        size=8 * 1024**3,
        seeders=42,
        quality=QualityInfo(resolution="2160p", source="WEB-DL"),
    )
    data.update(overrides)
    return SearchResult(**data)


def _service(radarr=None, sonarr=None, qbt=None, lidarr=None) -> AddService:
    return AddService(radarr or AsyncMock(), sonarr or AsyncMock(), qbittorrent=qbt, lidarr=lidarr)


# --------------------------------------------------------------- native path
@pytest.mark.asyncio
async def test_release_from_interactive_search_is_grabbed_natively():
    """guid + indexerId means *arr owns the download — including the magnet redirect.

    Fix round 2 (2026-08-10 review): `origin="arr"` is explicit here — the
    brief's literal test omitted it (predates the field), and `origin`
    defaults to "prowlarr" (fail-closed) since fix round 2, so this release
    would otherwise misroute to the push chain rather than the native path
    it's meant to exercise.
    """
    radarr = AsyncMock()
    radarr.grab_release.return_value = True
    service = _service(radarr=radarr)

    release = SearchResult(
        title="Dune 2160p", download_url="http://prowlarr/1/download?apikey=x",
        indexer="TPB", size=1, seeders=10, leechers=0, quality=QualityInfo(),
        guid="abc-1", indexer_id=3, custom_format_score=500, origin="arr",
    )

    ok, _ = await service.grab_release(release, ContentType.MOVIE, arr_id=15)

    assert ok is True
    radarr.grab_release.assert_awaited_once_with("abc-1", 3)
    radarr.push_release.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_grab_dispatches_to_sonarr_for_series():
    sonarr = AsyncMock()
    sonarr.grab_release.return_value = True
    service = _service(sonarr=sonarr)

    ok, action = await service.grab_release(_release(), ContentType.SERIES, arr_id=7)

    assert ok is True
    assert action.success is True
    sonarr.grab_release.assert_awaited_once_with("g1", 3)


@pytest.mark.asyncio
async def test_native_grab_failure_is_reported_without_raising():
    radarr = AsyncMock()
    radarr.grab_release.side_effect = RuntimeError("Radarr временно недоступен")
    service = _service(radarr=radarr)

    ok, action = await service.grab_release(_release(), ContentType.MOVIE, arr_id=15)

    assert ok is False
    assert action.success is False
    assert "недоступен" in action.error_message


@pytest.mark.asyncio
async def test_native_grab_logged_with_the_native_path():
    radarr = AsyncMock()
    radarr.grab_release.return_value = True
    service = _service(radarr=radarr)

    with patch("bot.services.add_service._log_grab_completed") as log_mock:
        await service.grab_release(_release(), ContentType.MOVIE, arr_id=15)

    log_mock.assert_called_once()
    assert log_mock.call_args.kwargs["success"] is True
    assert log_mock.call_args.kwargs["path"] == "native"


@pytest.mark.asyncio
async def test_prowlarr_sourced_release_never_takes_the_native_path():
    """Fix round 1 (2026-08-10 review): `indexer_id` truthiness alone is NOT
    a safe signal for the native path — `ProwlarrClient._normalize_result`
    also fills `indexer_id`, with PROWLARR's own indexer numbering, which is
    meaningless (or actively wrong) to *arr's native `/release` endpoint
    (pre-migration BUG-05). A release with `origin="prowlarr"` must be routed
    to the push chain even though it carries a truthy `indexer_id` — exactly
    the shape `ProwlarrClient._normalize_result` produces today.
    """
    radarr = AsyncMock()
    radarr.push_release.return_value = {"approved": True}
    service = _service(radarr=radarr)

    release = _release(origin="prowlarr", indexer_id=7)  # Prowlarr's OWN numbering
    assert release.indexer_id  # sanity: the truthy signal alone WOULD say "native"

    ok, _ = await service.grab_release(release, ContentType.MOVIE, arr_id=15)

    assert ok is True
    radarr.grab_release.assert_not_awaited()  # never routed to the native path
    radarr.push_release.assert_awaited_once()


@pytest.mark.asyncio
async def test_untagged_release_never_takes_the_native_path():
    """Fix round 2 (2026-08-10 review): `SearchResult.origin` must fail
    CLOSED, not open — a release built without an explicit `origin` (a
    hand-built one, or a future producer that forgets to tag it) must never
    be trusted for the native path just because `indexer_id` happens to be
    set. Constructs a `SearchResult` directly (not via the `_release()`
    helper, which explicitly sets `origin="arr"`) to prove the MODEL's own
    default is safe, independent of any test helper's choices.
    """
    radarr = AsyncMock()
    radarr.push_release.return_value = {"approved": True}
    service = _service(radarr=radarr)

    release = SearchResult(
        guid="g1", title="Movie.2024.2160p.WEB-DL", indexer="RuTracker",
        indexer_id=3,  # truthy — WOULD say "native" if origin weren't checked
        download_url="http://localhost:9696/1/download?apikey=SECRET",
    )
    assert release.origin == "prowlarr"  # sanity: the model's own default

    ok, _ = await service.grab_release(release, ContentType.MOVIE, arr_id=15)

    assert ok is True
    radarr.grab_release.assert_not_awaited()
    radarr.push_release.assert_awaited_once()


# ---------------------------------------------------------------------- sec-16
@pytest.mark.asyncio
async def test_push_release_rejects_a_private_url():
    """SEC-16 (fix round 1, restored): push_release must never be called with
    a private URL — *arr would fetch it itself, from inside the LAN, turning
    *arr into an SSRF proxy. Restored from `f30545d~1`
    (`test_push_release_rejects_private_url_movie`), adapted to the current
    `grab_release(release, content_type, arr_id)` shape: no verdict was
    rendered (the push never even ran), so — same as pre-migration — this
    falls through to *arr's own auto-search rather than being reported as a
    rejection.
    """
    radarr = AsyncMock()
    service = _service(radarr=radarr)

    release = _release(indexer_id=None, download_url="http://192.168.1.1/x")
    ok, action = await service.grab_release(release, ContentType.MOVIE, arr_id=15)

    radarr.push_release.assert_not_awaited()
    assert ok is True
    assert action.success is True
    radarr.search_movie.assert_awaited_once_with(15)


@pytest.mark.asyncio
async def test_push_release_rejects_a_loopback_url():
    """SEC-16 (fix round 1, restored): same guard, loopback host. Restored
    from `f30545d~1` (`test_push_release_rejects_loopback_url_series`)."""
    sonarr = AsyncMock()
    service = _service(sonarr=sonarr)

    release = _release(indexer_id=None, download_url="http://127.0.0.1:8080/file.torrent")
    ok, action = await service.grab_release(release, ContentType.SERIES, arr_id=77)

    sonarr.push_release.assert_not_awaited()
    assert ok is True
    assert action.success is True
    sonarr.search_series.assert_awaited_once_with(77)


# ------------------------------------------------------------------ fallback
@pytest.mark.asyncio
async def test_release_without_indexer_id_falls_back_to_push():
    """A Prowlarr free-text hit has no *arr indexer id — push it instead.

    The brief's literal `download_url` ("http://prowlarr/1/download?apikey=x")
    does not resolve on a dev machine, which is irrelevant to what this test
    is actually about (path selection, not SEC-16) — SEC-16 (fix round 1,
    restored) would otherwise correctly block the push and fail this test for
    the wrong reason. Patch `_validate_download_url` rather than weaken the
    guard or use an unsafe URL to make it pass: never make a test pass by
    opening a security control (see Task 8's `too_short` precedent).
    """
    radarr = AsyncMock()
    radarr.push_release.return_value = {"approved": True}
    service = _service(radarr=radarr)

    release = SearchResult(
        title="Dune 2160p", download_url="http://prowlarr/1/download?apikey=x",
        indexer="TPB", size=1, seeders=10, leechers=0, quality=QualityInfo(),
        guid="abc-1", indexer_id=None,
    )

    with patch(
        "bot.services.add_service._validate_download_url",
        new=AsyncMock(return_value=True),
    ):
        ok, _ = await service.grab_release(release, ContentType.MOVIE, arr_id=15)

    assert ok is True
    radarr.grab_release.assert_not_awaited()
    radarr.push_release.assert_awaited_once()


@pytest.mark.asyncio
async def test_push_success_is_logged_with_the_push_path():
    radarr = AsyncMock()
    radarr.push_release.return_value = {"approved": True}
    service = _service(radarr=radarr)

    with patch("bot.services.add_service._log_grab_completed") as log_mock:
        await service.grab_release(_release(indexer_id=None), ContentType.MOVIE, arr_id=15)

    log_mock.assert_called_once()
    assert log_mock.call_args.kwargs["success"] is True
    assert log_mock.call_args.kwargs["path"] == "push"


@pytest.mark.asyncio
async def test_push_rejection_stops_the_chain_without_a_qbit_fallback():
    """An explicit profile rejection is a decision — no qBittorrent configured,
    no auto-search: report the rejection, don't grab something else."""
    radarr = AsyncMock()
    radarr.push_release.return_value = {"approved": False, "rejections": ["Not enough seeders"]}
    service = _service(radarr=radarr, qbt=None)

    ok, action = await service.grab_release(_release(indexer_id=None), ContentType.MOVIE, arr_id=15)

    assert ok is False
    assert action.success is False
    assert "Not enough seeders" in action.error_message
    assert "Not enough seeders" in action.details
    radarr.search_movie.assert_not_awaited()


@pytest.mark.asyncio
async def test_push_rejection_falls_back_to_qbittorrent_when_configured():
    radarr = AsyncMock()
    radarr.push_release.return_value = {"approved": False, "rejections": ["blocked"]}
    qbt = AsyncMock()
    qbt.add_torrent_url.return_value = True
    service = _service(radarr=radarr, qbt=qbt)

    ok, action = await service.grab_release(
        _release(indexer_id=None, download_url="magnet:?xt=urn:btih:abcdef0123456789"),
        ContentType.MOVIE, arr_id=15,
    )

    assert ok is True
    assert action.success is True
    qbt.add_torrent_url.assert_awaited_once()
    assert qbt.add_torrent_url.await_args.kwargs["category"] == "radarr"


@pytest.mark.asyncio
async def test_qbittorrent_fallback_uses_the_anime_category():
    sonarr = AsyncMock()
    sonarr.push_release.return_value = {"approved": False, "rejections": ["blocked"]}
    qbt = AsyncMock()
    qbt.add_torrent_url.return_value = True
    service = _service(sonarr=sonarr, qbt=qbt)

    await service.grab_release(
        _release(indexer_id=None, download_url="magnet:?xt=urn:btih:abcdef0123456789"),
        ContentType.ANIME, arr_id=15,
    )

    assert qbt.add_torrent_url.await_args.kwargs["category"] == "anime"


@pytest.mark.asyncio
async def test_push_transport_failure_falls_through_to_auto_search():
    """A push that never got a verdict (transport error) is not a rejection —
    fall back to *arr's own auto-search rather than reporting failure."""
    from bot.clients.base import APIError

    radarr = AsyncMock()
    radarr.push_release.side_effect = APIError("Radarr временно недоступен (503)")
    service = _service(radarr=radarr)

    ok, action = await service.grab_release(_release(indexer_id=None), ContentType.MOVIE, arr_id=15)

    assert ok is True
    assert action.success is True
    radarr.search_movie.assert_awaited_once_with(15)


@pytest.mark.asyncio
async def test_auto_search_targets_the_picked_season_for_a_season_pack():
    """A season-pack release must trigger SeasonSearch, not a full series search."""
    sonarr = AsyncMock()
    sonarr.push_release.return_value = {"approved": False, "rejections": []}
    service = _service(sonarr=sonarr)

    release = _release(
        indexer_id=None, download_url=None, is_season_pack=True, detected_season=2,
    )
    ok, _ = await service.grab_release(release, ContentType.SERIES, arr_id=7)

    assert ok is True
    sonarr.search_season.assert_awaited_once_with(7, 2)
    sonarr.search_series.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_search_falls_back_to_the_whole_series_without_a_season():
    sonarr = AsyncMock()
    service = _service(sonarr=sonarr)

    release = _release(indexer_id=None, download_url=None)
    ok, _ = await service.grab_release(release, ContentType.SERIES, arr_id=7)

    assert ok is True
    sonarr.search_series.assert_awaited_once_with(7)
    sonarr.search_season.assert_not_awaited()


@pytest.mark.asyncio
async def test_push_release_response_is_never_logged_raw():
    """SEC-03: the push response echoes downloadUrl (with the tracker's apikey/
    passkey) — only the safe (approved/rejections) view may reach the logs."""
    radarr = AsyncMock()
    radarr.push_release.return_value = {
        "approved": True,
        "downloadUrl": "http://prowlarr/1/download?apikey=SUPERSECRET",
    }
    service = _service(radarr=radarr)

    with patch("bot.services.add_service.logger") as log_mock:
        bound = log_mock.bind.return_value
        await service.grab_release(_release(indexer_id=None), ContentType.MOVIE, arr_id=15)

    logged_text = " ".join(str(c) for c in bound.info.call_args_list)
    assert "SUPERSECRET" not in logged_text


# -------------------------------------------------------------- force download
@pytest.mark.asyncio
async def test_force_download_rejects_a_private_url():
    """SEC-16: the SSRF guard still gates everything handed directly to qBittorrent."""
    qbt = AsyncMock()
    service = _service(qbt=qbt)

    ok, action = await service.grab_release(
        _release(download_url="http://192.168.1.50:8080/admin"),
        ContentType.MOVIE, arr_id=15, force_download=True,
    )

    assert ok is False
    assert action.success is False
    qbt.add_torrent_url.assert_not_awaited()


@pytest.mark.asyncio
async def test_force_download_allows_a_magnet_uri():
    qbt = AsyncMock()
    qbt.add_torrent_url.return_value = True
    service = _service(qbt=qbt)

    ok, action = await service.grab_release(
        _release(download_url=None, magnet_url="magnet:?xt=urn:btih:abcdef0123456789"),
        ContentType.MOVIE, arr_id=15, force_download=True,
    )

    assert ok is True
    assert action.success is True
    qbt.add_torrent_url.assert_awaited_once()


@pytest.mark.asyncio
async def test_force_download_skips_both_grab_paths_entirely():
    """force_download must not attempt native OR push — straight to qBittorrent."""
    radarr = AsyncMock()
    qbt = AsyncMock()
    qbt.add_torrent_url.return_value = True
    service = _service(radarr=radarr, qbt=qbt)

    await service.grab_release(
        _release(indexer_id=3, download_url=None, magnet_url="magnet:?xt=urn:btih:abcdef0123456789"),
        ContentType.MOVIE, arr_id=15, force_download=True,
    )

    radarr.grab_release.assert_not_awaited()
    radarr.push_release.assert_not_awaited()
    qbt.add_torrent_url.assert_awaited_once()


@pytest.mark.asyncio
async def test_force_download_uses_the_anime_category():
    qbt = AsyncMock()
    qbt.add_torrent_url.return_value = True
    service = _service(qbt=qbt)

    await service.grab_release(
        _release(download_url="magnet:?xt=urn:btih:abcdef0123456789"),
        ContentType.ANIME, arr_id=7, force_download=True,
    )

    assert qbt.add_torrent_url.await_args.kwargs["category"] == "anime"


@pytest.mark.asyncio
async def test_force_download_without_qbittorrent_reports_config_error():
    service = _service(qbt=None)

    ok, action = await service.grab_release(
        _release(), ContentType.MOVIE, arr_id=15, force_download=True,
    )

    assert ok is False
    assert "qBittorrent" in action.error_message


@pytest.mark.asyncio
async def test_qbittorrent_rejection_is_reported_as_failure():
    qbt = AsyncMock()
    qbt.add_torrent_url.return_value = False
    service = _service(qbt=qbt)

    ok, action = await service.grab_release(
        _release(download_url="magnet:?xt=urn:btih:abcdef0123456789"),
        ContentType.MOVIE, arr_id=15, force_download=True,
    )

    assert ok is False
    assert action.success is False


@pytest.mark.asyncio
async def test_force_download_completed_logged_with_the_force_flag():
    qbt = AsyncMock()
    qbt.add_torrent_url.return_value = True
    service = _service(qbt=qbt)

    with patch("bot.services.add_service._log_grab_completed") as log_mock:
        await service.grab_release(
            _release(download_url="magnet:?xt=urn:btih:abcdef0123456789"),
            ContentType.MOVIE, arr_id=15, force_download=True,
        )

    assert log_mock.call_args.kwargs["force_download"] is True
    assert log_mock.call_args.kwargs["path"] == "qbit"


# ---------------------------------------------------------------- url masking
def test_mask_url_hides_apikey_and_passkey_path_segment():
    masked = _mask_url(
        "https://tracker.example/download/123/0123456789abcdef0123456789abcdef/x.torrent?apikey=SECRET"
    )
    assert "SECRET" not in masked
    assert "0123456789abcdef0123456789abcdef" not in masked
    assert "tracker.example" in masked


def test_safe_push_result_keeps_only_the_decision_fields():
    """SEC-03: the raw push response echoes downloadUrl (secret) — drop it."""
    raw = {
        "approved": False,
        "rejections": ["Not enough seeders"],
        "downloadUrl": "http://prowlarr/1/download?apikey=SECRET",
        "title": "Some.Release.Title",
    }
    safe = _safe_push_result(raw)
    assert safe == {"approved": False, "rejections": ["Not enough seeders"]}


def test_safe_push_result_tolerates_a_non_dict_response():
    assert _safe_push_result(None) == {"approved": None, "rejections": []}
    assert _safe_push_result([]) == {"approved": None, "rejections": []}


@pytest.mark.asyncio
async def test_validate_download_url_rejects_unknown_schemes():
    assert await _validate_download_url("file:///etc/passwd") is False
    assert await _validate_download_url("") is False


@pytest.mark.asyncio
async def test_validate_download_url_trusts_a_configured_service_host(monkeypatch):
    """SEC-01: a download URL pointing at Prowlarr's own configured host:port
    stays trusted on the LAN; other ports on the same host don't."""
    monkeypatch.setenv("PROWLARR_URL", "http://192.168.0.95:8088")
    from bot.config import get_settings

    get_settings.cache_clear()
    try:
        assert await _validate_download_url("http://192.168.0.95:8088/download?x=1") is True
        assert await _validate_download_url("http://192.168.0.95:22/") is False
    finally:
        get_settings.cache_clear()


# ------------------------------------------------------------------------ add
@pytest.mark.asyncio
async def test_add_movie_reuses_an_existing_radarr_entry():
    radarr = AsyncMock()
    radarr.get_movie_by_tmdb.return_value = MovieInfo(tmdb_id=42, title="Dune", year=2021, radarr_id=15)
    service = _service(radarr=radarr)

    added, action = await service.add_movie(
        MovieInfo(tmdb_id=42, title="Dune", year=2021), quality_profile_id=7, root_folder_path="G:\\radarr",
    )

    assert added.radarr_id == 15
    assert action.success is True
    radarr.add_movie.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_movie_adds_when_not_already_in_the_library():
    radarr = AsyncMock()
    radarr.get_movie_by_tmdb.return_value = None
    radarr.add_movie.return_value = MovieInfo(tmdb_id=42, title="Dune", year=2021, radarr_id=15)
    service = _service(radarr=radarr)

    added, action = await service.add_movie(
        MovieInfo(tmdb_id=42, title="Dune", year=2021), quality_profile_id=7, root_folder_path="G:\\radarr",
    )

    assert added.radarr_id == 15
    assert action.success is True
    radarr.add_movie.assert_awaited_once()
    assert radarr.add_movie.await_args.kwargs["quality_profile_id"] == 7
    assert radarr.add_movie.await_args.kwargs["root_folder_path"] == "G:\\radarr"


@pytest.mark.asyncio
async def test_add_movie_failure_is_reported_without_raising():
    radarr = AsyncMock()
    radarr.get_movie_by_tmdb.return_value = None
    radarr.add_movie.side_effect = RuntimeError("Radarr временно недоступен")
    service = _service(radarr=radarr)

    added, action = await service.add_movie(
        MovieInfo(tmdb_id=42, title="Dune", year=2021), quality_profile_id=7, root_folder_path="G:\\radarr",
    )

    assert added is None
    assert action.success is False


@pytest.mark.asyncio
async def test_add_series_reuses_an_existing_sonarr_entry():
    sonarr = AsyncMock()
    sonarr.get_series_by_tvdb.return_value = SeriesInfo(tvdb_id=654, title="Fargo", sonarr_id=7)
    service = _service(sonarr=sonarr)

    added, action = await service.add_series(
        SeriesInfo(tvdb_id=654, title="Fargo"), quality_profile_id=7, root_folder_path="G:\\tv-sonarr",
    )

    assert added.sonarr_id == 7
    assert action.success is True
    sonarr.add_series.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_series_defaults_to_the_standard_series_type():
    sonarr = AsyncMock()
    sonarr.get_series_by_tvdb.return_value = None
    sonarr.add_series.return_value = SeriesInfo(tvdb_id=654, title="Fargo", sonarr_id=7)
    service = _service(sonarr=sonarr)

    await service.add_series(
        SeriesInfo(tvdb_id=654, title="Fargo"), quality_profile_id=7, root_folder_path="G:\\tv-sonarr",
    )

    assert sonarr.add_series.await_args.kwargs["series_type"] == "standard"


@pytest.mark.asyncio
async def test_add_series_uses_the_anime_series_type_for_anime():
    """Rollback 2026-08-10: anime is not a separate library, just Sonarr's
    own seriesType — content_type=ANIME is what flips it."""
    sonarr = AsyncMock()
    sonarr.get_series_by_tvdb.return_value = None
    sonarr.add_series.return_value = SeriesInfo(tvdb_id=424, title="Frieren", sonarr_id=9, series_type="anime")
    service = _service(sonarr=sonarr)

    await service.add_series(
        SeriesInfo(tvdb_id=424, title="Frieren"), quality_profile_id=1, root_folder_path="G:\\anime",
        content_type=ContentType.ANIME,
    )

    assert sonarr.add_series.await_args.kwargs["series_type"] == "anime"


@pytest.mark.asyncio
async def test_add_series_failure_is_reported_without_raising():
    sonarr = AsyncMock()
    sonarr.get_series_by_tvdb.return_value = None
    sonarr.add_series.side_effect = RuntimeError("Sonarr временно недоступен")
    service = _service(sonarr=sonarr)

    added, action = await service.add_series(
        SeriesInfo(tvdb_id=654, title="Fargo"), quality_profile_id=7, root_folder_path="G:\\tv-sonarr",
    )

    assert added is None
    assert action.success is False


# ----------------------------------------------------- unconverted composites
@pytest.mark.asyncio
async def test_ensure_title_raises_not_implemented_naming_task_12():
    """Fix round 1 (2026-08-10 review): a bare AttributeError is a worse
    failure mode than a named stub for a real (if not-yet-repointed) caller —
    bot/handlers/search/commands.py:215 still calls this."""
    service = _service()
    with pytest.raises(NotImplementedError, match="Task 12"):
        await service.ensure_title()


@pytest.mark.asyncio
async def test_add_and_queue_best_raises_not_implemented_naming_task_12():
    """Real caller: bot/handlers/trending.py:370."""
    service = _service()
    with pytest.raises(NotImplementedError, match="Task 12"):
        await service.add_and_queue_best()


@pytest.mark.asyncio
async def test_grab_with_fallback_raises_not_implemented_naming_task_12():
    """Real caller: bot/handlers/search/grab.py:212."""
    service = _service()
    with pytest.raises(NotImplementedError, match="Task 12"):
        await service.grab_with_fallback()


# ---------------------------------------------------------------------- music
@pytest.mark.asyncio
async def test_add_artist_without_lidarr_returns_error():
    service = _service(lidarr=None)

    added, action = await service.add_artist(
        ArtistInfo(mb_id="mb-1", name="Metallica"), 1, 1, "/music"
    )

    assert added is None
    assert action.success is False


@pytest.mark.asyncio
async def test_add_artist_reuses_an_existing_lidarr_artist():
    lidarr = AsyncMock()
    lidarr.get_artist_by_mbid = AsyncMock(
        return_value=ArtistInfo(mb_id="mb-1", name="Metallica", lidarr_id=7)
    )
    service = _service(lidarr=lidarr)

    added, action = await service.add_artist(
        ArtistInfo(mb_id="mb-1", name="Metallica"), 1, 1, "/music"
    )

    assert added.lidarr_id == 7
    assert action.success is True
    lidarr.add_artist.assert_not_awaited()


# --------------------------------------------------------------- profiles etc
def test_resolve_profile_matches_a_string_id():
    from bot.models import QualityProfile

    profiles = [QualityProfile(id=1, name="1080p"), QualityProfile(id=7, name="4K Prefer")]
    assert AddService.resolve_profile(profiles, "7").name == "4K Prefer"
    assert AddService.resolve_profile(profiles, None).name == "1080p"
    assert AddService.resolve_profile(profiles, "missing").name == "1080p"


def test_resolve_root_folder_prefers_the_default_when_unset():
    from bot.models import RootFolder

    folders = [
        RootFolder(id=1, path="H:\\radarr", is_default=False),
        RootFolder(id=2, path="G:\\radarr", is_default=True),
    ]
    assert AddService.resolve_root_folder(folders, None) == "G:\\radarr"
    assert AddService.resolve_root_folder(folders, 1) == "H:\\radarr"


def test_resolve_root_folder_raises_without_folders():
    with pytest.raises(ValueError):
        AddService.resolve_root_folder([], None)


# ---------------------------------------------------------------------------
# Task 13: Radarr/Sonarr profile/folder convenience getters, mirroring the
# get_lidarr_* shape settings.py's table-driven picker already relies on —
# so /settings can drive Radarr and Sonarr through the same _SettingsEntry
# mechanism instead of a one-off direct-client path for just these two.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_radarr_profiles_delegates_to_the_radarr_client():
    radarr = AsyncMock()
    radarr.get_quality_profiles.return_value = ["p1"]
    service = _service(radarr=radarr)

    profiles = await service.get_radarr_profiles()

    assert profiles == ["p1"]
    radarr.get_quality_profiles.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_radarr_root_folders_delegates_to_the_radarr_client():
    radarr = AsyncMock()
    radarr.get_root_folders.return_value = ["f1"]
    service = _service(radarr=radarr)

    folders = await service.get_radarr_root_folders()

    assert folders == ["f1"]
    radarr.get_root_folders.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_sonarr_profiles_delegates_to_the_sonarr_client():
    sonarr = AsyncMock()
    sonarr.get_quality_profiles.return_value = ["p2"]
    service = _service(sonarr=sonarr)

    profiles = await service.get_sonarr_profiles()

    assert profiles == ["p2"]
    sonarr.get_quality_profiles.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_sonarr_root_folders_delegates_to_the_sonarr_client():
    sonarr = AsyncMock()
    sonarr.get_root_folders.return_value = ["f2"]
    service = _service(sonarr=sonarr)

    folders = await service.get_sonarr_root_folders()

    assert folders == ["f2"]
    sonarr.get_root_folders.assert_awaited_once()
