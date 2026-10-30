"""Сквозные сценарии задачи 6: навигация и интерфейс.

Аудит 2026-10-30: BUG-03 (страница загрузок), BUG-04/05/15 (музыка), BUG-13
(двойной тап), BUG-14 (ForceReply), LOGIC-14/15/17/18, BUG-16, LOGIC-19.
"""

import asyncio

import pytest

from bot.ui.callbacks import TitleActionCB, TsTorrentCB
from bot.ui.menu import MENU_DOWNLOADS, MENU_MUSIC
from tests.e2e.fakes import BotEnv, FakeResponse, TgCall, find_button, stock_radarr_movie, texts

ADMIN = BotEnv.ADMIN


def _edit(calls: list[TgCall]) -> TgCall:
    """Единственная правка текста в ответах на апдейт."""
    edits = [c for c in calls if c.name == "EditMessageText"]
    assert len(edits) == 1, [c.name for c in calls]
    return edits[0]


def _alerts(calls: list[TgCall]) -> list[str]:
    return [c.method.text for c in calls if c.name == "AnswerCallbackQuery" and c.method.show_alert]


# ------------------------------------------------- BUG-03: страница загрузок


def _torrents(count: int) -> list[dict]:
    return [
        {
            "hash": f"{index:040x}",
            "name": f"Torrent {index:02d}",
            "state": "downloading",
            "progress": 0.5,
            "size": 10**9,
            "total_size": 10**9,
            "added_on": 1_700_000_000 + index,
        }
        for index in range(count)
    ]


async def _downloads_env(make_bot_env, count: int):
    env = await make_bot_env(backends=("qbittorrent",))
    state = {"torrents": _torrents(count)}
    env.backends["qbittorrent"].route(
        "GET", "/api/v2/torrents/info", responder=lambda request: state["torrents"]
    )
    return env, state


async def test_refresh_after_the_list_shrank_redraws_the_last_page(make_bot_env):
    env, state = await _downloads_env(make_bot_env, 7)
    first = await env.say(ADMIN, "/downloads")
    page_two = await env.press(ADMIN, first, "▶️")
    state["torrents"] = _torrents(3)

    calls = await env.press(ADMIN, page_two, "Обновить")

    assert "Torrent 00" in " ".join(b.text for b in _edit(calls).buttons)
    assert _alerts(calls) == []


async def test_back_from_the_more_panel_after_the_list_shrank(make_bot_env):
    env, state = await _downloads_env(make_bot_env, 7)
    first = await env.say(ADMIN, "/downloads")
    page_two = await env.press(ADMIN, first, "▶️")
    panel = await env.press(ADMIN, page_two, "Ещё")
    state["torrents"] = _torrents(3)

    calls = await env.press(ADMIN, panel, "Назад")

    assert "Torrent 00" in " ".join(b.text for b in _edit(calls).buttons)


async def test_paging_forward_past_the_end_still_alerts(make_bot_env):
    """Парный контроль TEST-08a: явное листание за край остаётся алертом."""
    env, state = await _downloads_env(make_bot_env, 7)
    first = await env.say(ADMIN, "/downloads")
    forward = find_button(first, "▶️")
    state["torrents"] = _torrents(3)

    calls = await env.tap(ADMIN, forward.call, forward.button)

    assert _alerts(calls) == ["Неверная страница"]
    assert not [c for c in calls if c.name == "EditMessageText"]


async def test_old_format_page_button_in_a_chat_does_not_raise(make_bot_env):
    """Кнопка старого вида (`tpg:page:flt`) из прежней версии жива: ответ, не исключение."""
    env, _ = await _downloads_env(make_bot_env, 3)

    calls = await env.tap(ADMIN, None, "tpg:5:all")

    assert any(c.name in ("AnswerCallbackQuery", "EditMessageText") for c in calls)


# ------------------------------------------------- музыка: BUG-04, BUG-05, BUG-15


def _artist(name: str, mb_id: str, lidarr_id: int) -> dict:
    return {"artistName": name, "foreignArtistId": mb_id, "id": lidarr_id}


