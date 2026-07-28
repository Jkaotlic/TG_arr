"""Tests for the Scryer GraphQL client (bot/clients/scryer.py).

Written before the implementation (TDD). Shapes below are copied from real
responses of the live Scryer 0.17.2 instance at 192.168.31.95:8088 — see
`analysis/scryer-migration-2026-07-28.md` for the captured payloads.
"""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from bot.clients.base import APIError, AuthenticationError
from bot.clients.scryer import (
    ScryerClient,
    ScryerGraphQLError,
    mask_release_secrets,
)
from bot.models import ContentType, MovieInfo, SeriesInfo


def _resp(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        content=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        request=httpx.Request("POST", "http://scryer.local/graphql"),
    )


def _client() -> ScryerClient:
    return ScryerClient("http://scryer.local:8088", "admin", "hunter2")


LOGIN_OK = {"data": {"login": {"token": "tok-1", "expiresAt": "2026-07-29T00:00:00Z"}}}


# ------------------------------------------------------------------ auth
@pytest.mark.asyncio
async def test_login_sends_credentials_and_stores_token():
    client = _client()
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(return_value=_resp(LOGIN_OK))) as req:
        token = await client.login()
    assert token == "tok-1"
    body = req.await_args.kwargs["json"]
    assert body["variables"]["input"] == {"username": "admin", "password": "hunter2"}
    assert "login" in body["query"]


@pytest.mark.asyncio
async def test_execute_logs_in_once_then_reuses_the_token():
    client = _client()
    responses = [_resp(LOGIN_OK), _resp({"data": {"scryerVersion": "0.17.2"}}), _resp({"data": {"scryerVersion": "0.17.2"}})]
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(side_effect=responses)) as req:
        await client.execute("query { scryerVersion }")
        await client.execute("query { scryerVersion }")
    # login + 2 queries — the second query must NOT trigger another login
    assert req.await_count == 3
    assert req.await_args.kwargs["headers"]["Authorization"] == "Bearer tok-1"


@pytest.mark.asyncio
async def test_expired_token_triggers_relogin_on_http_401():
    """JWT TTL is 24h — the client must recover from an expired token itself."""
    client = _client()
    responses = [
        _resp(LOGIN_OK),
        _resp({"errors": [{"message": "unauthorized"}]}, status=401),
        _resp({"data": {"login": {"token": "tok-2", "expiresAt": None}}}),
        _resp({"data": {"scryerVersion": "0.17.2"}}),
    ]
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(side_effect=responses)) as req:
        data = await client.execute("query { scryerVersion }")
    assert data == {"scryerVersion": "0.17.2"}
    assert req.await_args.kwargs["headers"]["Authorization"] == "Bearer tok-2"


@pytest.mark.asyncio
async def test_graphql_unauthorized_error_at_http_200_triggers_relogin():
    """Scryer answers 200 with `errors` — an auth error there must relogin too."""
    client = _client()
    responses = [
        _resp(LOGIN_OK),
        _resp({"data": None, "errors": [{"message": "Unauthorized: token expired"}]}),
        _resp({"data": {"login": {"token": "tok-3", "expiresAt": None}}}),
        _resp({"data": {"scryerVersion": "0.17.2"}}),
    ]
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(side_effect=responses)):
        data = await client.execute("query { scryerVersion }")
    assert data == {"scryerVersion": "0.17.2"}


@pytest.mark.asyncio
async def test_relogin_is_attempted_only_once():
    """A permanently rejecting server must not loop — one retry, then raise."""
    client = _client()
    unauth = {"data": None, "errors": [{"message": "unauthorized"}]}
    responses = [_resp(LOGIN_OK), _resp(unauth), _resp(LOGIN_OK), _resp(unauth)]
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(side_effect=responses)):
        with pytest.raises(AuthenticationError):
            await client.execute("query { scryerVersion }")


@pytest.mark.asyncio
async def test_login_failure_raises_authentication_error():
    client = _client()
    bad = {"data": None, "errors": [{"message": "invalid credentials"}]}
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(return_value=_resp(bad))):
        with pytest.raises(AuthenticationError):
            await client.login()


