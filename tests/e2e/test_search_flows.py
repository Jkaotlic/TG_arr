"""Сквозные сценарии задачи 4: бот не должен молча добавлять не тот тайтл.

Находки LOGIC-01..04, LOGIC-13, BUG-01, BUG-02, BUG-12, BUG-18 аудита 2026-10-30.
Строки старого формата в тестах написаны буквально: такие кнопки уже лежат в чатах
пользователей, и `pack()` их не воспроизведёт.
"""

import asyncio
import time

import pytest

from bot.handlers import music, trending
from bot.models import SeriesInfo
from tests.e2e.fakes import BotEnv, FakeBackend, find_button, movie, release, stock_radarr_movie, texts

ADMIN = BotEnv.ADMIN


def show(title: str, tvdb_id: int, *, year: int = 2020, genres=(), seasons=(1,), **overrides) -> dict:
    """Сериал, как его отдаёт Sonarr."""
    return {
        "title": title,
        "tvdbId": tvdb_id,
        "year": year,
        "titleSlug": f"show-{tvdb_id}",
        "genres": list(genres),
        "seasons": [{"seasonNumber": number, "monitored": False} for number in seasons],
        "qualityProfileId": 1,
        "monitored": False,
        "images": [],
        **overrides,
    }


def by_term(backend: FakeBackend, path: str, answers: dict[str, list]) -> None:
    """Ответ lookup зависит от запроса; незнакомый запрос даёт пустой список."""
    backend.route("GET", path, responder=lambda request: answers.get(request.query["term"], []))


def stock_sonarr(env: BotEnv, answers: dict[str, list], releases=None) -> None:
    """Sonarr на поток «/series → список → карточка»: id сериала равен его tvdbId."""
    sonarr = env.sonarr
    sonarr.route("GET", "/api/v3/series", [])
    sonarr.route("POST", "/api/v3/series", responder=lambda request: {**request.json, "id": request.json["tvdbId"]})
    sonarr.route("GET", "/api/v3/series/{id}", show("Any", 1))
    sonarr.route("PUT", "/api/v3/series/{id}", responder=lambda request: request.json)
    sonarr.route(
        "GET", "/api/v3/release",
        releases if releases is not None else [release(seasonNumber=1, episodeNumbers=[1])],
    )
    # После `{id}`: последний заданный маршрут важнее, а `{id}` поймал бы и «lookup».
    by_term(sonarr, "/api/v3/series/lookup", answers)


def stock_radarr_candidates(env: BotEnv, answers: dict[str, list], movie_id: int = 7) -> None:
    """Radarr с заданной выдачей lookup и потоком добавления и списка релизов."""
    stock_radarr_movie(env.radarr, movie_id=movie_id)
    by_term(env.radarr, "/api/v3/movie/lookup", answers)
    env.radarr.route("POST", "/api/v3/movie", responder=lambda request: {**request.json, "id": movie_id})


def stale_answer(calls) -> bool:
    return any(call.name == "AnswerCallbackQuery" and call.text and "устарел" in call.text for call in calls)


def added_movie_ids(env: BotEnv) -> list[int]:
    return [request.json["tmdbId"] for request in env.radarr.calls("POST", "/api/v3/movie")]


# ------------------------------------------------------------------- LOGIC-01


async def test_single_exact_title_is_the_one_that_gets_added(bot_env):
    """Вопроса нет, потому что совпадение одно. Добавиться должно именно оно."""
    stock_radarr_candidates(bot_env, {"Дюна": [
        movie(title="Дюна: Часть вторая", year=2024, tmdbId=2),
        movie(title="Дюна", year=2021, tmdbId=1),
    ]})

    await bot_env.say(ADMIN, "/movie Дюна")

    assert added_movie_ids(bot_env) == [1]


