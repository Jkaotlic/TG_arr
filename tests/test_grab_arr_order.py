"""Audit 2026-09-23, Task B1: *arr-native ordering and "⚡ Лучший" gating.

GRAB-01: the bot sorted by customFormatScore before quality, so a 1080p
release with a higher CF score beat a 2160p one — and Radarr (profile 7,
upgradeAllowed, cutoff = 2160p) then re-downloaded the 4K anyway. *arr's own
order is qualityWeight first, customFormatScore second.

GRAB-02 (part) / GRAB-12: "⚡ Лучший" was gated on the bot's heuristic score
(80 — nothing is ever below it), so it could auto-take a release *arr itself
rejected. It must follow *arr's verdict.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.models import (
    ContentType,
    MovieInfo,
    QualityInfo,
    SearchResult,
    SearchSession,
    User,
    UserPreferences,
)
from bot.ui.callbacks import GrabBestCB


def _arr(guid: str, *, weight: int, cf: int, rejected: bool = False, res: str = "1080p") -> SearchResult:
    return SearchResult(
        guid=guid, title=f"Movie.{res}.{guid}", origin="arr", indexer_id=1,
        quality=QualityInfo(resolution=res), quality_weight=weight,
        custom_format_score=cf, rejected=rejected, size=1,
    )


# ---------------------------------------------------------------- parsing
def test_parse_release_reads_quality_weight_and_custom_format_names():
    from bot.clients.radarr import RadarrClient

    release = RadarrClient("http://radarr", "key")._parse_release({
        "guid": "g", "title": "Movie.2160p", "indexerId": 3,
        "qualityWeight": 1901,
        "customFormats": [{"id": 1, "name": "English"}, {"id": 2, "name": "RusSubs"}, {"id": 3}],
        "customFormatScore": 500,
        "languages": [{"id": 1, "name": "English"}],
    })

    assert release.quality_weight == 1901
    assert release.custom_formats == ["English", "RusSubs"]
    assert release.languages == ["English"]


def test_parse_release_without_quality_weight_leaves_it_unset():
    from bot.clients.radarr import RadarrClient

    release = RadarrClient("http://radarr", "key")._parse_release({"guid": "g", "title": "T"})
    assert release.quality_weight is None
    assert release.custom_formats == []


# ---------------------------------------------------------------- ordering
def test_arr_results_sort_quality_before_custom_format_score():
    """1080p CF500 vs 2160p CF250 → 2160p first (what Radarr itself would take)."""
    from bot.services.scoring import ScoringService

    hd = _arr("hd", weight=1000, cf=500, res="1080p")
    uhd = _arr("uhd", weight=1900, cf=250, res="2160p")

    ordered = ScoringService().sort_results([hd, uhd], ContentType.MOVIE)

    assert [r.guid for r in ordered] == ["uhd", "hd"]


def test_arr_results_rejected_sink_even_with_higher_quality():
    from bot.services.scoring import ScoringService

    bad = _arr("bad", weight=1900, cf=-1000, rejected=True, res="2160p")
    ok = _arr("ok", weight=1000, cf=0)

    ordered = ScoringService().sort_results([bad, ok], ContentType.MOVIE)
    assert [r.guid for r in ordered] == ["ok", "bad"]


def test_arr_results_ties_keep_arr_order():
    """Stable: equal (rejected, weight, CF) keeps *arr's own order, the bot's
    heuristic does not re-shuffle (it would otherwise prefer the seeded one)."""
    from bot.services.scoring import ScoringService

    first = _arr("first", weight=1000, cf=250)
    second = _arr("second", weight=1000, cf=250)
    second.seeders = 500  # the bot heuristic would promote this one

    ordered = ScoringService().sort_results([first, second], ContentType.MOVIE)
    assert [r.guid for r in ordered] == ["first", "second"]
    # the heuristic score is still written for display
    assert all(r.calculated_score != 0 for r in ordered)


def test_arr_results_with_higher_cf_win_within_same_quality():
    from bot.services.scoring import ScoringService

    low = _arr("low", weight=1000, cf=0)
    high = _arr("high", weight=1000, cf=500)
    ordered = ScoringService().sort_results([low, high], ContentType.MOVIE)
    assert [r.guid for r in ordered] == ["high", "low"]


# ---------------------------------------------------------------- best gating
@pytest.mark.parametrize(
    "results, expected",
    [
        ([_arr("a", weight=1900, cf=250)], "a"),
        ([_arr("a", weight=1900, cf=0)], "a"),
        ([_arr("a", weight=1900, cf=-1, rejected=False)], None),
        ([_arr("a", weight=1900, cf=500, rejected=True)], None),
        ([], None),
    ],
)
def test_grab_best_candidate_follows_arr_verdict(results, expected):
    from bot.services.scoring import grab_best_candidate

    best = grab_best_candidate(results, auto_grab_enabled=True, threshold=80)
    assert (best.guid if best else None) == expected


def test_grab_best_candidate_needs_auto_grab_enabled():
    from bot.services.scoring import grab_best_candidate

    assert grab_best_candidate([_arr("a", weight=1, cf=1)], auto_grab_enabled=False, threshold=0) is None


def test_grab_best_candidate_ignores_heuristic_threshold_for_arr_results():
    """GRAB-12: for *arr results the verdict decides, not the 0–150 heuristic."""
    from bot.services.scoring import grab_best_candidate

    r = _arr("a", weight=1, cf=0)
    r.calculated_score = 10
    assert grab_best_candidate([r], auto_grab_enabled=True, threshold=80) is r


def _kb_texts(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


@pytest.mark.asyncio
async def test_all_rejected_results_show_no_best_button():
    from bot.handlers.search import services

    results = [_arr("a", weight=1900, cf=0, rejected=True), _arr("b", weight=1000, cf=0, rejected=True)]
    message = MagicMock()
    message.edit_text = AsyncMock()
    db_user = User(tg_id=1, preferences=UserPreferences(auto_grab_enabled=True))
    settings = MagicMock(auto_grab_score_threshold=80)

    with patch.object(services, "safe_edit", AsyncMock()) as edit:
        await services._render_results_page(
            message, results, 0, 1, "q", ContentType.MOVIE, 5, db_user, settings,
        )

    markup = edit.await_args.kwargs["reply_markup"]
    assert not any("Лучший" in t for t in _kb_texts(markup))


@pytest.mark.asyncio
async def test_accepted_best_shows_best_button():
    from bot.handlers.search import services

    results = [_arr("a", weight=1900, cf=250, res="2160p")]
    results[0].calculated_score = 10  # heuristic below the threshold — irrelevant now
    message = MagicMock()
    db_user = User(tg_id=1, preferences=UserPreferences(auto_grab_enabled=True))
    settings = MagicMock(auto_grab_score_threshold=80)

    with patch.object(services, "safe_edit", AsyncMock()) as edit:
        await services._render_results_page(
            message, results, 0, 1, "q", ContentType.MOVIE, 5, db_user, settings,
        )

    markup = edit.await_args.kwargs["reply_markup"]
    assert any("Лучший" in t for t in _kb_texts(markup))


@pytest.mark.asyncio
async def test_handle_grab_best_refuses_a_rejected_first_result():
    """The keyboard may be old; the handler re-checks and does not grab."""
    from bot.handlers import search

    search._grab_in_progress.clear()
    rejected = _arr("bad", weight=1900, cf=500, rejected=True)
    session = SearchSession(
        user_id=1, query="q", content_type=ContentType.MOVIE, results=[rejected],
        selected_content=MovieInfo(tmdb_id=1, title="M", year=2024, radarr_id=5), list_token="tok123",
    )
    db = MagicMock()
    db.get_session = AsyncMock(return_value=session)
    db.save_session = AsyncMock()
    cb = MagicMock()
    cb.from_user = MagicMock(id=1)
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    db_user = User(tg_id=1, preferences=UserPreferences(auto_grab_enabled=True))

    with patch.object(search, "get_services", AsyncMock(return_value=(MagicMock(), MagicMock()))), \
         patch.object(search, "grab_release", AsyncMock()) as gr:
        await search.handle_grab_best(cb, GrabBestCB(tok="tok123"), db_user, db)

    gr.assert_not_awaited()
    assert cb.answer.await_count >= 1
