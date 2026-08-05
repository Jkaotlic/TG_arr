"""Хендлеры раздела TorrServer: панель и её деградация."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import ForceReply

from bot.handlers import torrserver as ts_handlers
from bot.models import SyncHookResult, TorrServerFile, TorrServerRelease, TorrServerStats, TorrServerTorrent
from bot.services.torrserver_service import AddResult
from bot.ui.menu import TORRSERVER_PROMPT


@pytest.fixture(autouse=True)
def _clear_results_cache():
    """`_results` is a module-level dict shared by every test in this file.
    Several tests mutate `_results[7]` with no teardown of their own; without
    this reset, whether a given test sees an empty or a stale cache depends
    on file/execution order rather than on what the test itself arranged."""
    ts_handlers._results.clear()
    yield
    ts_handlers._results.clear()


@pytest.mark.asyncio
async def test_panel_says_how_to_configure_when_disabled():
    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock, return_value=None):
        text, keyboard = await ts_handlers.render_panel()

    assert "TORRSERVER_URL" in text
    assert keyboard is None


@pytest.mark.asyncio
async def test_panel_renders_stats():
    client = MagicMock()
    client.get_stats = AsyncMock(return_value=TorrServerStats(
        version="MatriX.142.2", torrent_count=6, total_size=1024, cache_size=1024,
        use_disk=False, source_count=6,
    ))
    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock, return_value=client):
        text, keyboard = await ts_handlers.render_panel()

    assert "MatriX.142.2" in text
    assert keyboard is not None


@pytest.mark.asyncio
async def test_panel_survives_a_dead_server():
    client = MagicMock()
    client.get_stats = AsyncMock(side_effect=ts_handlers.TorrServerError("недоступен"))
    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock, return_value=client):
        text, keyboard = await ts_handlers.render_panel()

    assert "недоступен" in text
    assert keyboard is not None  # кнопка «Обновить» должна остаться


@pytest.mark.asyncio
async def test_panel_survives_an_unexpected_error():
    """A non-TorrServerError failure (network blip, bug, whatever) out of
    get_stats() must still degrade to a card with the retry keyboard, not an
    unhandled exception reaching the aiogram dispatcher."""
    client = MagicMock()
    client.get_stats = AsyncMock(side_effect=RuntimeError("boom"))
    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock, return_value=client):
        text, keyboard = await ts_handlers.render_panel()

    assert "❌" in text
    assert keyboard is not None  # кнопка «Обновить» должна остаться


def test_router_is_registered_before_search_router():
    """handle_text_search ловит любой текст, а aiogram не каскадирует
    обработчики после совпадения — наш роутер обязан идти раньше.

    Существующие роутеры (кроме torrserver) создаются без ``name=`` (см.
    bot/handlers/search/services.py, emby.py, ...), поэтому сравнение по
    ``.name`` неприменимо в общем случае — сверяем позицию по идентичности
    объекта роутера.
    """
    from bot.handlers import setup_routers
    from bot.handlers import search as search_module
    from bot.handlers import torrserver as ts_module

    subs = setup_routers().sub_routers
    assert subs.index(ts_module.router) < subs.index(search_module.router)


HIT = TorrServerRelease(title="Dune 2021 BDRemux", size=1024, seeders=10,
                        link="http://p:9696/2/download?link=a", tracker="RuTracker.org")


def _message(text="Dune"):
    message = MagicMock()
    message.text = text
    message.answer = AsyncMock()
    message.from_user = MagicMock(id=7)
    return message


@pytest.mark.asyncio
async def test_search_prompt_uses_force_reply_with_the_exact_marker():
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.answer = AsyncMock()

    with patch.object(ts_handlers, "accessible_message", return_value=callback.message):
        await ts_handlers.handle_search_prompt(callback)

    text = callback.message.answer.await_args.args[0]
    markup = callback.message.answer.await_args.kwargs["reply_markup"]
    assert text == TORRSERVER_PROMPT
    assert isinstance(markup, ForceReply)


@pytest.mark.asyncio
async def test_reply_runs_a_search_and_caches_hits():
    client = MagicMock()
    client.search = AsyncMock(return_value=[HIT])
    status = MagicMock()
    status.edit_text = AsyncMock()
    message = _message("Dune 2021")
    message.answer = AsyncMock(return_value=status)

    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock, return_value=client):
        await ts_handlers.handle_search_reply(message)

    client.search.assert_awaited_once()
    assert ts_handlers._results[7][0].title == "Dune 2021 BDRemux"
    assert "Найдено" in status.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_reply_rejects_a_too_short_query():
    message = _message("a")
    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock) as client_getter:
        await ts_handlers.handle_search_reply(message)

    client_getter.assert_not_awaited()
    assert "коротк" in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_empty_search_result_is_reported():
    client = MagicMock()
    client.search = AsyncMock(return_value=[])
    status = MagicMock()
    status.edit_text = AsyncMock()
    message = _message("асдфасдф")
    message.answer = AsyncMock(return_value=status)

    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock, return_value=client):
        await ts_handlers.handle_search_reply(message)

    assert "не найдено" in status.edit_text.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_add_publishes_and_answers_with_the_stream_link():
    ts_handlers._results[7] = [HIT]
    torrent = TorrServerTorrent(hash="abc", title="Dune 2021", stat=3,
                                stat_string="Torrent working",
                                files=[TorrServerFile(id=1, path="Dune/Dune.mkv", length=10)])
    service = MagicMock()
    service.add_and_publish = AsyncMock(return_value=AddResult(
        torrent=torrent, metadata_ready=True,
        sync=SyncHookResult(status="ok", duration_s=1.0),
        stream_url="http://ts:8090/stream/Dune.mkv?link=abc&index=1&play",
    ))

    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.from_user = MagicMock(id=7)
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    from bot.ui.callbacks import TsAddCB

    with patch.object(ts_handlers, "get_torrserver_service", new_callable=AsyncMock,
                      return_value=service), \
         patch.object(ts_handlers, "accessible_message", return_value=callback.message):
        await ts_handlers.handle_add(callback, TsAddCB(idx=0))

    service.add_and_publish.assert_awaited_once_with(HIT.link, HIT.title, "")
    assert "Emby" in callback.message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_no_stale_cache_leaks_in_from_a_previous_test():
    """`_results` is a module-level dict shared by every test in this file.
    The previous test (test_add_publishes_and_answers_with_the_stream_link)
    sets `_results[7]` and never tears it down — without the autouse
    `_clear_results_cache` fixture, this test would inherit that stale entry
    purely because of file order, not because anything here set it up."""
    assert ts_handlers._results.get(7) is None


@pytest.mark.asyncio
async def test_add_after_cache_expiry_asks_to_search_again():
    ts_handlers._results.pop(7, None)
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.from_user = MagicMock(id=7)
    callback.message = MagicMock()

    from bot.ui.callbacks import TsAddCB

    with patch.object(ts_handlers, "accessible_message", return_value=callback.message):
        await ts_handlers.handle_add(callback, TsAddCB(idx=0))

    assert callback.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_search_reply_reports_a_torrserver_error():
    client = MagicMock()
    client.search = AsyncMock(side_effect=ts_handlers.TorrServerError("сервер недоступен"))
    status = MagicMock()
    status.edit_text = AsyncMock()
    message = _message("Dune 2021")
    message.answer = AsyncMock(return_value=status)

    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock, return_value=client):
        await ts_handlers.handle_search_reply(message)

    assert "сервер недоступен" in status.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_search_reply_survives_an_unexpected_error():
    """A non-TorrServerError failure (network blip, bug, whatever) out of
    client.search() must still degrade to an error message, not an unhandled
    exception reaching the aiogram dispatcher (mirrors render_panel's own
    generic-exception coverage)."""
    client = MagicMock()
    client.search = AsyncMock(side_effect=RuntimeError("boom"))
    status = MagicMock()
    status.edit_text = AsyncMock()
    message = _message("Dune 2021")
    message.answer = AsyncMock(return_value=status)

    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock, return_value=client):
        await ts_handlers.handle_search_reply(message)

    assert "недоступен" in status.edit_text.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_reply_rejects_a_too_long_query():
    message = _message("a" * (ts_handlers.MAX_QUERY_LENGTH + 1))
    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock) as client_getter:
        await ts_handlers.handle_search_reply(message)

    client_getter.assert_not_awaited()
    assert "длинн" in message.answer.await_args.args[0]


def _releases(n):
    """`n` distinct, orderable hits — titles double as an easy way to tell
    "which absolute item did we actually open" apart in assertions."""
    return [
        TorrServerRelease(title=f"Item {i} 2021", size=1024, seeders=i, link=f"http://p/{i}")
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_page_renders_the_requested_page():
    """handle_page's own contract: the requested page number is the one that
    actually gets rendered (results_per_page defaults to 5, so 7 hits split
    into page 0 = items 0-4, page 1 = items 5-6)."""
    ts_handlers._results[7] = _releases(7)
    message = MagicMock()
    message.edit_text = AsyncMock()
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.from_user = MagicMock(id=7)

    from bot.ui.callbacks import TsPageCB

    with patch.object(ts_handlers, "accessible_message", return_value=message):
        await ts_handlers.handle_page(callback, TsPageCB(page=1))

    text = message.edit_text.await_args.args[0]
    assert "Item 5" in text
    assert "Item 0" not in text
    callback.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_page_with_stale_cache_alerts_instead_of_rendering():
    ts_handlers._results.pop(7, None)
    message = MagicMock()
    message.edit_text = AsyncMock()
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.from_user = MagicMock(id=7)

    from bot.ui.callbacks import TsPageCB

    with patch.object(ts_handlers, "accessible_message", return_value=message):
        await ts_handlers.handle_page(callback, TsPageCB(page=1))

    message.edit_text.assert_not_awaited()
    assert callback.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_page_two_tap_opens_the_release_actually_tapped():
    """The diff's top risk: a page-relative button index would resolve a tap
    on page 2 to whatever sits at the *same on-page position* on page 1
    instead of the item the user actually tapped. This test builds the
    keyboard for real (via handle_page, which drives the actual
    Keyboards.torrserver_results(..., offset=...)) and follows its own
    packed callback_data through handle_release — so a regression in either
    the keyboard's offset or the handler's index use would be caught here,
    not just by reading the code."""
    ts_handlers._results[7] = _releases(7)  # page 0: idx 0-4, page 1: idx 5-6

    page_message = MagicMock()
    page_message.edit_text = AsyncMock()
    page_callback = MagicMock()
    page_callback.answer = AsyncMock()
    page_callback.from_user = MagicMock(id=7)

    from bot.ui.callbacks import TsPageCB, TsReleaseCB

    with patch.object(ts_handlers, "accessible_message", return_value=page_message):
        await ts_handlers.handle_page(page_callback, TsPageCB(page=1))

    markup = page_message.edit_text.await_args.kwargs["reply_markup"]
    first_hit_button = markup.inline_keyboard[0][0]
    # The first hit button on page two must be the item at absolute index 5.
    assert "Item 5" in first_hit_button.text
    tapped = TsReleaseCB.unpack(first_hit_button.callback_data)

    release_message = MagicMock()
    release_message.edit_text = AsyncMock()
    release_callback = MagicMock()
    release_callback.answer = AsyncMock()
    release_callback.from_user = MagicMock(id=7)

    with patch.object(ts_handlers, "accessible_message", return_value=release_message):
        await ts_handlers.handle_release(release_callback, tapped)

    opened_text = release_message.edit_text.await_args.args[0]
    assert "Item 5" in opened_text
    assert "Item 0" not in opened_text


@pytest.mark.asyncio
async def test_release_after_cache_expiry_asks_to_search_again():
    ts_handlers._results.pop(7, None)
    message = MagicMock()
    message.edit_text = AsyncMock()
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.from_user = MagicMock(id=7)

    from bot.ui.callbacks import TsReleaseCB

    with patch.object(ts_handlers, "accessible_message", return_value=message):
        await ts_handlers.handle_release(callback, TsReleaseCB(idx=0))

    message.edit_text.assert_not_awaited()
    assert callback.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_list_shows_torrents():
    client = MagicMock()
    client.list_torrents = AsyncMock(return_value=[TorrServerTorrent(
        hash="abc", title="Dune 2021", size=1024, stat=5, stat_string="Torrent in db")])
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock, return_value=client), \
         patch.object(ts_handlers, "accessible_message", return_value=callback.message):
        await ts_handlers.handle_list(callback, is_admin=True)

    assert "Dune 2021" in callback.message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_list_survives_an_unexpected_error():
    """A non-TorrServerError failure while rendering the list (a TelegramBadRequest
    from an oversized message, or — as here — a ValueError from
    Keyboards.torrserver_list when a hash overflows callback_data) must not
    reach the dispatcher unhandled. Because the keyboard is built as an
    argument to safe_edit, the exception happens *before* callback.answer()
    is reached — without a guard the user gets a spinning button and no
    message at all."""
    client = MagicMock()
    client.list_torrents = AsyncMock(return_value=[TorrServerTorrent(
        hash="abc", title="Dune 2021", size=1024, stat=5, stat_string="Torrent in db")])
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    from bot.ui.keyboards import Keyboards

    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock, return_value=client), \
         patch.object(ts_handlers, "accessible_message", return_value=callback.message), \
         patch.object(Keyboards, "torrserver_list", side_effect=ValueError("too long")):
        await ts_handlers.handle_list(callback, is_admin=True)

    assert callback.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_delete_confirm_survives_an_unexpected_error_while_rendering():
    """Same failure mode as handle_list, but after the deletion itself
    already succeeded — the torrent must stay deleted and the handler must
    not crash even though re-rendering the list blew up."""
    client = MagicMock()
    client.remove_torrent = AsyncMock()
    client.list_torrents = AsyncMock(return_value=[])
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    from bot.ui.callbacks import TsTorrentCB
    from bot.ui.keyboards import Keyboards

    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock, return_value=client), \
         patch.object(ts_handlers, "accessible_message", return_value=callback.message), \
         patch.object(Keyboards, "torrserver_list", side_effect=ValueError("too long")):
        await ts_handlers.handle_delete_confirm(
            callback, TsTorrentCB(action="delconf", h="abc"), is_admin=True)

    client.remove_torrent.assert_awaited_once_with("abc")
    assert callback.answer.await_args_list  # answered at least once, no crash


@pytest.mark.asyncio
async def test_delete_prompt_is_refused_for_non_admins():
    """The confirmation prompt is itself admin-gated: a crafted callback that
    skips the panel's delete button and hits `del` directly must not reach
    the "are you sure" screen either — only `handle_delete_confirm` actually
    deletes, but showing the prompt to a non-admin is still a reachable path
    worth refusing explicitly."""
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.message = MagicMock()

    from bot.ui.callbacks import TsTorrentCB

    await ts_handlers.handle_delete_prompt(
        callback, TsTorrentCB(action="del", h="abc"), is_admin=False)

    assert callback.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_delete_is_refused_for_non_admins():
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.message = MagicMock()

    from bot.ui.callbacks import TsTorrentCB

    await ts_handlers.handle_delete_confirm(
        callback, TsTorrentCB(action="delconf", h="abc"), is_admin=False)

    assert callback.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_delete_removes_and_warns_about_the_delayed_strm_cleanup():
    client = MagicMock()
    client.remove_torrent = AsyncMock()
    client.list_torrents = AsyncMock(return_value=[])
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    from bot.ui.callbacks import TsTorrentCB

    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock, return_value=client), \
         patch.object(ts_handlers, "accessible_message", return_value=callback.message):
        await ts_handlers.handle_delete_confirm(
            callback, TsTorrentCB(action="delconf", h="abc"), is_admin=True)

    client.remove_torrent.assert_awaited_once_with("abc")
    assert "10 минут" in callback.message.edit_text.await_args.args[0]