async def test_query_year_beats_the_first_candidate(bot_env):
    """«Dune 2021» не должен превратиться в фильм 1984 года."""
    stock_radarr_candidates(bot_env, {"Dune": [
        movie(title="Dune", year=1984, tmdbId=84),
        movie(title="Dune", year=2021, tmdbId=21),
    ]})

    await bot_env.say(ADMIN, "/movie Dune 2021")

    assert added_movie_ids(bot_env) == [21]


async def test_exact_title_of_the_wrong_year_is_asked_about(bot_env):
    """Год запроса не нашёлся ни у кого: точное название 1984 года не повод добавлять."""
    stock_radarr_candidates(bot_env, {"Dune": [
        movie(title="Dune", year=1984, tmdbId=84),
        movie(title="Dune: Part Two", year=2024, tmdbId=24),
    ]})

    calls = await bot_env.say(ADMIN, "/movie Dune 2021")

    assert added_movie_ids(bot_env) == []
    assert find_button(calls, "Dune (1984)")


# -------------------------------------------------------------- BUG-01, BUG-02


async def test_stale_title_button_does_not_add_from_the_newer_list(bot_env):
    """Выбор A открыт, поиск B, нажатие под A: в *arr не уходит ничего."""
    stock_radarr_candidates(bot_env, {
        "Alpha": [movie(title="Alpha One", tmdbId=11), movie(title="Alpha Two", tmdbId=12)],
        "Beta": [movie(title="Beta One", tmdbId=21), movie(title="Beta Two", tmdbId=22)],
    })
    question_a = await bot_env.say(ADMIN, "/movie Alpha")
    button_a = find_button(question_a, "Alpha Two")
    question_b = await bot_env.say(ADMIN, "/movie Beta")

    calls = await bot_env.tap(ADMIN, button_a.call, button_a)

    assert stale_answer(calls), texts(calls)
    assert added_movie_ids(bot_env) == []

    # Выбор B при этом жив.
    await bot_env.press(ADMIN, question_b, "Beta Two")
    assert added_movie_ids(bot_env) == [22]


def ambiguous_query(env: BotEnv, *queries: str) -> None:
    """Фильм и сериал с одним названием: автоопределение обязано спросить."""
    movies = {query: [movie(title=query, tmdbId=100 + i)] for i, query in enumerate(queries)}
    shows = {query: [show(query, 200 + i)] for i, query in enumerate(queries)}
    stock_radarr_candidates(env, movies)
    stock_sonarr(env, shows)


async def test_stale_type_button_does_not_use_the_newer_query(bot_env):
    """Вопрос по A открыт, вопрос по B, «Фильм» под A: запрос B не уходит как фильм."""
    ambiguous_query(bot_env, "Alpha", "Beta")
    question_a = await bot_env.say(ADMIN, "Alpha")
    button_a = find_button(question_a, "Фильм")
    question_b = await bot_env.say(ADMIN, "Beta")
    lookups_before = len(bot_env.radarr.calls("GET", "/api/v3/movie/lookup"))

    calls = await bot_env.tap(ADMIN, button_a.call, button_a)

    assert stale_answer(calls), texts(calls)
    assert added_movie_ids(bot_env) == []
    assert len(bot_env.radarr.calls("GET", "/api/v3/movie/lookup")) == lookups_before

    await bot_env.press(ADMIN, question_b, "Фильм")
    assert added_movie_ids(bot_env) == [101]


# --------------------------------------------------------------------- BUG-12


async def test_type_button_double_tap_runs_one_search(bot_env):
    ambiguous_query(bot_env, "Alpha")
    button = find_button(await bot_env.say(ADMIN, "Alpha"), "Фильм")
    lookups_before = len(bot_env.radarr.calls("GET", "/api/v3/movie/lookup"))

    first, second = await asyncio.gather(
        bot_env.tap(ADMIN, button.call, button),
        bot_env.tap(ADMIN, button.call, button),
    )

    assert len(bot_env.radarr.calls("GET", "/api/v3/movie/lookup")) == lookups_before + 1
    assert added_movie_ids(bot_env) == [100]
    assert any("Уже обрабатываю" in text for text in texts([*first, *second]))


