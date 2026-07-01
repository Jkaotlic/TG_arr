"""Feature #5: external-metadata link buttons (TMDB/IMDb/TVDB) on content cards."""

from bot.models import MovieInfo, SeriesInfo
from bot.ui.keyboards import Keyboards
from bot.models import ContentType, SearchResult


def _urls(markup) -> list[str]:
    return [b.url for row in markup.inline_keyboard for b in row if getattr(b, "url", None)]


def test_movie_details_has_tmdb_and_imdb_links():
    movie = MovieInfo(tmdb_id=27205, imdb_id="tt1375666", title="Inception", year=2010)
    urls = _urls(Keyboards.movie_details(movie))
    assert any("themoviedb.org/movie/27205" in u for u in urls)
    assert any("imdb.com/title/tt1375666" in u for u in urls)


def test_movie_details_without_imdb_still_has_tmdb():
    movie = MovieInfo(tmdb_id=27205, title="Inception", year=2010)
    urls = _urls(Keyboards.movie_details(movie))
    assert any("themoviedb.org/movie/27205" in u for u in urls)
    assert not any("imdb.com" in u for u in urls)


def test_series_details_has_tmdb_and_tvdb_links():
    series = SeriesInfo(tvdb_id=81189, tmdb_id=1396, title="Breaking Bad", year=2008)
    urls = _urls(Keyboards.series_details(series))
    assert any("themoviedb.org/tv/1396" in u for u in urls)  # tv, not movie
    assert any("thetvdb.com" in u and "81189" in u for u in urls)


def test_release_details_includes_links_only_when_content_given():
    result = SearchResult(guid="g", title="t")
    movie = MovieInfo(tmdb_id=27205, imdb_id="tt1375666", title="Inception", year=2010)

    without = _urls(Keyboards.release_details(result, ContentType.MOVIE))
    assert without == []

    with_content = _urls(Keyboards.release_details(result, ContentType.MOVIE, content=movie))
    assert any("themoviedb.org/movie/27205" in u for u in with_content)
