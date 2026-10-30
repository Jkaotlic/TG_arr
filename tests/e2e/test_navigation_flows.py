"""Сквозные сценарии задачи 6: навигация и интерфейс.

Аудит 2026-10-30: BUG-03 (страница загрузок), BUG-04/05/15 (музыка), BUG-13
(двойной тап), BUG-14 (ForceReply), LOGIC-14/15/17/18, BUG-16, LOGIC-19.
"""

from tests.e2e.fakes import BotEnv, TgCall, find_button, texts

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