def _album(album_id: int, title: str, artist_id: int) -> dict:
    return {"id": album_id, "title": title, "artistId": artist_id, "releaseDate": "2020-01-01T00:00:00Z"}


async def _music_library(make_bot_env, *, torrents: list | None = None):
    """Lidarr, где у артистов Alpha и Beta уже есть id, по одному альбому и пустые раздачи."""
    env = await make_bot_env(backends=("lidarr",))
    lidarr = env.backends["lidarr"]
    artists = {"Alpha": [_artist("Alpha One", "mb-a1", 5)], "Beta": [_artist("Beta One", "mb-b1", 6)]}
    albums = {"5": [_album(31, "Alpha Album", 5)], "6": [_album(41, "Beta Album", 6)]}
    lidarr.route("GET", "/api/v1/artist/lookup", responder=lambda r: artists.get(r.query["term"], []))
    lidarr.route("GET", "/api/v1/album", responder=lambda r: albums.get(r.query["artistId"], []))
    lidarr.route("GET", "/api/v1/release", torrents if torrents is not None else [])
    return env


async def _open_artist(env, query: str, name: str):
    prompt = (await env.say(ADMIN, "🎵 Музыка"))[-1]
    found = await env.say(ADMIN, query, reply_to=prompt)
    return await env.press(ADMIN, found, name)


async def _open_albums(env, query: str, name: str):
    card = await _open_artist(env, query, name)
    return await env.press(ADMIN, card, "Искать")


async def test_back_from_empty_torrents_keeps_the_artist(make_bot_env):
    env = await _music_library(make_bot_env)
    albums = await _open_albums(env, "Alpha", "Alpha One")
    sources = await env.press(ADMIN, albums, "Alpha Album")
    empty = await env.press(ADMIN, sources, "Торренты")
    assert any("не найдены" in text for text in texts(empty))

    scope = await env.press(ADMIN, empty, "Назад")
    whole = await env.press(ADMIN, scope, "Вся дискография")

    assert not stale_text(whole), texts(whole)
    assert any("вся дискография?" in text for text in texts(whole))


def stale_text(calls) -> bool:
    return any("устарел" in (c.text or "") for c in calls if c.name == "AnswerCallbackQuery")


async def test_album_button_of_another_artist_answers_stale(make_bot_env):
    """BUG-15: артист B выбран, кэш альбомов ещё от A: кнопка A не должна открыть альбом."""
    env = await _music_library(make_bot_env)
    alpha_albums = await _open_albums(env, "Alpha", "Alpha One")
    album_button = find_button(alpha_albums, "Alpha Album")
    await _open_artist(env, "Beta", "Beta One")

    calls = await env.tap(ADMIN, album_button.call, album_button)

    assert stale_text(calls), texts(calls)
    assert not [c for c in calls if c.name == "EditMessageText"]


async def _wait_for_request(backend, method: str, path: str) -> None:
    for _ in range(200):
        if backend.calls(method, path):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"запрос {method} {path} так и не пришёл")


async def test_new_search_during_torrent_lookup_is_not_overwritten(make_bot_env):
    """BUG-05: пока Lidarr ищет раздачи, пользователь начал другой поиск."""
    env = await _music_library(make_bot_env)
    lidarr = env.backends["lidarr"]
    albums = await _open_albums(env, "Alpha", "Alpha One")
    sources = await env.press(ADMIN, albums, "Alpha Album")
    lidarr.route("GET", "/api/v1/release", [{"guid": "g", "title": "Alpha Album FLAC", "indexerId": 1,
                                             "protocol": "torrent", "seeders": 3, "size": 10**8}], hang=True)
    torrents = find_button(sources, "Торренты")

    lookup = asyncio.create_task(env.tap(ADMIN, torrents.call, torrents.button))
    await _wait_for_request(lidarr, "GET", "/api/v1/release")
    stock_radarr_movie(env.radarr)
    await env.say(ADMIN, "/movie Дюна")
    session_before = await env.db.get_session(ADMIN)
    lidarr.recover()
    calls = await lookup

    session_after = await env.db.get_session(ADMIN)
    assert session_before is not None and session_after is not None
    assert session_after.model_dump() == session_before.model_dump()
    assert not any("Alpha Album FLAC" in text for text in texts(calls))