# ------------------------------------------------------------------ errors
@pytest.mark.asyncio
async def test_graphql_errors_at_http_200_are_not_swallowed():
    """The documented Scryer footgun: errors arrive with HTTP 200 + data: null."""
    client = _client()
    payload = {"data": None, "errors": [{"message": "managed child indexers are controlled by their parent sync"}]}
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(side_effect=[_resp(LOGIN_OK), _resp(payload)])):
        with pytest.raises(ScryerGraphQLError) as exc:
            await client.execute("mutation { updateIndexerConfig }")
    assert "managed child indexers" in str(exc.value)
    assert isinstance(exc.value, APIError)


@pytest.mark.asyncio
async def test_partial_data_with_errors_still_raises():
    client = _client()
    payload = {"data": {"titles": None}, "errors": [{"message": "boom"}]}
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(side_effect=[_resp(LOGIN_OK), _resp(payload)])):
        with pytest.raises(ScryerGraphQLError):
            await client.execute("query { titles { items { id } } }")


# ------------------------------------------------------------------ secrets
def test_mask_release_secrets_hides_prowlarr_apikey_and_candidate_token():
    """SEC: downloadUrl embeds Prowlarr's apikey and candidateToken is a JWT
    that carries the same URL inside — neither may reach the logs."""
    raw = (
        "http://127.0.0.1:9696/2/download?apikey=6b7b4a9e4c7e4a3aa644bf8b0bf4036a"
        "&link=eWJXQjdiM3Nl&file=Apex"
    )
    masked = mask_release_secrets(raw)
    assert "6b7b4a9e4c7e4a3aa644bf8b0bf4036a" not in masked
    assert "eWJXQjdiM3Nl" not in masked
    assert "127.0.0.1:9696" in masked


def test_mask_release_secrets_handles_none_and_plain_text():
    assert mask_release_secrets(None) == ""
    assert mask_release_secrets("") == ""
    assert "magnet:?xt=urn:btih:" in mask_release_secrets("magnet:?xt=urn:btih:abc")


# ------------------------------------------------------------------ metadata
SEARCH_MULTI = {
    "data": {
        "searchMetadataMulti": {
            "movies": [
                {
                    "tvdbId": "6187", "name": "Dune: Part One", "imdbId": "tt1160419",
                    "year": 2021, "type": "movie", "slug": "dune-2021",
                    "posterUrl": "https://image.tmdb.org/t/p/w154/x.jpg",
                    "overview": "Наследник дома Атрейдесов…", "language": "eng",
                    "runtimeMinutes": 155,
                },
            ],
            "series": [
                {"tvdbId": "121361", "name": "Game of Thrones", "imdbId": "tt0944947",
                 "year": 2011, "type": "series", "slug": "game-of-thrones",
                 "posterUrl": None, "overview": None, "language": "eng", "runtimeMinutes": 60},
            ],
            "anime": [
                {"tvdbId": "424536", "name": "Frieren", "imdbId": None, "year": 2023,
                 "type": "anime", "slug": "frieren", "posterUrl": None, "overview": None,
                 "language": "jpn", "runtimeMinutes": 24},
            ],
        }
    }
}


@pytest.mark.asyncio
async def test_search_metadata_multi_maps_all_three_facets():
    client = _client()
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(side_effect=[_resp(LOGIN_OK), _resp(SEARCH_MULTI)])):
        result = await client.search_metadata_multi("Dune")

    movie = result[ContentType.MOVIE][0]
    assert isinstance(movie, MovieInfo)
    assert movie.title == "Dune: Part One"
    assert movie.year == 2021
    assert movie.imdb_id == "tt1160419"
    assert movie.metadata_id == "6187"

    series = result[ContentType.SERIES][0]
    assert isinstance(series, SeriesInfo)
    assert series.title == "Game of Thrones"

    anime = result[ContentType.ANIME][0]
    # Anime is a first-class facet in Scryer but rides the SeriesInfo model.
    assert isinstance(anime, SeriesInfo)
    assert anime.title == "Frieren"
    assert anime.facet == "ANIME"


