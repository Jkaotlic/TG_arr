"""BUG-08 / LOGIC-07: lookup picks candidate by detected_year of the chosen release."""

from bot.handlers.search import _pick_by_year
from bot.models import MovieInfo


def _movie(year):
    return MovieInfo(title="Dune", tmdb_id=year, year=year)


def test_pick_by_year_prefers_release_year():
    """User clicked a 2021 release of "Dune" — picker must prefer the 2021
    candidate over the popularity-sorted 1984 candidate."""
    candidates = [_movie(1984), _movie(2021), _movie(2000)]
    chosen = _pick_by_year(candidates, release_year=2021, query_year=None)
    assert chosen.year == 2021


def test_pick_by_year_falls_back_to_query_year():
    candidates = [_movie(1984), _movie(2021)]
    chosen = _pick_by_year(candidates, release_year=None, query_year=2021)
    assert chosen.year == 2021


def test_pick_by_year_off_by_one_tolerated():
    """A release tagged 2020 still matches a 2021 candidate (release-year drift)."""
    candidates = [_movie(1984), _movie(2021)]
    chosen = _pick_by_year(candidates, release_year=2020, query_year=None)
    assert chosen.year == 2021


def test_pick_by_year_no_year_returns_first():
    candidates = [_movie(1984), _movie(2021)]
    chosen = _pick_by_year(candidates, release_year=None, query_year=None)
    assert chosen.year == 1984


def test_pick_by_year_empty_returns_none():
    assert _pick_by_year([], 2021, 2021) is None