async def test_title_button_double_tap_adds_once(bot_env):
    stock_radarr_candidates(bot_env, {
        "Alpha": [movie(title="Alpha One", tmdbId=11), movie(title="Alpha Two", tmdbId=12)],
    })
    button = find_button(await bot_env.say(ADMIN, "/movie Alpha"), "Alpha Two")

    first, second = await asyncio.gather(
        bot_env.tap(ADMIN, button.call, button),
        bot_env.tap(ADMIN, button.call, button),
    )

    assert added_movie_ids(bot_env) == [12]
    assert any("Уже обрабатываю" in text for text in texts([*first, *second]))


def remember_trending_series(series: SeriesInfo) -> None:
    trending._cache_put(trending._trending_series_cache, series.tmdb_id, series)


async def test_trending_add_double_tap_posts_once(bot_env):
    stock_sonarr(bot_env, {"Hit": [show("Hit", 500, year=2024)]})
    remember_trending_series(SeriesInfo(tmdb_id=55, title="Hit", year=2024))

    await asyncio.gather(
        bot_env.tap(ADMIN, None, "addc:series:55"),
        bot_env.tap(ADMIN, None, "addc:series:55"),
    )

    assert len(bot_env.sonarr.calls("POST", "/api/v3/series")) == 1


# --------------------------------------------------------------------- BUG-18


async def _open_series_card(env: BotEnv, query: str):
    listing = await env.say(ADMIN, f"/series {query}")
    return await env.press(ADMIN, listing, "2160p")


async def test_stale_season_preset_does_not_change_the_newer_card(bot_env):
    """Пикер пресетов A остался открытым, в сессии уже B: мониторинг B не меняется."""
    stock_sonarr(bot_env, {"Alpha": [show("Alpha", 301)], "Beta": [show("Beta", 302)]})
    picker = await bot_env.press(ADMIN, await _open_series_card(bot_env, "Alpha"), "Мониторинг сезонов")
    preset = find_button(picker, "Только будущие")
    await _open_series_card(bot_env, "Beta")

    calls = await bot_env.tap(ADMIN, preset.call, preset)

    assert stale_answer(calls), texts(calls)
    assert (await bot_env.db.get_session(ADMIN)).monitor_type is None


async def test_stale_season_menu_and_back_do_not_open_the_newer_card(bot_env):
    stock_sonarr(bot_env, {"Alpha": [show("Alpha", 301)], "Beta": [show("Beta", 302)]})
    card = await _open_series_card(bot_env, "Alpha")
    menu = find_button(card, "Мониторинг сезонов")
    picker = await bot_env.press(ADMIN, card, "Мониторинг сезонов")
    picker_back = find_button(picker, "Назад")
    await _open_series_card(bot_env, "Beta")

    for stale in (menu, picker_back):
        calls = await bot_env.tap(ADMIN, stale.call, stale)
        assert stale_answer(calls), (stale.button.text, texts(calls))
        assert [call.name for call in calls] == ["AnswerCallbackQuery"]


async def test_season_menu_flow_still_works_with_tokens(bot_env):
    stock_sonarr(bot_env, {"Alpha": [show("Alpha", 301)]})
    picker = await bot_env.press(ADMIN, await _open_series_card(bot_env, "Alpha"), "Мониторинг сезонов")

    after_preset = await bot_env.press(ADMIN, picker, "Только будущие")

    assert (await bot_env.db.get_session(ADMIN)).monitor_type == "future"
    reopened = await bot_env.press(ADMIN, after_preset, "Мониторинг сезонов")
    card_again = await bot_env.press(ADMIN, reopened, "Назад")
    assert find_button(card_again, "Скачать")


