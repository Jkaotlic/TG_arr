"""Tests for UI formatters (BUG-11 timezone, BUG-12 truncation)."""

from datetime import datetime, timezone

import pytest

from bot.models import ActionLog, ActionType, ContentType, SearchResult, TorrentInfo, TorrentState
from bot.ui.formatters import Formatters


def _has_moscow_tz() -> bool:
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo("Europe/Moscow")
        return True
    except Exception:
        return False


@pytest.mark.skipif(
    not _has_moscow_tz(),
    reason="IANA tz database not available on this runner (install tzdata).",
)
def test_calendar_respects_settings_timezone(monkeypatch):
    """BUG-11: calendar groups episodes by *local* calendar day (tz-aware)."""
    # Force Europe/Moscow timezone
    monkeypatch.setenv("TIMEZONE", "Europe/Moscow")
    from bot.config import get_settings

    get_settings.cache_clear()

    episodes = [
        {
            "air_date": "2026-04-18T22:00:00Z",  # 2026-04-19 01:00 MSK
            "series_title": "Test Show",
            "title": "Finale",
            "season": 1,
            "episode": 10,
            "has_file": False,
        }
    ]
    out = Formatters.format_calendar(episodes=episodes, movies=[], days=7)
    # In MSK this airs on the 19th — heading should reference 19
    assert "19" in out
    assert "апреля" in out


def test_format_calendar_truncates_safely():
    """BUG-12: long calendar stays <= 3800 chars and does not break HTML tags."""
    # Build a large number of fake episodes to force truncation
    episodes = [
        {
            "air_date": f"2026-04-{(i % 28) + 1:02d}T12:00:00Z",
            "series_title": f"Series Number {i:03d} With A Long Enough Title",
            "title": f"Episode Title Number {i:03d} Also Long",
            "season": 1,
            "episode": i,
            "has_file": False,
        }
        for i in range(200)
    ]
    out = Formatters.format_calendar(episodes=episodes, movies=[], days=7)
    assert len(out) <= 3800
    # Must not end with an unclosed HTML tag (no dangling '<' without '>')
    # Simple check: last '<' must be matched by a later '>'
    last_open = out.rfind("<")
    last_close = out.rfind(">")
    assert last_close > last_open, "Truncation left an unclosed HTML tag"


# ---------------------------------------------------------------------------
# BUG-06: "today" for the calendar header must use settings.timezone, not UTC
# — between 00:00 and 03:00 MSK, the UTC date is still "yesterday" and an
# episode airing "today" in Moscow was mislabelled "tomorrow". All other
# datetimes rendered to the user (publish_date, torrent added/completed,
# history) must also go through local-time conversion.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    not _has_moscow_tz(),
    reason="IANA tz database not available on this runner (install tzdata).",
)
def test_calendar_today_uses_local_timezone_not_utc(monkeypatch):
    """23:30 UTC on day N is already day N+1 in Moscow (UTC+3) — the 'сегодня'
    marker must land on the episode airing at that local instant."""
    monkeypatch.setenv("TIMEZONE", "Europe/Moscow")
    from bot.config import get_settings

    get_settings.cache_clear()

    # "Now" is 2026-04-18 23:30 UTC == 2026-04-19 02:30 MSK.
    import bot.ui.formatters as formatters_mod

    fixed_now = datetime(2026, 4, 18, 23, 30, tzinfo=timezone.utc)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now.astimezone(tz) if tz is not None else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr(formatters_mod, "datetime", _FixedDateTime)

    episodes = [
        {
            # Airs at the same local instant as "now" — should be "сегодня" (19th, MSK).
            "air_date": "2026-04-18T23:30:00Z",
            "series_title": "Test Show",
            "title": "Finale",
            "season": 1,
            "episode": 10,
            "has_file": False,
        }
    ]
    out = Formatters.format_calendar(episodes=episodes, movies=[], days=7)
    assert "сегодня" in out, out


