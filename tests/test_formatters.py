"""Tests for UI formatters (BUG-11 timezone, BUG-12 truncation)."""

import pytest

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