async def test_cancel_during_torrent_lookup_does_not_resurrect_the_session(make_bot_env):
    env = await _music_library(make_bot_env)
    lidarr = env.backends["lidarr"]
    albums = await _open_albums(env, "Alpha", "Alpha One")
    sources = await env.press(ADMIN, albums, "Alpha Album")
    lidarr.route("GET", "/api/v1/release", [{"guid": "g", "title": "Alpha Album FLAC", "indexerId": 1,
                                             "protocol": "torrent", "seeders": 3, "size": 10**8}], hang=True)
    torrents = find_button(sources, "Торренты")
    cancel = find_button(sources, "Отмена")

    lookup = asyncio.create_task(env.tap(ADMIN, torrents.call, torrents.button))
    await _wait_for_request(lidarr, "GET", "/api/v1/release")
    await env.tap(ADMIN, cancel.call, cancel.button)
    lidarr.recover()
    calls = await lookup

    assert await env.db.get_session(ADMIN) is None
    assert [c for c in calls if c.name == "EditMessageText" and "Alpha Album FLAC" in (c.text or "")] == []


async def test_cancel_during_artist_add_does_not_resurrect_the_session(make_bot_env):
    """BUG-05, вторая точка: «Искать (добавит в Lidarr)» ждёт POST артиста."""
    env = await _music_library(make_bot_env)
    lidarr = env.backends["lidarr"]
    lidarr.route("GET", "/api/v1/artist/lookup", [{"artistName": "Gamma", "foreignArtistId": "mb-g"}])
    lidarr.route("GET", "/api/v1/artist", [])
    lidarr.route("POST", "/api/v1/artist", {"artistName": "Gamma", "foreignArtistId": "mb-g", "id": 9}, hang=True)
    lidarr.route("GET", "/api/v1/album", [_album(51, "Gamma Album", 9)])
    card = await _open_artist(env, "Gamma", "Gamma")
    add = find_button(card, "Искать")
    cancel = find_button(card, "Отмена")

    adding = asyncio.create_task(env.tap(ADMIN, add.call, add.button))
    await _wait_for_request(lidarr, "POST", "/api/v1/artist")
    await env.tap(ADMIN, cancel.call, cancel.button)
    lidarr.recover()
    calls = await adding

    assert await env.db.get_session(ADMIN) is None
    assert not [c for c in calls if c.name == "EditMessageText" and "Gamma Album" in " ".join(b.text for b in c.buttons)]


# ------------------------------------------------- BUG-14: ForceReply и команды


@pytest.mark.parametrize("typed", ["/downloads", MENU_DOWNLOADS])
@pytest.mark.parametrize("prompt_source", ["torrserver", "music"])
async def test_command_or_menu_button_typed_into_a_prompt_reply_is_not_a_query(make_bot_env, prompt_source, typed):
    env = await make_bot_env(backends=("torrserver", "qbittorrent", "lidarr"))
    env.backends["torrserver"].route("POST", "/torrents", [])
    env.backends["lidarr"].route("GET", "/api/v1/artist/lookup", [])
    if prompt_source == "torrserver":
        prompt = next(c for c in await env.tap(ADMIN, None, "ts_search") if c.name == "SendMessage")
    else:
        prompt = (await env.say(ADMIN, MENU_MUSIC))[-1]
    torrserver_before = len(env.backends["torrserver"].requests)
    lidarr_before = len(env.backends["lidarr"].requests)

    await env.say(ADMIN, typed, reply_to=prompt)

    assert env.backends["qbittorrent"].calls("GET", "/api/v2/torrents/info"), "список загрузок не открыт"
    assert len(env.backends["torrserver"].requests) == torrserver_before
    assert len(env.backends["lidarr"].requests) == lidarr_before


