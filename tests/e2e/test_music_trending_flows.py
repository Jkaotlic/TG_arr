"""Сквозные сценарии задачи 5: тип сериала из трендов и сессия после сбоя Lidarr.

Аудит 2026-10-30: сериал из трендов (B) и отказ Lidarr при поиске артиста (C),
плюс /wanted с очередью длиннее одной страницы (BUG-11).
"""

import pytest

from bot.models import SeriesInfo
from tests.e2e.fakes import BotEnv, stock_radarr_movie, texts
from tests.e2e.test_search_flows import _series_posts, remember_trending_series, show, stock_sonarr

ADMIN = BotEnv.ADMIN


# ------------------------------------------------- тип сериала из трендов


@pytest.mark.parametrize(
    ("genres", "series_type"),
    [
        pytest.param(["Anime", "Animation"], "anime", id="жанр Anime решает"),
        pytest.param(["anime"], "anime", id="регистр жанра не важен"),
        pytest.param(["Animation", "Comedy"], "standard", id="одна анимация это обычный сериал"),
        pytest.param([], "standard", id="жанров нет"),
    ],
)
async def test_trending_auto_add_sets_the_series_type_from_genres(bot_env, genres, series_type):
    stock_sonarr(bot_env, {"Hit Show": [show("Hit Show", 402, year=2024, genres=genres)]})
    remember_trending_series(SeriesInfo(tmdb_id=55, title="Hit Show", year=2024))

    await bot_env.tap(ADMIN, None, "addc:series:55")

    posts = _series_posts(bot_env)
    assert [post["tvdbId"] for post in posts] == [402]
    assert [post["seriesType"] for post in posts] == [series_type]


@pytest.mark.parametrize(
    ("picked", "series_type"),
    [
        pytest.param("Hit Show Reborn", "anime", id="выбран аниме-тайтл"),
        pytest.param("Hit Show Classic", "standard", id="выбран обычный мультсериал"),
    ],
)
async def test_trending_explicit_choice_sets_the_series_type_of_the_picked_title(bot_env, picked, series_type):
    """Ветка выбора: тип берётся у выбранного кандидата, а не у первого в списке."""
    stock_sonarr(bot_env, {"Hit Show": [
        show("Hit Show Reborn", 401, year=2024, genres=["Anime", "Animation"]),
        show("Hit Show Classic", 402, year=1999, genres=["Animation"]),
    ]})
    remember_trending_series(SeriesInfo(tmdb_id=55, title="Hit Show", year=2024))

    choice = await bot_env.tap(ADMIN, None, "addc:series:55")
    assert _series_posts(bot_env) == []
    await bot_env.press(ADMIN, choice, picked)

    posts = _series_posts(bot_env)
    assert len(posts) == 1
    assert posts[0]["seriesType"] == series_type


# ------------------------------------------------- сбой Lidarr и старая сессия


async def test_lidarr_failure_clears_the_stale_session(make_bot_env):
    """Как и «Артист не найден»: после отказа Lidarr чужие кнопки не оживают."""
    env = await make_bot_env(backends=("lidarr",))
    stock_radarr_movie(env.radarr)
    await env.say(ADMIN, "/movie Дюна")
    assert await env.db.get_session(ADMIN) is not None, "предусловие: у пользователя есть сессия"
    env.backends["lidarr"].fail_all(500)
    prompt = (await env.say(ADMIN, "🎵 Музыка"))[-1]

    calls = await env.say(ADMIN, "Radiohead", reply_to=prompt)

    assert "Артист не найден" not in "\n".join(texts(calls))
    assert await env.db.get_session(ADMIN) is None


async def test_unknown_artist_still_clears_the_stale_session(make_bot_env):
    """Парный контроль: прежняя ветка «ничего не найдено» работает как работала."""
    env = await make_bot_env(backends=("lidarr",))
    stock_radarr_movie(env.radarr)
    await env.say(ADMIN, "/movie Дюна")
    env.backends["lidarr"].route("GET", "/api/v1/artist/lookup", [])
    prompt = (await env.say(ADMIN, "🎵 Музыка"))[-1]

    calls = await env.say(ADMIN, "Radiohead", reply_to=prompt)

    assert any("Артист не найден" in text for text in texts(calls))
    assert await env.db.get_session(ADMIN) is None


# ------------------------------------------------- /wanted с длинной очередью


async def test_wanted_shows_the_true_total_of_a_long_queue(bot_env):
    movies = [{"id": i, "title": f"Movie {i:03d}", "year": 2020} for i in range(3)]
    episodes = [
        {"id": 100 + i, "title": "E", "seasonNumber": 1, "series": {"title": "Long Show"}} for i in range(4)
    ]
    bot_env.radarr.route(
        "GET", "/api/v3/wanted/missing",
        {"page": 1, "pageSize": 200, "totalRecords": 480, "records": movies},
    )
    bot_env.sonarr.route(
        "GET", "/api/v3/wanted/missing",
        {"page": 1, "pageSize": 200, "totalRecords": 1500, "records": episodes},
    )

    calls = await bot_env.say(ADMIN, "/wanted")

    text = "\n".join(texts(calls))
    assert "Ищется: 1980 позиций" in text
    assert "Фильмы (480)" in text
    assert "1500 эп." in text
    assert "Movie 000" in text and "Long Show" in text