def test_to_local_converts_utc_to_configured_timezone(monkeypatch):
    """_to_local(dt) converts a UTC-aware datetime into settings.timezone."""
    monkeypatch.setenv("TIMEZONE", "Europe/Moscow")
    from bot.config import get_settings

    get_settings.cache_clear()

    dt_utc = datetime(2026, 4, 18, 21, 0, tzinfo=timezone.utc)  # 00:00 MSK next day
    local = Formatters._to_local(dt_utc)
    assert local.hour == 0
    assert local.day == 19


def test_to_local_treats_naive_datetime_as_utc(monkeypatch):
    """A naive datetime (no tzinfo) is assumed UTC before conversion."""
    monkeypatch.setenv("TIMEZONE", "Europe/Moscow")
    from bot.config import get_settings

    get_settings.cache_clear()

    dt_naive = datetime(2026, 4, 18, 21, 0)
    local = Formatters._to_local(dt_naive)
    assert local.hour == 0
    assert local.day == 19


def test_to_local_returns_none_for_none():
    assert Formatters._to_local(None) is None


def test_release_details_publish_date_shown_in_local_timezone(monkeypatch):
    """format_release_details renders publish_date converted to settings.timezone."""
    monkeypatch.setenv("TIMEZONE", "Europe/Moscow")
    from bot.config import get_settings

    get_settings.cache_clear()

    result = SearchResult(
        guid="g1",
        title="Test.Release",
        publish_date=datetime(2026, 4, 18, 21, 0, tzinfo=timezone.utc),  # 00:00 MSK next day
    )
    out = Formatters.format_release_details(result)
    assert "19.04.2026 00:00" in out


def test_torrent_details_dates_shown_in_local_timezone(monkeypatch):
    """format_torrent_details renders added_on/completion_on in settings.timezone."""
    monkeypatch.setenv("TIMEZONE", "Europe/Moscow")
    from bot.config import get_settings

    get_settings.cache_clear()

    torrent = TorrentInfo(
        hash="a" * 40,
        name="Test Torrent",
        size=1000,
        progress=1.0,
        state=TorrentState.COMPLETED,
        added_on=datetime(2026, 4, 18, 21, 0, tzinfo=timezone.utc),
        completion_on=datetime(2026, 4, 18, 22, 0, tzinfo=timezone.utc),
    )
    out = Formatters.format_torrent_details(torrent)
    assert "19.04.2026 00:00" in out  # added_on
    assert "19.04.2026 01:00" in out  # completion_on


def test_download_complete_notification_shown_in_local_timezone(monkeypatch):
    """format_download_complete_notification renders completion_on in local tz."""
    monkeypatch.setenv("TIMEZONE", "Europe/Moscow")
    from bot.config import get_settings

    get_settings.cache_clear()

    torrent = TorrentInfo(
        hash="a" * 40,
        name="Test Torrent",
        size=1000,
        progress=1.0,
        state=TorrentState.COMPLETED,
        completion_on=datetime(2026, 4, 18, 21, 0, tzinfo=timezone.utc),
    )
    out = Formatters.format_download_complete_notification(torrent)
    assert "19.04.2026 00:00" in out


def test_action_log_history_shown_in_local_timezone(monkeypatch):
    """format_action_log renders created_at in settings.timezone."""
    monkeypatch.setenv("TIMEZONE", "Europe/Moscow")
    from bot.config import get_settings

    get_settings.cache_clear()

    action = ActionLog(
        user_id=1,
        action_type=ActionType.SEARCH,
        content_type=ContentType.MOVIE,
        query="Dune",
        created_at=datetime(2026, 4, 18, 21, 30, tzinfo=timezone.utc),  # 00:30 MSK next day
    )
    out = Formatters.format_action_log([action])
    assert "19.04 00:30" in out


# ---------------------------------------------------------------------------
# DEAD-14: except-clause simplified to plain `except Exception`; ZoneInfo
# object cached at module level instead of re-constructed per call.
# ---------------------------------------------------------------------------
def test_zoneinfo_is_cached_across_calls(monkeypatch):
    """Repeated _to_local calls with the same timezone must reuse a cached
    ZoneInfo instance rather than re-parsing tzdata every time."""
    monkeypatch.setenv("TIMEZONE", "Europe/Moscow")
    from bot.config import get_settings

    get_settings.cache_clear()

    Formatters._to_local(datetime(2026, 4, 18, 21, 0, tzinfo=timezone.utc))
    tz1 = Formatters._get_cached_zoneinfo("Europe/Moscow")
    tz2 = Formatters._get_cached_zoneinfo("Europe/Moscow")
    assert tz1 is tz2


