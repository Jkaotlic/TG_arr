"""Round-4 audit Phase 2 — behavioural bug fixes (BUG-02/04/05, LOGIC-01/03)."""



from bot.models import QualityInfo, SearchResult


# ---------------------------------------------------------------------------
# BUG-02: leechers=0 must be preserved (not collapsed to None by truthiness)
# ---------------------------------------------------------------------------
def test_scryer_release_preserves_zero_leechers():
    """Migration 2026-07-28: same invariant, new source. `0 leechers` must stay
    0 and not collapse to None — the release card renders "S/L" from it."""
    from bot.clients.scryer import ScryerClient

    client = ScryerClient("http://scryer", "admin", "pw")
    result = client._release_to_model(
        {
            "source": "RuTracker",
            "title": "Movie.2024.1080p",
            "seeders": 5,
            "peers": 0,
            "sizeBytes": 1024,
            "sourceKind": "TORRENT_FILE",
        },
        "title-1",
    )
    assert result.seeders == 5
    assert result.leechers == 0


def test_remux_bonus_applies_without_source_token():
    from bot.services.scoring import ScoringService

    s = ScoringService()
    remux = SearchResult(guid="g", title="t", quality=QualityInfo(is_remux=True))
    plain = SearchResult(guid="g", title="t", quality=QualityInfo())
    assert s.calculate_score(remux) - s.calculate_score(plain) == s.weights.source_remux


# ---------------------------------------------------------------------------
# LOGIC-03: language penalties must only fire on scene tags, not real titles
# ---------------------------------------------------------------------------
def test_language_penalty_only_on_scene_tags_not_titles():
    from bot.services.scoring import ScoringWeights

    w = ScoringWeights()

    def pen(title: str) -> int:
        return sum(p for pat, p in w._bad_keyword_patterns if pat.search(title))

    # Legit titles containing a language word must NOT be penalised.
    assert pen("The French Dispatch 2021 1080p") == 0
    assert pen("The Italian Job 2003 1080p") == 0
    # Scene-tagged language (dot/dash separated) SHOULD be penalised.
    assert pen("Some.Movie.2021.FRENCH.1080p.x264") == w.bad_keywords["french"]
    # Strong keywords stay penalised regardless of separators.
    assert pen("Movie sample 1080p") == w.bad_keywords["sample"]


# ---------------------------------------------------------------------------
# BUG-04: single targeted season must not be added with a monitor-everything type
# ---------------------------------------------------------------------------
def test_decide_monitor_type_single_season_not_all():
    from bot.handlers.search import _decide_monitor_type

    pack = SearchResult(guid="g", title="t", is_season_pack=True)
    single = SearchResult(guid="g", title="t", detected_season=2, is_season_pack=False)
    full = SearchResult(guid="g", title="t")

    assert _decide_monitor_type(pack, force_download=False) == "all"
    assert _decide_monitor_type(single, force_download=False) == "none"
    assert _decide_monitor_type(full, force_download=False) == "all"
    assert _decide_monitor_type(single, force_download=True) == "all"
