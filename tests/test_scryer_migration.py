"""Behaviour tests for the Scryer migration (2026-07-28).

Covers the parts of the flow that changed shape, not just moved:
scoring now defers to Scryer's verdict, detection is one metadata call,
and grabbing queues a candidate token instead of pushing a release URL.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.models import ArtistInfo, ContentType, MovieInfo, QualityInfo, SearchResult, SeriesInfo
from bot.services.add_service import AddService
from bot.services.scoring import ScoringService
from bot.services.search_service import SearchService


def _release(title: str, *, score=None, allowed=None, resolution="1080p", seeders=10) -> SearchResult:
    return SearchResult(
        guid=title,
        title=title,
        indexer="RuTracker",
        size=8 * 1024**3,
        seeders=seeders,
        quality=QualityInfo(resolution=resolution, source="WEB-DL"),
        scryer_score=score,
        scryer_allowed=allowed,
        candidate_token=f"tok-{title}",
        scryer_title_id="t1",
    )


# ------------------------------------------------------------------ scoring
def test_scryer_verdict_outranks_local_score():
    """Scryer applies the quality profile AND the Rego rules (ENG audio +250,
    RU subs +250, RU dub without ENG -1000). The bot must not fight that: a
    release Scryer scores higher wins even if the local heuristic disagrees."""
    scoring = ScoringService()
    # 2160p would win on local scoring alone, but Scryer ranks it far lower
    # (e.g. Russian dub without an English track).
    local_favourite = _release("Movie.2160p.WEB-DL", score=-800, allowed=True, resolution="2160p")
    scryer_favourite = _release("Movie.1080p.WEB-DL.ENG", score=2680, allowed=True, resolution="1080p")

    ordered = scoring.sort_results([local_favourite, scryer_favourite], ContentType.MOVIE)
    assert ordered[0] is scryer_favourite


def test_blocked_releases_sink_below_allowed_ones():
    scoring = ScoringService()
    blocked = _release("Blocked.2160p.REMUX", score=5000, allowed=False, resolution="2160p")
    allowed = _release("Allowed.1080p", score=10, allowed=True)

    ordered = scoring.sort_results([blocked, allowed], ContentType.MOVIE)
    assert ordered[0] is allowed
    assert ordered[1] is blocked


def test_local_scoring_still_used_when_scryer_has_no_verdict():
    """Releases without a Scryer decision (older sessions) keep the old order."""
    scoring = ScoringService()
    good = _release("Movie.2160p.BluRay.REMUX", resolution="2160p")
    poor = _release("Movie.480p.CAMRip", resolution="480p")

    ordered = scoring.sort_results([poor, good], ContentType.MOVIE)
    assert ordered[0] is good
    assert all(r.calculated_score for r in ordered)


def test_scryer_score_breaks_ties_by_local_score():
    scoring = ScoringService()
    a = _release("A.2160p.REMUX", score=100, allowed=True, resolution="2160p")
    b = _release("B.480p", score=100, allowed=True, resolution="480p")
    ordered = scoring.sort_results([b, a], ContentType.MOVIE)
    assert ordered[0] is a


# ---------------------------------------------------------------- detection
def _search_service(scryer) -> SearchService:
    return SearchService(scryer, scoring=ScoringService())


@pytest.mark.asyncio
async def test_detection_uses_one_metadata_call():
    """The old flow fired parallel Radarr+Sonarr+Lidarr lookups behind a
    semaphore and a circuit breaker. Scryer answers all three video facets in
    a single query."""
    scryer = MagicMock()
    scryer.search_metadata_multi = AsyncMock(return_value={
        ContentType.MOVIE: [MovieInfo(title="Dune: Part One", year=2021, metadata_id="6187")],
        ContentType.SERIES: [],
        ContentType.ANIME: [],
    })
    service = _search_service(scryer)

    result = await service.detect_with_confidence("Dune Part One")

    scryer.search_metadata_multi.assert_awaited_once()
    assert result.content_type == ContentType.MOVIE
    assert result.confidence > 0.7
    assert result.lookup_results and result.lookup_results[0].title == "Dune: Part One"


@pytest.mark.asyncio
async def test_anime_is_detected_as_its_own_facet():
    scryer = MagicMock()
    scryer.search_metadata_multi = AsyncMock(return_value={
        ContentType.MOVIE: [],
        ContentType.SERIES: [SeriesInfo(title="Frieren Beyond Journeys End", year=2023, facet="SERIES")],
        ContentType.ANIME: [SeriesInfo(title="Frieren", year=2023, facet="ANIME", metadata_id="424536")],
    })
    service = _search_service(scryer)

    result = await service.detect_with_confidence("Frieren")

    assert result.content_type == ContentType.ANIME
    assert result.lookup_results[0].facet == "ANIME"


@pytest.mark.asyncio
async def test_detection_falls_back_to_unknown_when_scryer_is_down():
    scryer = MagicMock()
    scryer.search_metadata_multi = AsyncMock(side_effect=RuntimeError("connection refused"))
    service = _search_service(scryer)

    result = await service.detect_with_confidence("Whatever")

    assert result.content_type == ContentType.UNKNOWN
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_episodic_query_still_distinguishes_series_from_anime():
    """A season marker used to short-circuit straight to SERIES. With anime as
    its own Scryer library (and its own quality profile), that would file every
    "Frieren S01E05" into the wrong library — so the marker now only narrows
    the candidates to series-vs-anime instead of deciding by itself."""
    scryer = MagicMock()
    scryer.search_metadata_multi = AsyncMock(return_value={
        ContentType.MOVIE: [MovieInfo(title="Frieren The Movie", year=2025)],
        ContentType.SERIES: [],
        ContentType.ANIME: [SeriesInfo(title="Frieren", year=2023, facet="ANIME")],
    })
    service = _search_service(scryer)

    result = await service.detect_with_confidence("Frieren S01E05")

    assert result.content_type == ContentType.ANIME


@pytest.mark.asyncio
async def test_episodic_query_falls_back_to_series_when_metadata_is_down():
    """The season marker is still strong evidence on its own — a metadata
    outage must not bounce an unambiguous "S01E05" back at the user."""
    scryer = MagicMock()
    scryer.search_metadata_multi = AsyncMock(side_effect=RuntimeError("down"))
    service = _search_service(scryer)

    result = await service.detect_with_confidence("Breaking Bad S01E05")

    assert result.content_type == ContentType.SERIES
    assert result.confidence == 0.9


@pytest.mark.asyncio
async def test_episodic_query_never_resolves_to_music():
    scryer = MagicMock()
    scryer.search_metadata_multi = AsyncMock(return_value={
        ContentType.MOVIE: [], ContentType.SERIES: [], ContentType.ANIME: [],
    })
    lidarr = MagicMock()
    lidarr.lookup_artist = AsyncMock(return_value=[])
    service = SearchService(scryer, scoring=ScoringService(), lidarr=lidarr)

    result = await service.detect_with_confidence("Some Band S01E01")

    lidarr.lookup_artist.assert_not_awaited()
    assert result.content_type == ContentType.SERIES


# --------------------------------------------------------------- add / grab
@pytest.mark.asyncio
async def test_ensure_title_reuses_an_existing_catalog_entry():
    """Searching twice must not create a duplicate title in Scryer."""
    existing = MovieInfo(title="Apex", year=2026, scryer_id="known-id")
    scryer = MagicMock()
    scryer.find_title = AsyncMock(return_value=existing)
    scryer.add_title = AsyncMock()
    service = AddService(scryer)

    title, created = await service.ensure_title(
        MovieInfo(title="Apex", year=2026, metadata_id="358476"), ContentType.MOVIE
    )

    assert title.scryer_id == "known-id"
    assert created is False
    scryer.add_title.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_title_adds_unmonitored_so_search_does_not_pollute_the_library():
    """A release search needs a titleId, so the title has to exist first — but
    merely *looking* must not start monitoring it. Monitoring is switched on
    when the user actually grabs something."""
    added = MovieInfo(title="Apex", year=2026, scryer_id="new-id")
    scryer = MagicMock()
    scryer.find_title = AsyncMock(return_value=None)
    scryer.add_title = AsyncMock(return_value=MagicMock(title=added, reused_existing=False))
    service = AddService(scryer)

    title, created = await service.ensure_title(
        MovieInfo(title="Apex", year=2026, metadata_id="358476"), ContentType.MOVIE
    )

    assert created is True
    assert title.scryer_id == "new-id"
    assert scryer.add_title.await_args.kwargs["monitored"] is False


@pytest.mark.asyncio
async def test_grab_queues_the_candidate_and_starts_monitoring():
    scryer = MagicMock()
    scryer.find_title = AsyncMock(return_value=MovieInfo(title="Apex", year=2026, scryer_id="t1"))
    scryer.queue_existing_title_download = AsyncMock(
        return_value=MagicMock(queued=True, status="QUEUED", job_id="job-1")
    )
    scryer.set_title_monitored = AsyncMock(return_value=True)
    service = AddService(scryer)

    release = _release("Apex.2160p.WEB-DL", score=2680, allowed=True)
    release.queue_scope = {"title": True}
    ok, action, message = await service.grab_release(
        MovieInfo(title="Apex", year=2026, scryer_id="t1"), release, ContentType.MOVIE
    )

    assert ok is True
    assert action.success is True
    scryer.queue_existing_title_download.assert_awaited_once()
    kwargs = scryer.queue_existing_title_download.await_args.kwargs
    assert kwargs["candidate_token"] == release.candidate_token
    assert kwargs["scope"] == {"title": True}
    scryer.set_title_monitored.assert_awaited_once_with("t1", True)
    assert "очередь" in message.lower() or "скач" in message.lower()


@pytest.mark.asyncio
async def test_grab_reports_a_conflict_without_raising():
    scryer = MagicMock()
    scryer.queue_existing_title_download = AsyncMock(
        return_value=MagicMock(queued=False, status="CONFLICT", job_id=None)
    )
    scryer.set_title_monitored = AsyncMock(return_value=True)
    service = AddService(scryer)

    ok, action, message = await service.grab_release(
        MovieInfo(title="Apex", year=2026, scryer_id="t1"),
        _release("Apex.2160p", score=1, allowed=True),
        ContentType.MOVIE,
    )

    assert ok is False
    assert action.success is False
    assert message


@pytest.mark.asyncio
async def test_grab_without_a_candidate_token_fails_cleanly():
    """A session restored from before the migration has no candidate token."""
    scryer = MagicMock()
    scryer.queue_existing_title_download = AsyncMock()
    service = AddService(scryer)

    stale = _release("Old.Release")
    stale.candidate_token = None
    ok, _action, message = await service.grab_release(
        MovieInfo(title="Apex", year=2026, scryer_id="t1"), stale, ContentType.MOVIE
    )

    assert ok is False
    scryer.queue_existing_title_download.assert_not_awaited()
    assert "повтор" in message.lower()


@pytest.mark.asyncio
async def test_auto_grab_delegates_the_release_choice_to_scryer():
    """"Скачать лучшее" hands the decision to Scryer's own profile+rules."""
    scryer = MagicMock()
    outcome = MagicMock(queued=True, reused_existing=False)
    outcome.title = MovieInfo(title="Apex", year=2026, scryer_id="t1")
    scryer.add_title_and_queue_download = AsyncMock(return_value=outcome)
    service = AddService(scryer)

    ok, _action, _message = await service.add_and_queue_best(
        MovieInfo(title="Apex", year=2026, metadata_id="358476"), ContentType.MOVIE
    )

    assert ok is True
    scryer.add_title_and_queue_download.assert_awaited_once()
    assert scryer.add_title_and_queue_download.await_args.kwargs["monitored"] is True