# ---------------------------------------------------------------------------
# BUG-11/TEST-07: _safe_truncate branch coverage + format_search_results_page
# must never exceed Telegram's 4096-char message limit.
# ---------------------------------------------------------------------------
class TestSafeTruncate:
    def test_text_within_budget_returned_unchanged(self):
        text = "short text\nwith a newline"
        assert Formatters._safe_truncate(text, max_len=3800) == text

    def test_cuts_at_last_newline_within_budget(self):
        # Build text so the last newline before max_len lands well before the end.
        text = "A" * 20 + "\n" + "B" * 200
        out = Formatters._safe_truncate(text, max_len=60)
        assert out.startswith("A" * 20)
        assert out.endswith("... (truncated)")
        assert "B" not in out

    def test_no_newline_in_budget_falls_back_to_char_cut(self):
        # No newlines at all — must still respect max_len and not crash.
        text = "X" * 5000
        out = Formatters._safe_truncate(text, max_len=100)
        assert len(out) <= 100
        assert out.endswith("... (truncated)")

    def test_does_not_break_html_tag_on_cut(self):
        # Force the cut boundary to fall inside an unclosed HTML tag.
        prefix = "y" * 40
        text = prefix + "<b>" + ("z" * 200)
        out = Formatters._safe_truncate(text, max_len=45)
        last_open = out.rfind("<")
        last_close = out.rfind(">")
        assert last_close > last_open or last_open == -1, out

    def test_budget_non_positive_hard_cuts(self):
        """max_len smaller than the suffix length: hard character cut."""
        text = "A" * 100
        out = Formatters._safe_truncate(text, max_len=5)
        assert out == text[:5]


def test_format_search_results_page_stays_within_telegram_limit():
    """BUG-11: 5 releases with 300-char titles + emoji-laden quality info must
    still fit Telegram's 4096-char message cap."""
    long_title = "🎬 Тестовый Релиз " + ("Очень.Длинное.Название.Релиза." * 15) + " 2160p"
    assert len(long_title) > 300

    results = [
        SearchResult(
            guid=f"g{i}",
            title=long_title,
            indexer="SomeVeryLongIndexerNameForTesting",
            size=5 * 1024 ** 3,
            seeders=100,
            leechers=10,
            calculated_score=42,
        )
        for i in range(5)
    ]

    out = Formatters.format_search_results_page(
        results, page=0, total_pages=1, query="test query", content_type=ContentType.MOVIE, per_page=5
    )
    assert len(out) <= 4096


def test_format_search_result_truncates_long_title():
    """BUG-11: an individual result's title is capped (~150 chars) before display."""
    huge_title = "A" * 500
    result = SearchResult(guid="g1", title=huge_title, calculated_score=10)
    out = Formatters.format_search_result(result, 1)
    # The rendered title portion must be materially shorter than the raw 500 chars.
    assert len(out) < 250


# ---------------------------------------------------------------------------
# TEST-17: edge cases — emoji/unicode titles, extreme lengths.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "title",
    [
        "🎬🔥 Movie.Title.With.Emoji.2024.1080p 🚀✨",
        "Фильм на русском 2024 года выпуска 1080p",
        "映画のタイトル 2024",
        "A" * 300,
        "Mixed 混合 🎭 Title 2024",
    ],
)
def test_format_search_result_handles_unicode_and_long_titles(title):
    """TEST-17: emoji/unicode/extremely long titles never crash formatting
    and always produce an HTML-safe, bounded string."""
    result = SearchResult(guid="g1", title=title, calculated_score=10)
    out = Formatters.format_search_result(result, 1)
    assert isinstance(out, str)
    assert len(out) < 400


