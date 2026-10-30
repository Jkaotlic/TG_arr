"""Аудит 2026-10-30, задача 6: проверки, для которых сквозной стенд избыточен.

Основные сценарии лежат в tests/e2e/test_navigation_flows.py.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.clients.torrserver import TorrServerError
from bot.handlers import torrserver
from bot.handlers.search import commands, free
from bot.ui.formatters import Formatters

#: Строка со всеми тремя символами, которые экранирует format_error.
NASTY = "a & <b>"
ESCAPED_ONCE = "a &amp; &lt;b&gt;"


def _assert_escaped_once(text: str) -> None:
    assert ESCAPED_ONCE in text, text
    assert "&amp;amp;" not in text and "&amp;lt;" not in text, text


@pytest.mark.asyncio
async def test_torrserver_search_error_is_escaped_once():
    """BUG-17: format_error уже экранирует, второй html.escape давал «&amp;amp;»."""
    message = MagicMock()
    message.text = "query"
    message.from_user = MagicMock(id=1)
    status = MagicMock()
    status.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=status)
    client = MagicMock()
    client.search = AsyncMock(side_effect=TorrServerError(NASTY))

    with patch.object(torrserver, "get_torrserver", AsyncMock(return_value=client)):
        await torrserver.handle_search_reply(message)

    _assert_escaped_once(status.edit_text.await_args.args[0])


@pytest.mark.asyncio
async def test_torrserver_add_error_is_escaped_once():
    callback = MagicMock()
    callback.from_user = MagicMock(id=1)
    callback.answer = AsyncMock()
    callback.message.edit_text = AsyncMock()
    service = MagicMock()
    service.add_and_publish = AsyncMock(side_effect=TorrServerError(NASTY))
    release = MagicMock(link="magnet:?x", title="t")

    with patch.object(torrserver, "_results", {1: [release]}), \
            patch.object(torrserver._tokens, "check", return_value=True), \
            patch.object(torrserver, "get_torrserver_service", AsyncMock(return_value=service)), \
            patch.object(torrserver, "accessible_message", return_value=callback.message), \
            patch("bot.handlers.search._claim_grab", AsyncMock(return_value=True)), \
            patch("bot.handlers.search._release_grab"):
        await torrserver.handle_add(callback, MagicMock(tok="t", idx=0))

    texts = [c.args[0] for c in callback.message.edit_text.await_args_list]
    _assert_escaped_once(texts[-1])


@pytest.mark.asyncio
async def test_free_search_failure_text_is_not_escaped_twice():
    """Латентно: describe_search_failure сейчас без спецсимволов, но не должен ломаться при них."""
    status = MagicMock()
    status.edit_text = AsyncMock()
    service = MagicMock()
    service.search_free_text = AsyncMock(side_effect=RuntimeError("x"))
    user = MagicMock()
    user.tg_id = 1
    user.preferences.preferred_resolution = None

    with patch.object(free._search, "get_services", AsyncMock(return_value=(service, MagicMock()))), \
            patch.object(free, "describe_search_failure", return_value=NASTY):
        await free.run_free_search(status, "q", user, MagicMock())

    _assert_escaped_once(status.edit_text.await_args.args[0])


def test_format_error_escapes_exactly_once():
    _assert_escaped_once(Formatters.format_error(NASTY))


def test_commands_module_does_not_escape_before_format_error():
    """commands.py:782 — тот же приём; проверяем исходник, драйвить весь поиск ради этого незачем."""
    import inspect

    source = inspect.getsource(commands)
    assert "format_error(html.escape(" not in source