# ------------------------------------------------------------- no dead ports
def test_no_references_to_the_dead_arr_ports():
    """Sonarr (:8989) and Radarr (:7878) are stopped for good."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for path in list(root.glob("bot/**/*.py")) + [root / ".env.example", root / "README.md"]:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if ":8989" in text or ":7878" in text:
            offenders.append(str(path.relative_to(root)))
    assert not offenders, f"dead *arr ports still referenced in: {offenders}"


def test_arr_clients_are_gone():
    import pathlib

    clients = pathlib.Path(__file__).resolve().parents[1] / "bot" / "clients"
    assert not (clients / "radarr.py").exists()
    assert not (clients / "sonarr.py").exists()
    assert not (clients / "prowlarr.py").exists()


# ------------------------------------------------------ title-form matching
@pytest.mark.asyncio
async def test_query_matching_the_head_of_a_longer_title_still_wins():
    """Live regression: "Frieren" returned UNKNOWN because the metadata title is
    "Frieren: Beyond Journey's End" — the substring bonus scaled by length
    ratio (7/29) scored it *lower* than the plain fuzzy match, so it never
    cleared the 0.7 confidence bar and the user got a pointless question."""
    scryer = MagicMock()
    scryer.search_metadata_multi = AsyncMock(return_value={
        ContentType.MOVIE: [],
        ContentType.SERIES: [],
        ContentType.ANIME: [
            SeriesInfo(title="Frieren: Beyond Journey's End", year=2023,
                       facet="ANIME", slug="sousou-no-frieren"),
        ],
    })
    service = _search_service(scryer)

    result = await service.detect_with_confidence("Frieren")

    assert result.content_type == ContentType.ANIME
    assert result.confidence >= 0.7


@pytest.mark.asyncio
async def test_matching_can_use_the_slug_when_the_title_is_localised():
    """Metadata titles are localised; the slug stays in latin script."""
    scryer = MagicMock()
    scryer.search_metadata_multi = AsyncMock(return_value={
        ContentType.MOVIE: [],
        ContentType.SERIES: [],
        ContentType.ANIME: [
            SeriesInfo(title="Провожающая в последний путь Фрирен", year=2023,
                       facet="ANIME", slug="sousou-no-frieren"),
        ],
    })
    service = _search_service(scryer)

    result = await service.detect_with_confidence("Sousou no Frieren")

    assert result.content_type == ContentType.ANIME


def test_head_match_does_not_promote_an_unrelated_title():
    """The head-of-title rule must not turn "Dune" into a match for
    "Dune Drifter: Something Else"'s unrelated neighbours."""
    service = _search_service(MagicMock())
    score = service._best_match_score(
        "friesenblut", [SeriesInfo(title="Frieren: Beyond Journey's End", slug="sousou-no-frieren")],
        None, prefer_year=False,
    )
    assert score < 0.7