async def test_stale_pagination_and_back_do_not_redraw_the_newer_list(make_bot_env):
    env = await make_bot_env(env={"RESULTS_PER_PAGE": "1"})
    releases = [release(guid=f"g{i}") for i in range(3)]
    stock_radarr_movie(env.radarr, releases=releases)
    listing = await env.say(ADMIN, "/movie Дюна")
    next_page = find_button(listing, "▶️")
    card = await env.press(ADMIN, listing, "2160p")
    back = find_button(card, "Назад")
    await env.say(ADMIN, "/movie Дюна")

    for stale in (next_page, back):
        calls = await env.tap(ADMIN, stale.call, stale)
        assert stale_answer(calls), (stale.button.text, texts(calls))
        assert [call.name for call in calls] == ["AnswerCallbackQuery"]


async def test_pagination_and_back_still_work_with_tokens(make_bot_env):
    env = await make_bot_env(env={"RESULTS_PER_PAGE": "1"})
    stock_radarr_movie(env.radarr, releases=[release(guid=f"g{i}") for i in range(3)])
    listing = await env.say(ADMIN, "/movie Дюна")

    page_two = await env.press(ADMIN, listing, "▶️")
    assert any("2/3" in button.text for button in page_two[0].buttons)
    card = await env.press(ADMIN, page_two, "2160p")
    back_to_list = await env.press(ADMIN, card, "Назад")
    assert any("2/3" in button.text for button in back_to_list[0].buttons)


async def _music_env(make_bot_env) -> BotEnv:
    env = await make_bot_env(backends=("lidarr",))
    by_term(env.backends["lidarr"], "/api/v1/artist/lookup", {
        "Alpha": [
            {"artistName": "Alpha One", "foreignArtistId": "mb-a1"},
            {"artistName": "Alpha Two", "foreignArtistId": "mb-a2"},
        ],
        "Beta": [
            {"artistName": "Beta One", "foreignArtistId": "mb-b1"},
            {"artistName": "Beta Two", "foreignArtistId": "mb-b2"},
        ],
    })
    return env


async def test_stale_artist_button_does_not_open_a_newer_artist(make_bot_env):
    env = await _music_env(make_bot_env)
    prompt = (await env.say(ADMIN, "🎵 Музыка"))[-1]
    first = await env.say(ADMIN, "Alpha", reply_to=prompt)
    stale = find_button(first, "Alpha Two")
    prompt = (await env.say(ADMIN, "🎵 Музыка"))[-1]
    second = await env.say(ADMIN, "Beta", reply_to=prompt)

    calls = await env.tap(ADMIN, stale.call, stale)

    assert stale_answer(calls), texts(calls)
    assert [call.name for call in calls] == ["AnswerCallbackQuery"]

    # Текущий список работает, и после листания кнопки остаются рабочими.
    fresh = await env.press(ADMIN, second, "Beta Two")
    assert any("Beta Two" in text for text in texts(fresh))


async def test_stale_trending_artist_button_does_not_search_a_newer_artist(make_bot_env):
    env = await make_bot_env(backends=("lidarr",), env={"DEEZER_ENABLED": "true"}, allow_unmatched=True)
    lidarr = env.backends["lidarr"]
    lidarr.route("GET", "/api/v1/artist/lookup", [])

    class Deezer:
        def __init__(self) -> None:
            self.chart = [{"name": "Old Hit"}]

        async def get_trending_artists(self, limit: int = 10) -> list[dict]:
            return self.chart

    deezer = Deezer()

    async def get_deezer():
        return deezer

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(music, "get_deezer", get_deezer)
        menu = await env.tap(ADMIN, None, "trending_music")
        stale = find_button(menu, "Old Hit")
        deezer.chart = [{"name": "New Hit"}]
        await env.tap(ADMIN, None, "trending_music")

        calls = await env.tap(ADMIN, stale.call, stale)

    assert stale_answer(calls), texts(calls)
    assert lidarr.calls("GET", "/api/v1/artist/lookup") == []