@pytest.mark.asyncio
async def test_search_metadata_uses_the_requested_facet():
    client = _client()
    payload = {"data": {"searchMetadata": SEARCH_MULTI["data"]["searchMetadataMulti"]["anime"]}}
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(side_effect=[_resp(LOGIN_OK), _resp(payload)])) as req:
        items = await client.search_metadata("Frieren", ContentType.ANIME)
    assert req.await_args.kwargs["json"]["variables"]["type"] == "ANIME"
    assert items[0].title == "Frieren"


# ------------------------------------------------------------------ catalog
TITLE_ROW = {
    "id": "b0aaf081-194f-4d13-81d8-3f4c27a1d818",
    "name": "Apex", "facet": "MOVIE", "year": 2026, "monitored": True,
    "overview": None, "posterUrl": None, "runtimeMinutes": None, "imdbId": "tt16431404",
    "qualityTier": "4K Remux + 1080P Fallback", "currentQualityTier": "2160P",
    "rootFolderId": "9deb35b0", "libraryId": "movie_default_library",
    "externalIds": [{"source": "tvdb", "value": "358476"}, {"source": "tmdb", "value": "1318447"}],
    "mediaFiles": [{"id": "c3962282"}],
}


@pytest.mark.asyncio
async def test_find_title_matches_name_and_year():
    client = _client()
    payload = {"data": {"titles": {"items": [TITLE_ROW], "totalCount": 1, "hasMore": False}}}
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(side_effect=[_resp(LOGIN_OK), _resp(payload)])):
        found = await client.find_title(ContentType.MOVIE, "Apex", 2026)
    assert found is not None
    assert found.scryer_id == "b0aaf081-194f-4d13-81d8-3f4c27a1d818"
    assert found.has_file is True


@pytest.mark.asyncio
async def test_find_title_rejects_a_year_mismatch():
    client = _client()
    payload = {"data": {"titles": {"items": [TITLE_ROW], "totalCount": 1, "hasMore": False}}}
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(side_effect=[_resp(LOGIN_OK), _resp(payload)])):
        found = await client.find_title(ContentType.MOVIE, "Apex", 1999)
    assert found is None


# ------------------------------------------------------------------ releases
RELEASE_ROW = {
    "source": "RuTracker.org (torznab)",
    "title": "Apex [2026, WEB-DL 2160p, HDR10] Dub + Original (Eng)",
    "link": "file=Apex",
    "downloadUrl": "http://127.0.0.1:9696/2/download?apikey=SECRETKEY&link=ZZZ&file=Apex",
    "sourceKind": "TORRENT_FILE", "sizeBytes": 16701344369, "publishedAt": None,
    "seeders": 229, "peers": 255, "infoHash": None, "freeleech": None,
    "candidateToken": "eyJhbGciOiJIUzI1NiJ9.payload.sig",
    "autoEligible": True, "autoDecisionCode": "eligible", "autoDecisionSummary": "would grab",
    "parsedRelease": {
        "rawTitle": "Apex [2026, WEB-DL 2160p, HDR10]", "quality": "2160p", "source": "WEB-DL",
        "videoCodec": None, "audio": None, "isRemux": False, "isProperUpload": False,
        "episode": {"season": 1, "episodeNumbers": [5]},
    },
    "qualityProfileDecision": {
        "allowed": True, "blockCodes": [], "releaseScore": 2680, "preferenceScore": 2680,
    },
    "queueScope": {"__typename": "TitleScopePayload", "wholeTitle": True},
}


@pytest.mark.asyncio
async def test_search_releases_maps_scryer_fields_onto_search_result():
    client = _client()
    payload = {"data": {"searchReleases": [RELEASE_ROW]}}
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(side_effect=[_resp(LOGIN_OK), _resp(payload)])):
        results = await client.search_releases("title-id-1")

    r = results[0]
    assert r.title.startswith("Apex")
    assert r.size == 16701344369
    assert r.seeders == 229
    assert r.indexer == "RuTracker.org (torznab)"
    assert r.candidate_token == "eyJhbGciOiJIUzI1NiJ9.payload.sig"
    assert r.scryer_title_id == "title-id-1"
    assert r.scryer_score == 2680
    assert r.scryer_allowed is True
    assert r.auto_eligible is True
    assert r.quality.resolution == "2160p"
    assert r.quality.source == "WEB-DL"
    assert r.protocol == "torrent"