@pytest.mark.asyncio
async def test_anime_over_series_is_not_re_asked_as_ambiguous():
    """Live regression: series and anime hold the SAME metadata entry
    ("Frieren: Beyond Journey's End" is in both facets), so both score 1.0.
    The anime-over-series rule picked ANIME — and then the generic ambiguity
    check overrode it back to UNKNOWN and asked the user a question they
    cannot answer better than we can."""
    entry = SeriesInfo(title="Frieren: Beyond Journey's End", year=2023, slug="sousou-no-frieren")
    scryer = MagicMock()
    scryer.search_metadata_multi = AsyncMock(return_value={
        ContentType.MOVIE: [],
        ContentType.SERIES: [entry.model_copy(update={"facet": "SERIES"})],
        ContentType.ANIME: [entry.model_copy(update={"facet": "ANIME"})],
    })
    service = _search_service(scryer)

    result = await service.detect_with_confidence("Frieren")

    assert result.content_type == ContentType.ANIME
    assert result.lookup_results and result.lookup_results[0].facet == "ANIME"


@pytest.mark.asyncio
async def test_a_genuine_cross_type_tie_still_asks_the_user():
    """The ambiguity check must still fire when the tie is movie-vs-music —
    "Metallica" is both a band and a concert film, and only the user knows."""
    scryer = MagicMock()
    scryer.search_metadata_multi = AsyncMock(return_value={
        ContentType.MOVIE: [MovieInfo(title="Metallica", year=2013)],
        ContentType.SERIES: [],
        ContentType.ANIME: [],
    })
    lidarr = MagicMock()
    lidarr.lookup_artist = AsyncMock(return_value=[ArtistInfo(mb_id="mb-1", name="Metallica")])
    service = SearchService(scryer, scoring=ScoringService(), lidarr=lidarr)

    result = await service.detect_with_confidence("Metallica")

    assert result.content_type == ContentType.UNKNOWN
    assert result.reason == "ambiguous"
