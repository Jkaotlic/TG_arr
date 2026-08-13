"""Глобальный обработчик ошибок aiogram (аудит 2026-08-13).

До этой правки у диспетчера не было ни одного `dp.errors` обработчика.
`LoggingMiddleware` исключение ловил и писал в лог с `exc_info`, то есть
наблюдаемость была — а пользователь при этом не получал НИЧЕГО: сообщение
молча не приходило, а у инлайн-кнопки оставались «часики» до таймаута
Telegram. «Бот завис» и «бот упал на этом запросе» выглядели для пользователя
одинаково.

Ответ пользователю здесь — best-effort: обработчик ошибок, который сам упал,
не должен ничего уронить (он последний в цепочке, дальше только полёт в
`dp.feed_update`).
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import ErrorEvent, Update

from bot.main import handle_unexpected_error


def _update_with_message(chat_id: int = 555):
    """Update с сообщением — минимум полей, которые читает обработчик."""
    message = MagicMock()
    message.chat = MagicMock(id=chat_id)
    message.answer = AsyncMock()
    update = MagicMock(spec=Update)
    update.update_id = 1
    update.message = message
    update.callback_query = None
    return update, message


def _update_with_callback(chat_id: int = 777):
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.chat = MagicMock(id=chat_id)
    callback.message.answer = AsyncMock()
    update = MagicMock(spec=Update)
    update.update_id = 2
    update.message = None
    update.callback_query = callback
    return update, callback


@pytest.mark.asyncio
async def test_user_gets_an_answer_when_a_handler_raises():
    """Главное: пользователь узнаёт, что запрос не выполнен."""
    update, message = _update_with_message()
    event = ErrorEvent(update=update, exception=RuntimeError("boom"))

    await handle_unexpected_error(event)

    message.answer.assert_awaited_once()
    assert "ошибка" in message.answer.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_callback_gets_its_spinner_stopped():
    """У колбэка «часики» крутятся до таймаута, пока не ответишь на него."""
    update, callback = _update_with_callback()
    event = ErrorEvent(update=update, exception=RuntimeError("boom"))

    await handle_unexpected_error(event)

    callback.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_internal_details_do_not_leak_to_the_user():
    """Текст исключения может нести URL с креденшелами — пользователю он не нужен."""
    update, message = _update_with_message()
    event = ErrorEvent(
        update=update,
        exception=RuntimeError("connect to http://user:pw@radarr:7878 failed"),
    )

    await handle_unexpected_error(event)

    sent = message.answer.await_args.args[0]
    assert "user:pw" not in sent
    assert "radarr:7878" not in sent


@pytest.mark.asyncio
async def test_a_failing_delivery_does_not_escape():
    """Обработчик ошибок — последний рубеж: он сам падать не имеет права."""
    update, message = _update_with_message()
    message.answer = AsyncMock(side_effect=Exception("bot was blocked"))
    event = ErrorEvent(update=update, exception=RuntimeError("boom"))

    await handle_unexpected_error(event)  # не должно бросить


@pytest.mark.asyncio
async def test_update_without_a_chat_is_survivable():
    """Не у каждого апдейта есть куда отвечать (например, my_chat_member)."""
    update = MagicMock(spec=Update)
    update.update_id = 3
    update.message = None
    update.callback_query = None
    event = ErrorEvent(update=update, exception=RuntimeError("boom"))

    await handle_unexpected_error(event)  # не должно бросить


@pytest.mark.asyncio
async def test_dispatcher_actually_routes_a_raising_handler_to_it():
    """Сквозная проверка проводки: не «функция в списке», а «диспетчер
    действительно доводит до неё упавший хендлер».

    Список обработчиков может быть заполнен, а исключение всё равно улетит
    мимо — например, если ошибка случилась в middleware. Здесь падение
    происходит внутри хендлера, ровно как в проде.
    """
    from aiogram import Dispatcher, Router
    from aiogram.types import Chat, Message, Update, User

    from bot.main import register_error_handler

    router = Router()

    @router.message()
    async def _boom(_message):
        raise RuntimeError("boom")

    dp = Dispatcher()
    dp.include_router(router)
    register_error_handler(dp)

    answered = []

    async def _fake_answer(text, **_kwargs):
        answered.append(text)

    message = Message(
        message_id=1,
        date=datetime(2026, 8, 13, tzinfo=timezone.utc),
        chat=Chat(id=555, type="private"),
        from_user=User(id=555, is_bot=False, first_name="T"),
        text="/whatever",
    ).as_(None)

    with patch.object(Message, "answer", new=lambda self, text, **kw: _fake_answer(text, **kw)):
        await dp.feed_update(bot=MagicMock(), update=Update(update_id=1, message=message))

    assert answered, "упавший хендлер не дошёл до обработчика ошибок"
    assert "ошибка" in answered[0].lower()


def test_dispatcher_has_the_error_handler_registered():
    """Проводка: без регистрации обработчик — мёртвый код.

    Проверяем именно то, что делает `main()`, а не то, что функция существует.
    """
    from aiogram import Dispatcher

    from bot.main import register_error_handler

    dp = Dispatcher()
    assert not dp.errors.handlers, "чистый Dispatcher не должен иметь обработчиков ошибок"

    register_error_handler(dp)

    assert [h.callback for h in dp.errors.handlers] == [handle_unexpected_error]