@pytest.mark.asyncio
async def test_search_releases_marks_blocked_releases():
    client = _client()
    blocked = dict(RELEASE_ROW)
    blocked["qualityProfileDecision"] = {
        "allowed": False, "blockCodes": ["russian_dub_without_english"],
        "releaseScore": -1000, "preferenceScore": -1000,
    }
    payload = {"data": {"searchReleases": [blocked]}}
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(side_effect=[_resp(LOGIN_OK), _resp(payload)])):
        results = await client.search_releases("t1")
    assert results[0].scryer_allowed is False
    assert results[0].block_codes == ["russian_dub_without_english"]


@pytest.mark.asyncio
async def test_search_releases_passes_season_and_episode():
    client = _client()
    payload = {"data": {"searchReleases": []}}
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(side_effect=[_resp(LOGIN_OK), _resp(payload)])) as req:
        await client.search_releases("t1", season=2, episode=5, limit=30)
    variables = req.await_args.kwargs["json"]["variables"]["input"]
    assert variables == {"titleId": "t1", "season": "2", "episode": "5", "limit": 30}


# ------------------------------------------------------------------ add/queue
@pytest.mark.asyncio
async def test_add_title_sends_anime_facet_and_external_ids():
    client = _client()
    payload = {"data": {"addTitle": {"title": TITLE_ROW, "reusedExistingTitle": False,
                                     "downloadJobId": None, "queuedDownload": None}}}
    anime = SeriesInfo(tvdb_id=0, title="Frieren", year=2023, metadata_id="424536", facet="ANIME")
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(side_effect=[_resp(LOGIN_OK), _resp(payload)])) as req:
        await client.add_title(anime, ContentType.ANIME, monitored=False)
    sent = req.await_args.kwargs["json"]["variables"]["input"]
    assert sent["facet"] == "ANIME"
    assert sent["monitored"] is False
    assert {"source": "tvdb", "value": "424536"} in sent["externalIds"]


@pytest.mark.asyncio
async def test_add_title_and_queue_download_uses_the_combined_mutation():
    client = _client()
    payload = {"data": {"addTitleAndQueueDownload": {
        "title": TITLE_ROW, "reusedExistingTitle": True, "downloadJobId": "job-1",
        "queuedDownload": {"status": "QUEUED", "jobId": "job-1", "titleId": "t1",
                           "titleName": "Apex", "conflict": None},
    }}}
    movie = MovieInfo(tmdb_id=0, title="Apex", year=2026, metadata_id="358476")
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(side_effect=[_resp(LOGIN_OK), _resp(payload)])) as req:
        result = await client.add_title_and_queue_download(movie, ContentType.MOVIE)
    assert "addTitleAndQueueDownload" in req.await_args.kwargs["json"]["query"]
    assert result.queued is True
    assert result.title.scryer_id == "b0aaf081-194f-4d13-81d8-3f4c27a1d818"


@pytest.mark.asyncio
async def test_queue_existing_title_download_defaults_to_whole_title_scope():
    client = _client()
    payload = {"data": {"queueExistingTitleDownload": {
        "status": "QUEUED", "jobId": "j1", "titleId": "t1", "titleName": "Apex", "conflict": None}}}
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(side_effect=[_resp(LOGIN_OK), _resp(payload)])) as req:
        queued = await client.queue_existing_title_download(title_id="t1", candidate_token="cand-token")
    sent = req.await_args.kwargs["json"]["variables"]["input"]
    assert sent["titleId"] == "t1"
    assert sent["candidateToken"] == "cand-token"
    assert sent["scope"] == {"title": True}
    assert queued.queued is True