# ------------------------------------------------- Review Focus: старый формат


@pytest.mark.parametrize(
    "legacy", ["ti:0", "ti:1", "ti:abc:-1", "type:movie", "type:series", "type:anime", "type:music"],
)
async def test_legacy_question_buttons_answer_stale(bot_env, legacy):
    """Открыт вопрос о тайтле, под рукой кнопка старого формата: сессию она не трогает."""
    stock_radarr_candidates(bot_env, {
        "Alpha": [movie(title="Alpha One", tmdbId=11), movie(title="Alpha Two", tmdbId=12)],
    })
    await bot_env.say(ADMIN, "/movie Alpha")
    before = (len(bot_env.radarr.requests), len(bot_env.sonarr.requests))

    calls = await bot_env.tap(ADMIN, None, legacy)

    assert [call.name for call in calls] == ["AnswerCallbackQuery"], texts(calls)
    assert "устарел" in calls[0].text
    assert (len(bot_env.radarr.requests), len(bot_env.sonarr.requests)) == before
    assert added_movie_ids(bot_env) == []
    assert (await bot_env.db.get_session(ADMIN)).lookup_candidates


@pytest.mark.parametrize(
    "legacy", ["pg:search:1", "pg:search:0", "back", "season_menu", "season_back", "ssn:all", "ssn:future"],
)
async def test_legacy_list_buttons_answer_stale(make_bot_env, legacy):
    """Открыта карточка сериала: кнопки старого формата не правят ни её, ни сессию."""
    env = await make_bot_env(env={"RESULTS_PER_PAGE": "1"})
    stock_sonarr(env, {"Alpha": [show("Alpha", 301)]}, releases=[
        release(guid=f"g{i}", seasonNumber=1, episodeNumbers=[1]) for i in range(3)
    ])
    await _open_series_card(env, "Alpha")
    before = await env.db.get_session(ADMIN)

    calls = await env.tap(ADMIN, None, legacy)

    assert [call.name for call in calls] == ["AnswerCallbackQuery"], texts(calls)
    assert "устарел" in calls[0].text
    after = await env.db.get_session(ADMIN)
    assert after.model_dump() == before.model_dump()


@pytest.mark.parametrize("legacy", ["art:0", "art:1", "tri:artist:0"])
async def test_legacy_artist_buttons_answer_stale(make_bot_env, legacy):
    env = await _music_env(make_bot_env)
    prompt = (await env.say(ADMIN, "🎵 Музыка"))[-1]
    await env.say(ADMIN, "Alpha", reply_to=prompt)
    music._trending_artists_cache[ADMIN] = [{"name": "Alpha"}]
    lookups = len(env.backends["lidarr"].requests)

    calls = await env.tap(ADMIN, None, legacy)

    assert [call.name for call in calls] == ["AnswerCallbackQuery"], texts(calls)
    assert "устарел" in calls[0].text
    assert len(env.backends["lidarr"].requests) == lookups


# ------------------------------------------------------------------- LOGIC-02


async def test_radarr_down_is_reported_honestly(bot_env):
    bot_env.radarr.fail_all(500)

    calls = await bot_env.say(ADMIN, "/movie Дюна")

    joined = "\n".join(texts(calls))
    assert "Radarr" in joined
    assert "Ничего не найдено" not in joined


async def test_hung_radarr_is_reported_honestly_and_in_bounded_time(make_bot_env, monkeypatch):
    env = await make_bot_env(env={"HTTP_TIMEOUT": "0.2"})
    monkeypatch.setattr("bot.clients.base.SLOW_ARR_TIMEOUT", 0.2)
    env.radarr.hang_all()

    started = time.monotonic()
    calls = await env.say(ADMIN, "/movie Дюна")
    elapsed = time.monotonic() - started

    joined = "\n".join(texts(calls))
    assert "Radarr" in joined
    assert "Ничего не найдено" not in joined
    assert elapsed < 5, f"запрос шёл {elapsed:.1f} с"
    env.radarr.recover()