# ------------------------------------------------- LOGIC-19: список TorrServer


async def test_torrserver_list_failure_after_delete_is_not_an_empty_list(make_bot_env):
    env = await make_bot_env(backends=("torrserver",))

    def torrents(request):
        if request.json["action"] == "rem":
            return {}
        return FakeResponse({"message": "boom"}, status=500)

    env.backends["torrserver"].route("POST", "/torrents", responder=torrents)
    delete = TsTorrentCB(action="delconf", h="a" * 40).pack()

    calls = await env.tap(ADMIN, None, delete)

    text = "\n".join(texts(calls))
    assert "Раздача удалена" in text
    assert "Список пуст" not in text
    assert "недоступен" in text


# ------------------------------------------------- LOGIC-14, BUG-13: настройки


def _alert_texts(calls: list[TgCall]) -> list[str]:
    return [c.method.text for c in calls if c.name == "AnswerCallbackQuery" and c.method.text]


async def test_settings_open_with_sonarr_down_and_say_what_is_missing(bot_env):
    bot_env.sonarr.fail_all(500)

    calls = await bot_env.say(ADMIN, "/settings")

    text = "\n".join(texts(calls))
    assert "Ваши настройки" in text
    assert "Ошибка загрузки настроек" not in text
    assert "Sonarr" in text and "недоступен" in text


async def test_unrelated_toggle_with_sonarr_down_is_not_an_update_error(bot_env):
    bot_env.sonarr.fail_all(500)
    menu = await bot_env.say(ADMIN, "/settings")
    quality = await bot_env.press(ADMIN, menu, "Качество")

    calls = await bot_env.press(ADMIN, quality, "1080p")

    assert _alert_texts(calls) == ["Разрешение обновлено!"]
    assert any("1080p" in text for text in texts(calls))
    assert (await bot_env.db.get_user(ADMIN)).preferences.preferred_resolution == "1080p"


async def test_double_tap_on_a_settings_toggle_gives_no_error_alert(bot_env):
    menu = await bot_env.say(ADMIN, "/settings")
    quality = await bot_env.press(ADMIN, menu, "Качество")
    pick = find_button(quality, "1080p")

    await bot_env.tap(ADMIN, pick.call, pick.button)
    second = await bot_env.tap(ADMIN, pick.call, pick.button)

    assert not any("Ошибка" in text for text in _alert_texts(second)), _alert_texts(second)


# ------------------------------------------------- Emby: BUG-16, BUG-13


def _stock_emby(env) -> None:
    emby = env.backends["emby"]
    emby.route("GET", "/System/Info", {
        "ServerName": "e2e-emby", "Version": "4.8", "OperatingSystem": "Linux",
        "CanSelfRestart": True, "CanSelfUpdate": True, "HasUpdateAvailable": False,
    })
    emby.route("GET", "/Library/VirtualFolders", [])
    emby.route("GET", "/Sessions", [])


async def test_emby_status_failure_keeps_a_refresh_button(make_bot_env):
    env = await make_bot_env(backends=("emby",))
    _stock_emby(env)
    shown = await env.say(ADMIN, "/emby")
    env.backends["emby"].fail_all(500)

    failed = await env.press(ADMIN, shown, "Обновить")

    assert any("Обновить" in b.text for b in _edit(failed).buttons)
    env.backends["emby"].recover()
    _stock_emby(env)
    recovered = await env.press(ADMIN, failed, "Обновить")
    assert any("e2e-emby" in text for text in texts(recovered))


async def test_emby_failure_keyboard_does_not_offer_admin_actions_to_a_user(make_bot_env):
    env = await make_bot_env(backends=("emby",))
    env.backends["emby"].fail_all(500)

    calls = await env.say(BotEnv.USER, "/emby")

    labels = [b.text for c in calls for b in c.buttons]
    assert any("Обновить" in label for label in labels)
    assert not any("Перезагрузить" in label or "Сканировать" in label for label in labels)