@pytest.mark.asyncio
async def test_queue_conflict_is_reported_not_raised():
    client = _client()
    payload = {"data": {"queueExistingTitleDownload": {
        "status": "CONFLICT", "jobId": None, "titleId": "t1", "titleName": "Apex",
        "conflict": {"__typename": "QueueDownloadConflictPayload"}}}}
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(side_effect=[_resp(LOGIN_OK), _resp(payload)])):
        queued = await client.queue_existing_title_download(title_id="t1", candidate_token="cand")
    assert queued.queued is False
    assert queued.status == "CONFLICT"


# ------------------------------------------------------------------ misc
@pytest.mark.asyncio
async def test_check_connection_reports_version():
    client = _client()
    payload = {"data": {"scryerVersion": "0.17.2", "systemHealth": {"serviceReady": True}}}
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(side_effect=[_resp(LOGIN_OK), _resp(payload)])):
        ok, version, elapsed = await client.check_connection()
    assert ok is True
    assert version == "0.17.2"
    assert elapsed >= 0


@pytest.mark.asyncio
async def test_check_connection_survives_a_dead_server():
    client = _client()
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(side_effect=httpx.ConnectError("refused"))):
        ok, version, _elapsed = await client.check_connection()
    assert ok is False
    assert version is None


@pytest.mark.asyncio
async def test_quality_profiles_and_root_folders_are_cached():
    """PERF: profile/root-folder reads are TTL-cached like the *arr clients were."""
    client = _client()
    profiles = {"data": {"qualityProfileSettings": {
        "globalProfileId": "4k", "profiles": [{"id": "4k", "name": "4K Remux + 1080P Fallback"}]}}}
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(side_effect=[_resp(LOGIN_OK), _resp(profiles)])) as req:
        first = await client.get_quality_profiles()
        second = await client.get_quality_profiles()
    assert first == second
    assert first[0].id == "4k"
    assert req.await_count == 2  # login + one query only


@pytest.mark.asyncio
async def test_get_download_queue_maps_items():
    client = _client()
    payload = {"data": {"downloadQueue": [{
        "id": "q1", "titleId": "t1", "titleName": "Apex", "facet": "MOVIE",
        "state": "DOWNLOADING", "displayState": "DOWNLOADING", "progressPercent": 42,
        "sizeBytes": 100, "remainingSeconds": 60, "queuedAt": None, "clientName": "qbit",
        "attentionRequired": False, "attentionReason": None, "importStatus": None,
        "downloadId": "abc", "episodeId": None,
    }]}}
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(side_effect=[_resp(LOGIN_OK), _resp(payload)])):
        items = await client.get_download_queue()
    assert items[0].title_name == "Apex"
    assert items[0].progress_percent == 42
    assert items[0].content_type == ContentType.MOVIE


@pytest.mark.asyncio
async def test_system_health_maps_indexer_stats():
    client = _client()
    payload = {"data": {"systemHealth": {
        "serviceReady": True, "totalTitles": 42, "monitoredTitles": 39,
        "titlesMovie": 26, "titlesSeries": 16, "titlesAnime": 0,
        "indexerStats": [{"indexerName": "RuTracker.org", "queriesLast24H": 245,
                          "successfulLast24H": 153, "failedLast24H": 92}],
    }}}
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(side_effect=[_resp(LOGIN_OK), _resp(payload)])):
        health = await client.system_health()
    assert health.total_titles == 42
    assert health.titles_anime == 0
    assert health.indexers[0].name == "RuTracker.org"


@pytest.mark.asyncio
async def test_calendar_maps_episodes():
    client = _client()
    payload = {"data": {"calendarEpisodes": [{
        "id": "e1", "titleId": "t1", "titleName": "X-Men '97", "titleFacet": "series",
        "seasonNumber": "2", "episodeNumber": "1", "episodeTitle": "Days of Past Future",
        "airDate": "2026-07-01", "monitored": True,
    }]}}
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(side_effect=[_resp(LOGIN_OK), _resp(payload)])):
        items = await client.get_calendar("2026-07-01", "2026-07-31")
    assert items[0].title_name == "X-Men '97"
    assert items[0].season_number == 2
    assert items[0].episode_number == 1