# ---------------------------------------------------------------------------
# Task 11: the release card surfaces *arr's own verdict (customFormatScore /
# rejected / rejections) instead of a second, bot-local language policy.
# ---------------------------------------------------------------------------
def test_formatter_shows_why_arr_refuses_a_release():
    """"Rejected" alone is useless — the reason is the actionable part."""
    from bot.models import QualityInfo, SearchResult
    from bot.ui.formatters.search import format_release

    release = SearchResult(
        guid="g1",
        title="Дюна 2021 2160p DUB", download_url="u", indexer="RuTracker",
        size=40_000_000_000, seeders=50, leechers=1, quality=QualityInfo(resolution="2160p"),
        origin="arr", rejected=True, rejections=["English is wanted, but found Russian"],
        custom_format_score=-1000,
    )

    text = format_release(release)

    assert "English is wanted, but found Russian" in text


def test_formatter_shows_arr_accepted_score_when_nonzero():
    """An accepted release still carries a customFormatScore worth showing
    (e.g. +250 for an English-audio release) — it explains why *arr ranked
    it where it did."""
    from bot.models import QualityInfo, SearchResult
    from bot.ui.formatters.search import format_release

    release = SearchResult(
        guid="g2",
        title="Dune 2021 2160p BluRay ENG", download_url="u", indexer="Knaben",
        size=40_000_000_000, seeders=200, leechers=1, quality=QualityInfo(resolution="2160p"),
        origin="arr", rejected=False, custom_format_score=250,
    )

    text = format_release(release)

    assert "250" in text


def test_formatter_distinguishes_no_verdict_from_accepted():
    """A Prowlarr free-text hit (origin='prowlarr') never went through *arr's
    profile — custom_format_score sits at its unset default of 0 and
    rejections is empty not because the release passed, but because nobody
    looked. The card must say so plainly and must NOT read as an *arr
    approval, which is what an accepted arr-origin release with the same
    zero score looks like.
    """
    from bot.models import QualityInfo, SearchResult
    from bot.ui.formatters.search import format_release

    no_verdict = SearchResult(
        guid="g3", title="Some.Movie.2021.2160p", download_url="u", indexer="Prowlarr free-text",
        size=40_000_000_000, seeders=10, leechers=1, quality=QualityInfo(resolution="2160p"),
        origin="prowlarr", rejected=False, custom_format_score=0,
    )
    accepted_zero_score = SearchResult(
        guid="g4", title="Some.Movie.2021.2160p", download_url="u", indexer="Radarr",
        size=40_000_000_000, seeders=10, leechers=1, quality=QualityInfo(resolution="2160p"),
        origin="arr", rejected=False, custom_format_score=0,
    )

    no_verdict_text = format_release(no_verdict)
    accepted_text = format_release(accepted_zero_score)

    assert no_verdict_text != accepted_text
    # The "no verdict" card must not claim *arr approved/accepted it.
    assert "одобр" not in no_verdict_text.lower()
    assert "принял" not in no_verdict_text.lower()
    # The accepted card must say something positive that *arr actually did evaluate it.
    assert "arr" in accepted_text.lower()


def test_search_result_list_item_omits_verdict_when_none_exists():
    """The compact multi-release list card must not fabricate a verdict for
    a Prowlarr free-text hit — silence there, not a false "approved" line.
    """
    result = SearchResult(
        guid="g5", title="Some.Movie.2021.2160p", origin="prowlarr",
        rejected=False, custom_format_score=0, calculated_score=10,
    )
    out = Formatters.format_search_result(result, 1)
    assert "одобр" not in out.lower()
    assert "отклон" not in out.lower()


def test_search_result_list_item_shows_rejection_from_arr():
    """The compact list card DOES surface a real *arr rejection — the user
    should be able to tell from the list alone, before opening the detail
    view."""
    result = SearchResult(
        guid="g6", title="Дюна 2021 2160p DUB", origin="arr",
        rejected=True, rejections=["English is wanted, but found Russian"],
        custom_format_score=-1000, calculated_score=10,
    )
    out = Formatters.format_search_result(result, 1)
    assert "English is wanted, but found Russian" in out
