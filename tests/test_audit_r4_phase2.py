"""Round-4 audit Phase 2 — behavioural bug fixes (BUG-02/04/05, LOGIC-01/03)."""

from unittest.mock import AsyncMock

import pytest

from bot.models import QualityInfo, SearchResult


# ---------------------------------------------------------------------------
# BUG-02: leechers=0 must be preserved (not collapsed to None by truthiness)
# ---------------------------------------------------------------------------
def test_prowlarr_normalize_preserves_zero_leechers():
    from bot.clients.prowlarr import ProwlarrClient

    c = ProwlarrClient("http://x", "k")
    assert c._normalize_result({"guid": "g", "title": "t", "seeders": 10, "leechers": 0}).leechers == 0
    assert c._normalize_result({"guid": "g", "title": "t", "peers": 0}).leechers == 0
    assert c._normalize_result({"guid": "g", "title": "t", "leechers": 5}).leechers == 5
    assert c._normalize_result({"guid": "g", "title": "t"}).leechers is None


# ---------------------------------------------------------------------------
# BUG-05: add_torrent_url must treat any 2xx without "Fails." as success
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_add_torrent_url_treats_empty_2xx_as_success():
    from bot.clients.qbittorrent import QBittorrentClient

    c = QBittorrentClient("http://x", "u", "p")

    c._request = AsyncMock(return_value="Ok.")
    assert await c.add_torrent_url("magnet:?xt=urn:btih:abc") is True

    c._request = AsyncMock(return_value=None)  # qBit >=5.2 empty body on success
    assert await c.add_torrent_url("magnet:?xt=urn:btih:abc") is True

    c._request = AsyncMock(return_value="Fails.")  # explicit rejection
    assert await c.add_torrent_url("magnet:?xt=urn:btih:abc") is False


# ---------------------------------------------------------------------------
# LOGIC-01: REMUX bonus must apply even when no source token was parsed
# ---------------------------------------------------------------------------
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