async def test_emby_restart_prompt_double_tap_has_no_error(make_bot_env):
    env = await make_bot_env(backends=("emby",))
    _stock_emby(env)
    shown = await env.say(ADMIN, "/emby")
    restart = find_button(shown, "Перезагрузить")

    await env.tap(ADMIN, restart.call, restart.button)
    second = await env.tap(ADMIN, restart.call, restart.button)

    assert not any("Ошибка" in text for text in _alert_texts(second)), _alert_texts(second)


# ------------------------------------------------- /title: LOGIC-15, 17, 18, BUG-13


def _library_movie(movie_id: int, title: str = "Dune") -> dict:
    return {
        "id": movie_id, "title": title, "year": 2021, "tmdbId": 1000 + movie_id, "monitored": True,
        "hasFile": True, "qualityProfileId": 1, "images": [],
    }


def _stock_library(env, movies: list[dict], series: list[dict] | None = None) -> None:
    # `{id}` ловит и «lookup», а последний заданный маршрут важнее: lookup идёт после.
    env.radarr.route("GET", "/api/v3/movie/{id}", responder=lambda r: next(
        m for m in movies if f"/{m['id']}" == r.path[r.path.rindex("/"):]
    ))
    env.radarr.route("PUT", "/api/v3/movie/{id}", responder=lambda request: request.json)
    env.radarr.route("GET", "/api/v3/movie/lookup", movies)
    env.sonarr.route("GET", "/api/v3/series/lookup", series or [])


async def test_title_delete_on_a_cache_miss_says_nothing_about_files(bot_env):
    _stock_library(bot_env, [_library_movie(7)])

    calls = await bot_env.tap(ADMIN, None, TitleActionCB(action="delete", kind="movie", title_id="7").pack())

    text = "\n".join(texts(calls))
    assert "Удалить" in text
    assert "на диске нет" not in text


async def test_title_delete_with_a_known_title_still_warns_about_files(bot_env):
    _stock_library(bot_env, [_library_movie(7)])
    card = await bot_env.say(ADMIN, "/title Dune")

    calls = await bot_env.press(ADMIN, card, "Удалить")

    assert any("останется" in text for text in texts(calls))


async def test_title_choices_say_how_many_are_shown(bot_env):
    _stock_library(bot_env, [_library_movie(i, f"Dune {i}") for i in range(1, 9)])

    calls = await bot_env.say(ADMIN, "/title Dune")

    assert any("Показано 5 из 8" in text for text in texts(calls)), texts(calls)


async def test_title_double_tap_on_unmonitor_has_no_error(bot_env):
    _stock_library(bot_env, [_library_movie(7)])
    card = await bot_env.say(ADMIN, "/title Dune")
    unmonitor = find_button(card, "Снять мониторинг")

    await bot_env.tap(ADMIN, unmonitor.call, unmonitor.button)
    second = await bot_env.tap(ADMIN, unmonitor.call, unmonitor.button)

    assert not any("Не удалось" in text or "Ошибка" in text for text in texts(second)), texts(second)


async def test_unmonitored_series_card_warns_that_only_new_episodes_are_followed(bot_env):
    series = {
        "id": 3, "title": "Hit Show", "year": 2020, "tvdbId": 55, "monitored": False,
        "seasons": [{"seasonNumber": 1, "monitored": False}], "images": [],
    }
    _stock_library(bot_env, [], [series])

    calls = await bot_env.say(ADMIN, "/title Hit Show")

    text = "\n".join(texts(calls))
    assert "только новые серии" in text


# ------------------------------------------------- музыка без Lidarr: имена в сообщениях


async def test_music_search_in_a_slskd_only_setup_does_not_name_lidarr(make_bot_env):
    env = await make_bot_env(backends=("slskd",), allow_unmatched=True)
    prompt = (await env.say(ADMIN, MENU_MUSIC))[-1]

    calls = await env.say(ADMIN, "Radiohead", reply_to=prompt)

    shown = "\n".join(texts(calls))
    assert "Lidarr" not in shown, shown
    assert "Soulseek" in shown
