"""Свободный поиск: /find и кнопка на тупиковом экране обычного поиска.

Каталожный поток отвечает на «хочу это в библиотеку»: он резолвит кандидата,
добавляет его в *arr и перечисляет релизы через интерактивный поиск *arr, со
всем его вердиктом. Про тайтл, которого TMDb/TVDB не знает, ему сказать нечего
— и раньше пользователь получал «ничего не найдено» без единого следующего
шага. Этот поток спрашивает Prowlarr напрямую.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.models import (
    ActionLog,
    ActionType,
    ContentType,
    SearchResult,
    SearchSession,
    User,
)


def _release(title: str = "Концерт 2019 1080p", idx: int = 0) -> SearchResult:
    return SearchResult(
        guid=f"guid-{idx}",
        origin="prowlarr",
        indexer="RuTracker",
        indexer_id=2,
        title=title,
        size=1_000_000,
        seeders=5,
        download_url="http://localhost:9696/2/download?apikey=SECRET",
    )


def _message() -> MagicMock:
    msg = MagicMock()
    msg.edit_text = AsyncMock()
    msg.answer = AsyncMock(return_value=msg)
    return msg


def _db(session=None) -> MagicMock:
    db = MagicMock()
    db.get_session = AsyncMock(return_value=session)
    db.save_session = AsyncMock()
    db.update_session = AsyncMock(return_value=True)
    db.delete_session = AsyncMock()
    db.log_action = AsyncMock()
    return db


def _callback(message=None) -> MagicMock:
    callback = MagicMock()
    callback.from_user = MagicMock()
    callback.from_user.id = 5
    callback.answer = AsyncMock()
    callback.message = message or _message()
    return callback


def _free_session(results=None) -> SearchSession:
    return SearchSession(
        user_id=5,
        query="концерт",
        content_type=ContentType.UNKNOWN,
        results=results if results is not None else [_release()],
        free_search=True,
    )


def _patched_services(monkeypatch, *, search_service=None, add_service=None):
    from bot.handlers.search import free as free_mod

    monkeypatch.setattr(
        free_mod._search,
        "get_services",
        AsyncMock(return_value=(search_service or MagicMock(), add_service or MagicMock())),
    )
    return free_mod


@pytest.mark.asyncio
async def test_find_stores_a_free_search_session(monkeypatch):
    search_service = MagicMock()
    search_service.search_free_text = AsyncMock(return_value=[_release()])
    free_mod = _patched_services(monkeypatch, search_service=search_service)

    db = _db()
    await free_mod.run_free_search(_message(), "концерт", User(tg_id=5), db)

    saved = db.save_session.await_args.args[1]
    assert saved.free_search is True
    assert saved.query == "концерт"
    assert len(saved.results) == 1


@pytest.mark.asyncio
async def test_find_reports_an_empty_result_set_without_saving_a_session(monkeypatch):
    search_service = MagicMock()
    search_service.search_free_text = AsyncMock(return_value=[])
    free_mod = _patched_services(monkeypatch, search_service=search_service)

    db = _db()
    msg = _message()
    await free_mod.run_free_search(msg, "ничего", User(tg_id=5), db)

    db.save_session.assert_not_awaited()
    msg.edit_text.assert_awaited()


@pytest.mark.asyncio
async def test_search_failure_is_explained_not_swallowed(monkeypatch):
    from bot.clients.base import ServiceConnectionError

    search_service = MagicMock()
    search_service.search_free_text = AsyncMock(side_effect=ServiceConnectionError("нет связи"))
    free_mod = _patched_services(monkeypatch, search_service=search_service)

    msg = _message()
    await free_mod.run_free_search(msg, "дюна", User(tg_id=5), _db())

    text = msg.edit_text.await_args.args[0]
    assert "Индексеры" in text


@pytest.mark.asyncio
async def test_grab_goes_through_add_service_without_an_arr_id(monkeypatch):
    from bot.ui.callbacks import FindGrabCB

    add_service = MagicMock()
    add_service.grab_release = AsyncMock(return_value=(
        True,
        ActionLog(user_id=0, action_type=ActionType.GRAB, content_type=ContentType.UNKNOWN),
    ))
    free_mod = _patched_services(monkeypatch, add_service=add_service)

    db = _db(_free_session())
    await free_mod.handle_find_grab(_callback(), FindGrabCB(idx=0), User(tg_id=5), db)

    add_service.grab_release.assert_awaited_once()
    assert "arr_id" not in add_service.grab_release.await_args.kwargs


@pytest.mark.asyncio
async def test_success_message_says_the_title_is_not_in_the_library(monkeypatch):
    """Иначе «скачал, а в Emby нет» читается как поломка."""
    from bot.ui.callbacks import FindGrabCB

    add_service = MagicMock()
    add_service.grab_release = AsyncMock(return_value=(
        True,
        ActionLog(user_id=0, action_type=ActionType.GRAB, content_type=ContentType.UNKNOWN),
    ))
    free_mod = _patched_services(monkeypatch, add_service=add_service)

    callback = _callback()
    await free_mod.handle_find_grab(callback, FindGrabCB(idx=0), User(tg_id=5), _db(_free_session()))

    final = callback.message.edit_text.await_args.args[0]
    assert "библиотек" in final.lower()


@pytest.mark.asyncio
async def test_failed_grab_shows_the_reason(monkeypatch):
    from bot.ui.callbacks import FindGrabCB

    action = ActionLog(user_id=0, action_type=ActionType.GRAB, content_type=ContentType.UNKNOWN)
    action.error_message = "Тайтла нет в библиотеке — Radarr/Sonarr не может искать сам"
    add_service = MagicMock()
    add_service.grab_release = AsyncMock(return_value=(False, action))
    free_mod = _patched_services(monkeypatch, add_service=add_service)

    callback = _callback()
    await free_mod.handle_find_grab(callback, FindGrabCB(idx=0), User(tg_id=5), _db(_free_session()))

    final = callback.message.edit_text.await_args.args[0]
    assert "Radarr/Sonarr" in final


@pytest.mark.asyncio
async def test_stale_results_are_refused():
    from bot.handlers.search import free as free_mod
    from bot.ui.callbacks import FindGrabCB

    callback = _callback()
    await free_mod.handle_find_grab(callback, FindGrabCB(idx=0), User(tg_id=5), _db(None))

    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_a_catalogue_session_is_not_treated_as_a_free_one():
    """У каталожной сессии свой обработчик граба, который умеет arr_id. Чужой
    коллбэк не должен на неё налететь."""
    from bot.handlers.search import free as free_mod
    from bot.ui.callbacks import FindGrabCB

    catalogue = SearchSession(
        user_id=5, query="дюна", content_type=ContentType.MOVIE, results=[_release()],
    )
    callback = _callback()
    await free_mod.handle_find_grab(callback, FindGrabCB(idx=0), User(tg_id=5), _db(catalogue))

    assert callback.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_release_card_warns_the_title_is_not_in_the_library():
    from bot.handlers.search import free as free_mod
    from bot.ui.callbacks import FindReleaseCB

    callback = _callback()
    await free_mod.handle_find_release(
        callback, FindReleaseCB(idx=0), User(tg_id=5), _db(_free_session()),
    )

    text = callback.message.edit_text.await_args.args[0]
    assert "библиотек" in text.lower()


@pytest.mark.asyncio
async def test_pagination_refuses_a_stale_session():
    from bot.handlers.search import free as free_mod
    from bot.ui.callbacks import FindPageCB

    callback = _callback()
    await free_mod.handle_find_page(callback, FindPageCB(page=1), User(tg_id=5), _db(None))

    assert callback.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_free_search_button_reuses_the_query_from_the_session(monkeypatch):
    """Пользователь не должен набирать запрос заново — он уже его вводил."""
    search_service = MagicMock()
    search_service.search_free_text = AsyncMock(return_value=[])
    free_mod = _patched_services(monkeypatch, search_service=search_service)

    dead_end = SearchSession(user_id=5, query="какой-то концерт", content_type=ContentType.UNKNOWN)
    await free_mod.handle_free_search_button(_callback(), User(tg_id=5), _db(dead_end))

    search_service.search_free_text.assert_awaited_once()
    assert search_service.search_free_text.await_args.args[0] == "какой-то концерт"


def test_find_is_in_the_published_command_catalog():
    from bot.ui.commands import COMMAND_GROUPS

    names = {name for _, entries in COMMAND_GROUPS for name, _ in entries}
    assert "find" in names
