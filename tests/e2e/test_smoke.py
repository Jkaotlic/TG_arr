"""Сквозные сценарии: то, чего не видят тесты хендлеров на MagicMock.

Роутинг, права, настоящие HTTP-ответы и кнопки, нажатые по `callback_data` из того,
что бот реально отправил.
"""

import asyncio

from bot.middleware.auth import STALE_MESSAGE_TEXT
from bot.ui.menu import MENU_MUSIC, MUSIC_PROMPT
from tests.e2e.fakes import BotEnv, find_button, release, stock_radarr_movie, texts

ADMIN, USER, STRANGER = BotEnv.ADMIN, BotEnv.USER, BotEnv.STRANGER


async def _open_release_card(env: BotEnv):
    """/movie → список релизов → карточка. Возвращает ответы с кнопкой «Скачать»."""
    listing = await env.say(ADMIN, "/movie Дюна")
    return await env.press(ADMIN, listing, "2160p")


async def test_stranger_is_refused_on_start(bot_env):
    calls = await bot_env.say(STRANGER, "/start")

    assert any("Доступ запрещён" in text for text in texts(calls))
    assert all(not backend.requests for backend in bot_env.backends.values())


async def test_stranger_cannot_use_a_real_grab_button(bot_env):
    stock_radarr_movie(bot_env.radarr)
    card = await _open_release_card(bot_env)
    download = find_button(card, "Скачать")

    calls = await bot_env.tap(STRANGER, download.call, download)

    assert [(call.name, call.text) for call in calls] == [("AnswerCallbackQuery", "Доступ запрещён")]
    assert bot_env.radarr.calls("POST", "/api/v3/release") == []


async def test_plain_user_cannot_list_users(bot_env):
    calls = await bot_env.say(USER, "/users")

    assert any("Только для администратора" in text for text in texts(calls))


async def test_movie_flow_grabs_the_release_the_user_saw(bot_env):
    stock_radarr_movie(
        bot_env.radarr,
        releases=[release(guid="wanted-guid", indexerId=11)],
    )

    card = await _open_release_card(bot_env)
    result = await bot_env.press(ADMIN, card, "Скачать")

    final = result[-1]
    assert final.name == "EditMessageText"
    assert "Dune (2021)" in final.text
    assert "Жду загрузку" in final.text
    posts = bot_env.radarr.calls("POST", "/api/v3/release")
    assert len(posts) == 1
    assert posts[0].json["guid"] == "wanted-guid"
    assert posts[0].json["indexerId"] == 11
    assert bot_env.radarr.unmatched == []


async def test_concurrent_double_tap_posts_once(bot_env):
    stock_radarr_movie(bot_env.radarr)
    download = find_button(await _open_release_card(bot_env), "Скачать")

    first, second = await asyncio.gather(
        bot_env.tap(ADMIN, download.call, download),
        bot_env.tap(ADMIN, download.call, download),
    )

    assert len(bot_env.radarr.calls("POST", "/api/v3/release")) == 1
    answers = [call.text for call in (*first, *second) if call.name == "AnswerCallbackQuery"]
    assert any(text and "Уже обрабатываю" in text for text in answers), answers


async def test_plain_text_goes_to_search(bot_env):
    stock_radarr_movie(bot_env.radarr)
    bot_env.sonarr.route("GET", "/api/v3/series/lookup", [])

    calls = await bot_env.say(ADMIN, "Дюна")

    lookups = bot_env.radarr.calls("GET", "/api/v3/movie/lookup")
    assert [request.query["term"] for request in lookups] == ["Дюна"]
    assert any(call.buttons for call in calls), texts(calls)


async def test_command_in_reply_to_music_prompt_reaches_its_own_router(make_bot_env):
    env = await make_bot_env(backends=("lidarr",))
    prompt = (await env.say(ADMIN, MENU_MUSIC))[-1]
    assert prompt.message.text == MUSIC_PROMPT

    calls = await env.say(ADMIN, "/downloads", reply_to=prompt)

    assert any("qBittorrent" in text for text in texts(calls)), texts(calls)
    assert env.backends["lidarr"].requests == []


async def test_text_in_reply_to_music_prompt_searches_artists(make_bot_env):
    """Парный контроль к сценарию выше: сам ответ на подсказку идёт в поиск артиста."""
    env = await make_bot_env(backends=("lidarr",))
    lidarr = env.backends["lidarr"]
    lidarr.route("GET", "/api/v1/artist/lookup", [])
    prompt = (await env.say(ADMIN, MENU_MUSIC))[-1]

    await env.say(ADMIN, "Radiohead", reply_to=prompt)

    assert [request.query["term"] for request in lidarr.calls("GET", "/api/v1/artist/lookup")] == ["Radiohead"]


async def test_button_older_than_48h_gets_an_answer(bot_env):
    """StaleMessageMiddleware подключён в проводке, а не только существует."""
    calls = await bot_env.tap(ADMIN, None, "whatever", inaccessible=True)

    assert [call.text for call in calls] == [STALE_MESSAGE_TEXT]
