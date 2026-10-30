"""Сквозные сценарии задачи 6: навигация и интерфейс.

Аудит 2026-10-30: BUG-03 (страница загрузок), BUG-04/05/15 (музыка), BUG-13
(двойной тап), BUG-14 (ForceReply), LOGIC-14/15/17/18, BUG-16, LOGIC-19.
"""

import asyncio

from tests.e2e.fakes import BotEnv, TgCall, find_button, stock_radarr_movie, texts

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