async def test_sonarr_down_is_reported_honestly_for_series_and_anime(bot_env):
    bot_env.sonarr.fail_all(500)

    for command in ("/series Дюна", "/anime Дюна"):
        joined = "\n".join(texts(await bot_env.say(ADMIN, command)))
        assert "Sonarr" in joined
        assert "Ничего не найдено" not in joined


async def test_partial_failure_does_not_pick_a_confident_type_and_is_not_cached(bot_env):
    """Лежит Sonarr, Radarr уверенно нашёл фильм. Это не повод считать запрос фильмом."""
    stock_radarr_candidates(bot_env, {"Alpha": [movie(title="Alpha", tmdbId=11)]})
    stock_sonarr(bot_env, {"Alpha": [show("Alpha", 301)]})
    bot_env.sonarr.fail_all(500)

    calls = await bot_env.say(ADMIN, "Alpha")

    joined = "\n".join(texts(calls))
    assert "Sonarr" in joined
    assert added_movie_ids(bot_env) == []
    assert find_button(calls, "Фильм")

    # Sonarr поднялся. Кэшированного «это фильм» нет: запрос считается заново.
    bot_env.sonarr.recover()
    lookups_before = len(bot_env.sonarr.calls("GET", "/api/v3/series/lookup"))
    await bot_env.say(ADMIN, "Alpha")
    assert len(bot_env.sonarr.calls("GET", "/api/v3/series/lookup")) == lookups_before + 1


async def test_every_video_service_down_gets_an_error_not_a_question(bot_env):
    bot_env.radarr.fail_all(500)
    bot_env.sonarr.fail_all(500)

    calls = await bot_env.say(ADMIN, "Alpha")

    joined = "\n".join(texts(calls))
    assert "Radarr" in joined and "Sonarr" in joined
    assert not any(call.buttons for call in calls), joined


def _library_show(**overrides) -> dict:
    return show("Alpha Show", 301, id=31, **overrides)


async def test_title_with_radarr_down_shows_sonarr_items_and_a_warning(bot_env):
    bot_env.radarr.fail_all(500)
    bot_env.sonarr.route("GET", "/api/v3/series/lookup", [_library_show()])

    calls = await bot_env.say(ADMIN, "/title Alpha")

    final = calls[-1]
    assert "Alpha Show" in final.text
    assert "Radarr" in final.text
    assert any("мониторинг" in button.text.lower() or "новыми" in button.text.lower() for button in final.buttons)


async def test_title_with_radarr_down_and_no_items_does_not_claim_absence(bot_env):
    bot_env.radarr.fail_all(500)
    bot_env.sonarr.route("GET", "/api/v3/series/lookup", [])

    calls = await bot_env.say(ADMIN, "/title Alpha")

    assert "Radarr" in calls[-1].text
    assert "В библиотеке нет" not in calls[-1].text


async def test_title_with_both_services_down_is_an_error(bot_env):
    bot_env.radarr.fail_all(500)
    bot_env.sonarr.fail_all(500)

    calls = await bot_env.say(ADMIN, "/title Alpha")

    assert "Radarr" in calls[-1].text and "Sonarr" in calls[-1].text
    assert "В библиотеке нет" not in calls[-1].text


async def test_trending_add_with_sonarr_down_names_sonarr(bot_env):
    bot_env.sonarr.fail_all(500)
    remember_trending_series(SeriesInfo(tmdb_id=55, title="Hit", year=2024))

    calls = await bot_env.tap(ADMIN, None, "addc:series:55")

    joined = "\n".join(texts(calls))
    assert "Sonarr" in joined
    assert "сопоставить" not in joined


# ------------------------------------------------------------------- LOGIC-03


def _series_posts(env: BotEnv) -> list[dict]:
    return [request.json for request in env.sonarr.calls("POST", "/api/v3/series")]


