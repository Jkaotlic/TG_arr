"""Чистая логика задачи 4 аудита 2026-10-30: выбор кандидата, разбор запроса,
жанры, сбои lookup и граница try вокруг граба."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.models import ActionLog, ActionType, ContentType, MovieInfo, SearchResult, SearchSession, SeriesInfo
from bot.services.search_service import SearchService, describe_search_failure


def _m(title: str, year: int = 2000, tmdb_id: int = 1) -> MovieInfo:
    return MovieInfo(title=title, year=year, tmdb_id=tmdb_id)


# ------------------------------------------------------------------- LOGIC-01


def _resolve(candidates, query, year=None):
    from bot.handlers.search.commands import resolve_title_candidate

    return resolve_title_candidate(candidates, query, year)


def test_no_candidates_nothing_to_choose_and_nothing_to_ask():
    resolution = _resolve([], "Дюна")
    assert resolution.chosen is None
    assert resolution.must_ask is False


def test_single_candidate_is_chosen_without_a_question():
    only = _m("Что-то другое", 2021)
    resolution = _resolve([only], "Дюна")
    assert resolution.chosen is only
    assert resolution.must_ask is False


def test_exact_title_is_chosen_even_when_it_is_not_first():
    second = _m("Дюна: Часть вторая", 2024)
    exact = _m("Дюна", 2021)
    resolution = _resolve([second, exact], "Дюна")
    assert resolution.chosen is exact
    assert resolution.must_ask is False


def test_exact_title_ignores_case_and_punctuation():
    exact = _m("Дюна:", 2021)
    resolution = _resolve([_m("Дюна 2"), exact], "дюна")
    assert resolution.chosen is exact


@pytest.mark.parametrize("titles", [("Дюна", "Дюна"), ("Дюна 1", "Дюна 2")])
def test_no_single_exact_title_means_a_question(titles):
    resolution = _resolve([_m(titles[0]), _m(titles[1])], "Дюна")
    assert resolution.must_ask is True
    assert resolution.chosen is None


def test_query_year_picks_the_only_candidate_of_that_year():
    old = _m("Dune", 1984)
    new = _m("Dune", 2021)
    resolution = _resolve([old, new], "Dune 2021", 2021)
    assert resolution.chosen is new
    assert resolution.must_ask is False


def test_query_year_tolerates_one_year_of_drift():
    new = _m("Dune", 2020)
    assert _resolve([_m("Dune", 1984), new], "Dune", 2021).chosen is new


def test_query_year_wins_over_a_differently_titled_exact_match():
    """Один кандидат нужного года: он и выбран, даже без точного названия."""
    right_year = _m("Dune: Part One", 2021)
    resolution = _resolve([_m("Dune", 1984), right_year], "Dune", 2021)
    assert resolution.chosen is right_year


def test_several_in_the_same_year_with_one_exact_title_picks_it():
    exact = _m("Dune", 2021)
    resolution = _resolve([_m("Dune: Making Of", 2021), exact, _m("Dune", 1984)], "Dune 2021", 2021)
    assert resolution.chosen is exact
    assert resolution.must_ask is False


def test_several_in_the_same_year_without_one_exact_title_asks():
    resolution = _resolve([_m("Dune A", 2021), _m("Dune B", 2021)], "Dune", 2021)
    assert resolution.must_ask is True


def test_exact_title_of_another_year_is_not_chosen():
    resolution = _resolve([_m("Dune", 1984), _m("Dune: Part Two", 2024)], "Dune 2021", 2021)
    assert resolution.must_ask is True
    assert resolution.chosen is None


def test_single_candidate_of_another_year_is_asked_about():
    """«Dune 2021» не превращается в фильм 1984 года и когда кандидат один."""
    resolution = _resolve([_m("Dune", 1984)], "Dune 2021", 2021)
    assert resolution.must_ask is True


def test_single_candidate_without_a_year_is_taken():
    only = SeriesInfo(title="Dune", tvdb_id=1)
    assert _resolve([only], "Dune 2021", 2021).chosen is only


def test_the_old_pair_is_gone():
    from bot.handlers.search import commands

    assert not hasattr(commands, "needs_title_confirmation")
    assert not hasattr(commands, "_pick_metadata_candidate")


# ------------------------------------------------------------------- LOGIC-12


@pytest.mark.parametrize("query", ["Cars2", "Cars 2", "Class2020", "Mars3", "Ford S2000"])
def test_digits_glued_to_a_word_are_not_a_season(query):
    parsed = SearchService(MagicMock(), MagicMock()).parse_query(query)
    assert parsed["season"] is None
    assert parsed["title"] == query


@pytest.mark.parametrize(
    ("query", "season", "episode", "title"),
    [
        ("Show S2", 2, None, "Show"),
        ("Show s02e03", 2, 3, "Show"),
        ("Show.S01E05.1080p", 1, 5, "Show"),
        ("Show_S01E05", 1, 5, "Show"),
        ("S01 Show", 1, None, "Show"),
        ("Show S00E01", 0, 1, "Show"),
        ("Show S01E05E06", 1, 5, "Show"),
        ("Show S01E05-E06", 1, 5, "Show"),
    ],
)
def test_real_season_markers_still_parse(query, season, episode, title):
    parsed = SearchService(MagicMock(), MagicMock()).parse_query(query)
    assert (parsed["season"], parsed["episode"], parsed["title"]) == (season, episode, title)


# ------------------------------------------------------------------- LOGIC-04


def _s(title: str, *genres: str) -> SeriesInfo:
    return SeriesInfo(title=title, tvdb_id=1, genres=list(genres))


@pytest.mark.parametrize(
    ("genres", "is_anime"),
    [
        (["Animation", "Comedy"], False),
        (["Anime"], True),
        (["anime"], True),
        (["Anime", "Animation"], True),
        (["Drama"], False),
        ([], False),
    ],
)
def test_only_the_anime_genre_decides_anime(genres, is_anime):
    item = _s("X", *genres)
    series, anime = SearchService.split_series_candidates([item])
    assert (anime == [item]) is is_anime
    assert (series == [item]) is not is_anime


async def test_series_candidates_keep_every_title():
    from bot.handlers.search.commands import _lookup_metadata_candidates

    plain, cartoon = _s("Plain"), _s("Cartoon", "Anime")
    service = MagicMock()
    service.lookup_series = AsyncMock(return_value=[plain, cartoon])

    assert await _lookup_metadata_candidates(service, "x", ContentType.SERIES) == [plain, cartoon]


async def test_anime_candidates_prefer_the_genre_and_fall_back_to_all():
    from bot.handlers.search.commands import _lookup_metadata_candidates

    plain, anime = _s("Plain"), _s("Frieren", "Anime")
    service = MagicMock()
    service.lookup_series = AsyncMock(return_value=[plain, anime])
    assert await _lookup_metadata_candidates(service, "x", ContentType.ANIME) == [anime]

    service.lookup_series = AsyncMock(return_value=[plain])
    assert await _lookup_metadata_candidates(service, "x", ContentType.ANIME) == [plain]


# ------------------------------------------------------------------- LOGIC-02


def _failing_service(error: Exception) -> SearchService:
    radarr, sonarr = AsyncMock(), AsyncMock()
    radarr.lookup_movie.side_effect = error
    sonarr.lookup_series.side_effect = error
    return SearchService(radarr, sonarr)


@pytest.mark.parametrize(
    ("method", "name"), [("lookup_movies", "Radarr"), ("lookup_series", "Sonarr")],
)
async def test_lookup_raises_and_names_the_service(method, name):
    from bot.services.search_service import CatalogLookupError

    with pytest.raises(CatalogLookupError) as caught:
        await getattr(_failing_service(RuntimeError("boom")), method)("Dune")

    assert name in describe_search_failure(caught.value)


async def test_lookup_failure_for_a_bad_key_says_so():
    from bot.clients.base import AuthenticationError
    from bot.services.search_service import CatalogLookupError

    with pytest.raises(CatalogLookupError) as caught:
        await _failing_service(AuthenticationError("401", status_code=401)).lookup_movies("Dune")

    text = describe_search_failure(caught.value)
    assert "Radarr" in text and "API" in text


async def test_empty_answer_is_still_an_empty_list():
    radarr = AsyncMock()
    radarr.lookup_movie.return_value = []
    assert await SearchService(radarr, AsyncMock()).lookup_movies("Dune") == []


def _detect_service(*, movies=(), series=(), radarr_error=None, sonarr_error=None, lidarr_error=None, artists=()):
    radarr, sonarr, lidarr = AsyncMock(), AsyncMock(), AsyncMock()
    radarr.lookup_movie = AsyncMock(return_value=list(movies), side_effect=radarr_error)
    sonarr.lookup_series = AsyncMock(return_value=list(series), side_effect=sonarr_error)
    lidarr.lookup_artist = AsyncMock(return_value=list(artists), side_effect=lidarr_error)
    return SearchService(radarr, sonarr, lidarr=lidarr)


@pytest.mark.parametrize(
    "broken",
    [
        {"sonarr_error": ConnectionError("down")},
        {"lidarr_error": ConnectionError("down")},
    ],
)
async def test_detection_with_a_failed_branch_asks_and_does_not_cache(broken):
    service = _detect_service(movies=[_m("Dune", 2021)], **broken)

    result = await service.detect_content_type("Dune")

    assert result.content_type is ContentType.UNKNOWN
    assert result.confidence < 0.7
    assert result.failed_services
    assert result.candidates["movie"] == ["Dune"]

    from bot.services.search_service import _cache_get, _normalize_query

    assert _cache_get(_normalize_query("Dune")) is None


async def test_detection_with_a_movie_branch_failed_does_not_call_it_a_series():
    service = _detect_service(series=[_s("Dune")], radarr_error=ConnectionError("down"))

    result = await service.detect_content_type("Dune")

    assert result.content_type is ContentType.UNKNOWN
    assert result.failed_services == ("radarr",)


async def test_detection_all_clear_is_still_cached_and_confident():
    service = _detect_service(movies=[_m("Dune", 2021)])

    result = await service.detect_content_type("Dune")

    assert result.content_type is ContentType.MOVIE
    assert result.failed_services == ()

    from bot.services.search_service import _cache_get, _normalize_query

    assert _cache_get(_normalize_query("Dune")) is not None


async def test_episodic_query_is_not_blocked_by_radarr_but_is_not_cached():
    service = _detect_service(series=[_s("Dune")], radarr_error=ConnectionError("down"))

    result = await service.detect_content_type("Dune S01")

    assert result.content_type is ContentType.SERIES

    from bot.services.search_service import _cache_get, _normalize_query

    assert _cache_get(_normalize_query("Dune S01")) is None


async def test_episodic_query_with_sonarr_down_goes_to_the_explicit_lookup():
    """Тип ясен из «S01», а сбой честно покажет следующий шаг: lookup_series бросит."""
    service = _detect_service(sonarr_error=ConnectionError("down"))

    result = await service.detect_content_type("Dune S01")

    assert result.content_type is ContentType.SERIES
    assert result.lookup_results == []


async def test_hung_branch_keeps_the_answer_of_the_other_one(monkeypatch):
    import asyncio

    from bot.services import search_service as module

    async def never(*_args):
        await asyncio.sleep(60)

    monkeypatch.setattr(module, "_DETECT_TIMEOUT_S", 0.05)
    radarr, sonarr = AsyncMock(), AsyncMock()
    radarr.lookup_movie.return_value = [_m("Dune", 2021)]
    sonarr.lookup_series = never
    service = SearchService(radarr, sonarr)

    result = await service.detect_content_type("Dune")

    assert result.failed_services == ("sonarr",)
    assert result.candidates["movie"] == ["Dune"]


# ------------------------------------------------------------------- LOGIC-16


def _grab_session() -> SearchSession:
    release = SearchResult(guid="g", title="Dune.2021", indexer="T", indexer_id=1)
    return SearchSession(
        user_id=5,
        query="Dune",
        content_type=ContentType.MOVIE,
        results=[release],
        selected_result=release,
        selected_content=MovieInfo(title="Dune", year=2021, tmdb_id=1, radarr_id=7),
        list_token="abc123",
    )


def _grab_mocks(*, grab_outcome=None, grab_error=None):
    add_service = MagicMock()
    action = ActionLog(user_id=0, action_type=ActionType.GRAB, content_type=ContentType.MOVIE)
    add_service.grab_release = AsyncMock(
        return_value=(grab_outcome if grab_outcome is not None else True, action), side_effect=grab_error,
    )
    add_service.radarr.set_movie_monitored = AsyncMock()
    message = MagicMock()
    message.edit_text = AsyncMock()
    message.answer = AsyncMock()
    message.chat.id = 5
    message.message_id = 9
    db = MagicMock()
    db.log_action = AsyncMock()
    db.delete_session = AsyncMock()
    user = MagicMock()
    user.preferences.notify_library = False
    return add_service, message, db, user


async def _run_grab(add_service, message, db, user):
    from bot.handlers.search.grab import _execute_grab

    await _execute_grab(message, _grab_session(), user, db, MagicMock(), add_service)


def _edited(message) -> str:
    return "\n".join(str(call.args[0]) for call in message.edit_text.await_args_list)


async def test_failed_confirmation_edit_after_a_grab_is_not_reported_as_failure():
    add_service, message, db, user = _grab_mocks()
    message.edit_text.side_effect = RuntimeError("telegram is having a moment")

    await _run_grab(add_service, message, db, user)

    add_service.grab_release.assert_awaited_once()
    assert message.edit_text.await_count == 1, "повторная правка с текстом об ошибке"
    assert "временно недоступна" not in _edited(message)
    db.delete_session.assert_not_awaited()
    assert "Dune" in message.answer.await_args.args[0]


async def test_failed_log_action_after_a_grab_is_not_reported_as_failure():
    add_service, message, db, user = _grab_mocks()
    db.log_action.side_effect = RuntimeError("db locked")

    await _run_grab(add_service, message, db, user)

    assert "временно недоступна" not in _edited(message)
    assert "Dune" in _edited(message)
    db.delete_session.assert_awaited_once()


async def test_failed_living_message_after_a_grab_is_not_reported_as_failure():
    add_service, message, db, user = _grab_mocks()
    user.preferences.notify_library = True
    message.edit_text.side_effect = RuntimeError("telegram is having a moment")

    await _run_grab(add_service, message, db, user)

    assert message.edit_text.await_count == 1
    db.delete_session.assert_not_awaited()


async def test_a_grab_that_raises_still_reports_failure_and_drops_the_session():
    add_service, message, db, user = _grab_mocks(grab_error=RuntimeError("arr exploded"))

    await _run_grab(add_service, message, db, user)

    assert "временно недоступна" in _edited(message)
    db.delete_session.assert_awaited_once()


async def test_a_refused_grab_still_reports_failure_and_drops_the_session():
    add_service, message, db, user = _grab_mocks(grab_outcome=False)
    action = add_service.grab_release.return_value[1]
    action.error_message = "Radarr сказал нет"

    await _run_grab(add_service, message, db, user)

    assert "Radarr сказал нет" in _edited(message)
    db.delete_session.assert_awaited_once()


# ------------------------------------------------------------- Правки ревью


async def test_slow_detection_does_not_open_the_breaker(monkeypatch):
    """Короткий срок автоопределения ничего не говорит о здоровье сервиса."""
    import asyncio

    from bot.services import search_service as module

    async def never(*_args):
        await asyncio.sleep(60)

    monkeypatch.setattr(module, "_DETECT_TIMEOUT_S", 0.01)
    radarr, sonarr = AsyncMock(), AsyncMock()
    radarr.lookup_movie.return_value = []
    sonarr.lookup_series = never
    service = SearchService(radarr, sonarr)

    for attempt in range(4):
        await service.detect_content_type(f"query {attempt}")

    assert not module._CIRCUIT_BREAKER.is_open("sonarr")


@pytest.mark.parametrize(
    ("has_lidarr", "expected", "absent"),
    [(False, "Soulseek", "Lidarr"), (True, "Lidarr", "Soulseek")],
)
async def test_music_branch_is_named_after_what_is_configured(has_lidarr, expected, absent):
    from bot.services.search_service import describe_unreachable

    lidarr = AsyncMock() if has_lidarr else None
    if lidarr is not None:
        lidarr.lookup_artist.side_effect = ConnectionError("down")
    slskd = AsyncMock()
    slskd.lookup_artists.side_effect = ConnectionError("down")
    radarr, sonarr = AsyncMock(), AsyncMock()
    radarr.lookup_movie.return_value = [_m("Queen", 2020)]
    sonarr.lookup_series.return_value = []
    service = SearchService(radarr, sonarr, lidarr=lidarr, slskd=slskd)

    result = await service.detect_content_type("Queen")

    text = describe_unreachable(result.failed_services)
    assert expected in text and absent not in text


async def test_public_artist_lookup_reports_failure():
    lidarr = AsyncMock()
    lidarr.lookup_artist.side_effect = ConnectionError("down")
    service = SearchService(AsyncMock(), AsyncMock(), lidarr=lidarr)

    with pytest.raises(ConnectionError):
        await service.lookup_artist("Queen")


async def test_public_artist_lookup_still_falls_back_to_slskd():
    lidarr, slskd = AsyncMock(), AsyncMock()
    lidarr.lookup_artist.side_effect = ConnectionError("down")
    slskd.lookup_artists.return_value = ["found"]
    service = SearchService(AsyncMock(), AsyncMock(), lidarr=lidarr, slskd=slskd)

    assert await service.lookup_artist("Queen") == ["found"]


@pytest.mark.parametrize(
    "name", ["content_type_selection", "season_presets", "title_candidates", "artist_list", "trending_artists"],
)
def test_keyboards_with_indexed_buttons_require_a_token(name):
    import inspect

    from bot.ui.keyboards import Keyboards

    parameter = inspect.signature(getattr(Keyboards, name)).parameters["list_token"]
    assert parameter.default is inspect.Parameter.empty


def test_title_normalizer_is_public_and_shared():
    from bot.handlers import trending
    from bot.handlers.search import commands
    from bot.services.search_service import normalize_title

    assert normalize_title("Дюна:  Часть - вторая") == "дюна часть вторая"
    assert not hasattr(commands, "_normalize_title")
    assert not hasattr(trending, "_normalize_title")


# --- TMDb: external_ids


def _tmdb_client(**kwargs):
    from bot.clients.tmdb import TMDbClient

    return TMDbClient(api_key="v3-secret-key", **kwargs)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"tvdb_id": 121361}, 121361),
        ({"tvdb_id": None}, None),
        ({"tvdb_id": 0}, None),
        ({"tvdb_id": "x"}, None),
        ({"tvdb_id": True}, None),
        ({}, None),
        ([], None),
    ],
)
async def test_external_ids_returns_only_a_real_id(payload, expected):
    client = _tmdb_client()
    client._safe_request = AsyncMock(return_value=payload)

    assert await client.get_series_tvdb_id(55) == expected
    assert client._safe_request.await_args.args[:2] == ("GET", "/tv/55/external_ids")


def test_tmdb_parser_keeps_the_original_name():
    series = _tmdb_client()._parse_series({"id": 1, "name": "Игра престолов", "original_name": "Game of Thrones"})

    assert series.title == "Игра престолов"
    assert series.original_title == "Game of Thrones"


async def test_tmdb_client_goes_through_the_configured_proxy():
    client = _tmdb_client(proxy_url="http://198.51.100.7:8899")

    http = await client._get_client()
    try:
        assert http._mounts, "прокси не подключён"
    finally:
        await client.close()


async def test_external_ids_failure_does_not_put_the_key_in_logs(monkeypatch):
    from structlog.testing import capture_logs

    from bot.handlers import trending

    tmdb = _tmdb_client()
    # Так выглядело бы исключение httpx с полным URL запроса.
    tmdb._safe_request = AsyncMock(
        side_effect=RuntimeError("GET https://api.themoviedb.org/3/tv/55/external_ids?api_key=v3-secret-key"),
    )
    monkeypatch.setattr(trending, "get_tmdb", AsyncMock(return_value=tmdb))
    search_service = MagicMock()
    search_service.lookup_series = AsyncMock(return_value=[])

    with capture_logs() as logs:
        await trending._resolve_series_for_add(search_service, SeriesInfo(title="X", tvdb_id=0, tmdb_id=55))

    assert logs, "сбой должен быть виден в логах"
    assert "v3-secret-key" not in repr(logs)