async def test_trending_with_matching_title_and_near_year_adds_right_away(bot_env):
    stock_sonarr(bot_env, {"Hit Show": [
        show("Other Thing", 401, year=2024),
        show("Hit Show", 402, year=2023),
    ]})
    remember_trending_series(SeriesInfo(tmdb_id=55, title="Hit Show", year=2024))

    await bot_env.tap(ADMIN, None, "addc:series:55")

    assert [post["tvdbId"] for post in _series_posts(bot_env)] == [402]


@pytest.mark.parametrize(
    "candidates",
    [
        pytest.param([show("Hit Show Reborn", 401, year=2024)], id="другое название"),
        pytest.param([show("Hit Show", 401, year=2010)], id="год далеко"),
        pytest.param([show("Hit Show", 401, year=2024), show("Hit Show", 402, year=2024)], id="двое одинаковых"),
    ],
)
async def test_trending_fuzzy_match_offers_a_choice_and_adds_nothing(bot_env, candidates):
    stock_sonarr(bot_env, {"Hit Show": candidates})
    remember_trending_series(SeriesInfo(tmdb_id=55, title="Hit Show", year=2024))

    calls = await bot_env.tap(ADMIN, None, "addc:series:55")

    assert _series_posts(bot_env) == []
    assert any(candidates[0]["title"] in button.text for call in calls for button in call.buttons)


async def test_trending_explicit_choice_adds_the_picked_series(bot_env):
    stock_sonarr(bot_env, {"Hit Show": [
        show("Hit Show Reborn", 401, year=2024),
        show("Hit Show Classic", 402, year=1999),
    ]})
    remember_trending_series(SeriesInfo(tmdb_id=55, title="Hit Show", year=2024))

    choice = await bot_env.tap(ADMIN, None, "addc:series:55")
    await bot_env.press(ADMIN, choice, "Hit Show Classic")

    posts = _series_posts(bot_env)
    assert [post["tvdbId"] for post in posts] == [402]
    assert posts[0]["addOptions"]["monitor"] == "all"


async def test_trending_stale_choice_adds_nothing(bot_env):
    stock_sonarr(bot_env, {
        "Hit Show": [show("Hit Show Reborn", 401, year=2024)],
        "Next Show": [show("Next Show Reborn", 402, year=2024)],
    })
    remember_trending_series(SeriesInfo(tmdb_id=55, title="Hit Show", year=2024))
    remember_trending_series(SeriesInfo(tmdb_id=56, title="Next Show", year=2024))

    first = await bot_env.tap(ADMIN, None, "addc:series:55")
    stale = find_button(first, "Hit Show Reborn")
    await bot_env.tap(ADMIN, None, "addc:series:56")

    calls = await bot_env.tap(ADMIN, stale.call, stale)

    assert stale_answer(calls), texts(calls)
    assert _series_posts(bot_env) == []


async def test_trending_no_candidates_refuses_clearly(bot_env):
    stock_sonarr(bot_env, {})
    remember_trending_series(SeriesInfo(tmdb_id=55, title="Hit Show", year=2024))

    calls = await bot_env.tap(ADMIN, None, "addc:series:55")

    assert _series_posts(bot_env) == []
    assert any("Не удалось сопоставить" in text for text in texts(calls))


# ------------------------------------------------------------------- LOGIC-04


async def test_plain_animation_is_added_as_a_standard_series(bot_env):
    """«Rick and Morty» с жанром Animation не становится аниме."""
    stock_sonarr(bot_env, {"Rick and Morty": [show("Rick and Morty", 301, genres=["Animation", "Comedy"])]})

    await bot_env.say(ADMIN, "/series Rick and Morty")

    assert [post["seriesType"] for post in _series_posts(bot_env)] == ["standard"]


async def test_series_command_keeps_a_title_with_the_anime_genre(bot_env):
    """/series не выбрасывает кандидатов: пользователь сам сказал «сериал»."""
    stock_sonarr(bot_env, {"Frieren": [show("Frieren", 301, genres=["Anime", "Animation"])]})

    await bot_env.say(ADMIN, "/series Frieren")

    assert [post["seriesType"] for post in _series_posts(bot_env)] == ["standard"]


async def test_autodetection_of_a_title_with_both_genres_is_anime(bot_env):
    stock_radarr_candidates(bot_env, {})
    stock_sonarr(bot_env, {"Frieren": [show("Frieren", 301, genres=["Anime", "Animation"])]})

    await bot_env.say(ADMIN, "Frieren")

    assert [post["seriesType"] for post in _series_posts(bot_env)] == ["anime"]


async def test_autodetection_of_plain_animation_is_a_standard_series(bot_env):
    stock_radarr_candidates(bot_env, {})
    stock_sonarr(bot_env, {"Rick and Morty": [show("Rick and Morty", 301, genres=["Animation"])]})

    await bot_env.say(ADMIN, "Rick and Morty")

    assert [post["seriesType"] for post in _series_posts(bot_env)] == ["standard"]


async def test_anime_command_with_only_animation_genre_does_not_lose_the_title(bot_env):
    """/anime: жанра Anime у кандидата нет, но тип назвал сам пользователь."""
    stock_sonarr(bot_env, {"Frieren": [show("Frieren", 301, genres=["Animation"])]})

    await bot_env.say(ADMIN, "/anime Frieren")

    assert [post["seriesType"] for post in _series_posts(bot_env)] == ["anime"]


# ------------------------------------------------------------------- LOGIC-13


async def test_s00e01_searches_season_zero(bot_env):
    stock_sonarr(
        bot_env,
        {"Alpha": [show("Alpha", 301, seasons=(0, 1, 2))]},
        releases=[
            release(guid="special", seasonNumber=0, episodeNumbers=[1]),
            release(guid="other", seasonNumber=0, episodeNumbers=[2]),
        ],
    )

    calls = await bot_env.say(ADMIN, "/series Alpha S00E01")

    requests = bot_env.sonarr.calls("GET", "/api/v3/release")
    assert [request.query["seasonNumber"] for request in requests] == ["0"]
    assert sum("2160p" in button.text for call in calls for button in call.buttons) == 1
    assert bot_env.sonarr.calls("POST", "/api/v3/command") == []


async def test_s00_grab_does_not_touch_other_seasons_flow(bot_env):
    """Без номера серии: сезон 0 целиком, а не вопрос «какой сезон» и не сезон 1."""
    stock_sonarr(
        bot_env,
        {"Alpha": [show("Alpha", 301, seasons=(0, 1, 2))]},
        releases=[release(guid="special", seasonNumber=0, episodeNumbers=[1])],
    )

    calls = await bot_env.say(ADMIN, "/series Alpha S00")

    requests = bot_env.sonarr.calls("GET", "/api/v3/release")
    assert [request.query["seasonNumber"] for request in requests] == ["0"]
    assert not any("Весь сериал" in button.text for call in calls for button in call.buttons)


async def test_whole_series_button_still_means_whole_series(bot_env):
    """Парный контроль: ноль из выбора сезона по-прежнему значит «весь сериал»."""
    stock_sonarr(bot_env, {"Alpha": [show("Alpha", 301, seasons=(1, 2))]})
    bot_env.sonarr.route("POST", "/api/v3/command", {"id": 1})
    bot_env.sonarr.route("GET", "/api/v3/seasonpass", [])
    bot_env.sonarr.route("POST", "/api/v3/seasonpass", {})

    await bot_env.press(ADMIN, await bot_env.say(ADMIN, "/series Alpha"), "Весь сериал")

    assert bot_env.sonarr.calls("GET", "/api/v3/release") == []
    assert [request.json["name"] for request in bot_env.sonarr.calls("POST", "/api/v3/command")] == ["SeriesSearch"]
