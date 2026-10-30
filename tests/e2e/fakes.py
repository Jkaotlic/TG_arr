"""Подделки для e2e: Telegram на уровне сессии и *arr на настоящем HTTP.

Подделано ровно то, что вне нашего кода: Bot API и серверы *arr. Диспетчер,
middleware, роутеры, SQLite и httpx настоящие.

Важно про падения. Хендлеры ловят `except Exception`, а диспетчер отдаёт всё
остальное в обработчик ошибок, поэтому исключение из подделки до теста может не
дойти. Поэтому нарушения контракта Telegram копятся в `FakeTelegram.violations`,
а упавшие хендлеры в `BotEnv.handler_errors`, и `BotEnv` роняет тест сам, сразу
после апдейта.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import socket
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Awaitable, Callable, Iterable, Iterator, Optional
from urllib.parse import urlsplit

import pytest
from aiogram import Bot, Dispatcher, methods
from aiogram.client.default import Default
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    Chat,
    InaccessibleMessage,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
    ReplyKeyboardMarkup,
    Update,
    User,
)
from aiohttp import web

# --------------------------------------------------------------------- Telegram

BOT_ID = 42
BOT_TOKEN = f"{BOT_ID}:TEST"

MAX_TEXT_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024
MAX_CALLBACK_DATA_BYTES = 64
MAX_CALLBACK_ANSWER_LENGTH = 200
MAX_INLINE_BUTTONS = 100

NOT_MODIFIED = (
    "Bad Request: message is not modified: specified new message content and "
    "reply markup are exactly the same as a specified one"
)

#: Поля кнопки, задающие её действие. У настоящей кнопки ровно одно из них.
_BUTTON_ACTIONS = (
    "url",
    "callback_data",
    "web_app",
    "login_url",
    "switch_inline_query",
    "switch_inline_query_current_chat",
    "switch_inline_query_chosen_chat",
    "copy_text",
    "callback_game",
    "pay",
)

_BUTTON_URL_SCHEMES = frozenset({"http", "https", "tg"})

_TAG_ATTRIBUTES: dict[str, frozenset[str]] = {
    "b": frozenset(),
    "strong": frozenset(),
    "i": frozenset(),
    "em": frozenset(),
    "u": frozenset(),
    "ins": frozenset(),
    "s": frozenset(),
    "strike": frozenset(),
    "del": frozenset(),
    "pre": frozenset(),
    "code": frozenset({"class"}),
    "a": frozenset({"href"}),
    "span": frozenset({"class"}),
    "tg-spoiler": frozenset(),
    "tg-emoji": frozenset({"emoji-id"}),
    "blockquote": frozenset({"expandable"}),
}

_ENTITY = re.compile(r"&(?:lt|gt|amp|quot|#[0-9]+|#[xX][0-9a-fA-F]+);")
_CLOSE_TAG = re.compile(r"</([a-zA-Z][a-zA-Z0-9-]*)\s*>")
_OPEN_TAG = re.compile(r"""<([a-zA-Z][a-zA-Z0-9-]*)((?:\s+[a-zA-Z-]+(?:=(?:"[^"]*"|'[^']*'))?)*)\s*>""")
_ATTRIBUTE = re.compile(r"""([a-zA-Z-]+)(?:=(?:"([^"]*)"|'([^']*)'))?""")


class TelegramContractViolation(AssertionError):
    """Настоящий Telegram отверг бы этот запрос. Это ошибка бота, а не теста."""


def parse_telegram_html(text: str) -> str:
    """Проверить разметку как Bot API и вернуть текст без неё.

    Свой разбор вместо `html.parser`: тот прощает и `a < b`, и незакрытый тег в
    конце строки, причём по-разному в 3.12 и 3.14, а Telegram на них отвечает
    400. Правила взяты из раздела «HTML style» документации Bot API: разрешённые
    теги и атрибуты, вложенность без пересечений, только сущности `&lt; &gt;
    &amp; &quot;` и числовые, а голые `<`, `>`, `&` экранируются.
    """
    plain: list[str] = []
    stack: list[str] = []
    pos = 0
    while pos < len(text):
        char = text[pos]
        if char == "&":
            match = _ENTITY.match(text, pos)
            if match is None:
                raise TelegramContractViolation(f"голый или неизвестный & на позиции {pos}: {text[pos:pos + 30]!r}")
            plain.append(html.unescape(match.group()))
            pos = match.end()
        elif char == ">":
            raise TelegramContractViolation(f"голый > на позиции {pos}: {text[max(0, pos - 30):pos + 1]!r}")
        elif char == "<":
            close = _CLOSE_TAG.match(text, pos)
            if close is not None:
                tag = close.group(1).lower()
                if not stack or stack[-1] != tag:
                    opened = stack[-1] if stack else "ничего"
                    raise TelegramContractViolation(f"</{tag}> закрывает {opened} на позиции {pos}")
                stack.pop()
                pos = close.end()
                continue
            opening = _OPEN_TAG.match(text, pos)
            if opening is None:
                raise TelegramContractViolation(f"не тег: голый < на позиции {pos}: {text[pos:pos + 30]!r}")
            tag = opening.group(1).lower()
            _check_tag(tag, opening.group(2))
            stack.append(tag)
            pos = opening.end()
        else:
            plain.append(char)
            pos += 1
    if stack:
        raise TelegramContractViolation(f"не закрыты теги: {stack}")
    return "".join(plain)


def _check_tag(tag: str, raw_attributes: str) -> None:
    if tag not in _TAG_ATTRIBUTES:
        raise TelegramContractViolation(f"Telegram не знает тег <{tag}>")
    attributes = {m.group(1).lower(): m.group(2) or m.group(3) or "" for m in _ATTRIBUTE.finditer(raw_attributes)}
    unknown = set(attributes) - _TAG_ATTRIBUTES[tag]
    if unknown:
        raise TelegramContractViolation(f"<{tag}> не принимает атрибуты {sorted(unknown)}")
    if tag == "a" and not attributes.get("href"):
        raise TelegramContractViolation("<a> без href")
    if tag == "span" and attributes.get("class") != "tg-spoiler":
        raise TelegramContractViolation('<span> допустим только с class="tg-spoiler"')
    if tag == "code" and "class" in attributes and not attributes["class"].startswith("language-"):
        raise TelegramContractViolation('<code> допускает только class="language-..."')
    if tag == "tg-emoji" and not attributes.get("emoji-id"):
        raise TelegramContractViolation("<tg-emoji> без emoji-id")


def validate_keyboard(markup: Any) -> None:
    """Клавиатура как её проверяет Bot API.

    Инлайн: до 100 кнопок, `callback_data` до 64 байт, у кнопки ровно одно действие,
    `url` со схемой http, https или tg. Обычная: непустые ряды с непустым текстом.
    """
    if isinstance(markup, ReplyKeyboardMarkup):
        if not markup.keyboard or not all(markup.keyboard):
            raise TelegramContractViolation(f"пустая клавиатура или ряд: {markup.keyboard!r}")
        if not all(button.text for row in markup.keyboard for button in row):
            raise TelegramContractViolation("кнопка обычной клавиатуры без текста")
        return
    if not isinstance(markup, InlineKeyboardMarkup):
        return
    total = sum(len(row) for row in markup.inline_keyboard)
    if total > MAX_INLINE_BUTTONS:
        raise TelegramContractViolation(f"{total} кнопок при лимите {MAX_INLINE_BUTTONS}")
    for row in markup.inline_keyboard:
        for button in row:
            actions = [name for name in _BUTTON_ACTIONS if getattr(button, name) is not None]
            if len(actions) != 1:
                raise TelegramContractViolation(
                    f"кнопка {button.text!r}: нужно ровно одно действие, а заданы {actions or 'никакие'}"
                )
            if not button.text:
                raise TelegramContractViolation("кнопка без текста")
            if button.callback_data is not None:
                size = len(button.callback_data.encode("utf-8"))
                if not 1 <= size <= MAX_CALLBACK_DATA_BYTES:
                    raise TelegramContractViolation(
                        f"кнопка {button.text!r}: callback_data {size} байт: {button.callback_data!r}"
                    )
            if button.url is not None:
                parts = urlsplit(button.url)
                if parts.scheme not in _BUTTON_URL_SCHEMES or not parts.netloc:
                    raise TelegramContractViolation(f"кнопка {button.text!r}: недопустимый url {button.url!r}")


#: Какому апдейту принадлежит вызов Bot API. Контекст, а не срез журнала: при
#: двойном тапе через gather вызовы двух апдейтов идут вперемешку.
_CURRENT_UPDATE: ContextVar[Optional[int]] = ContextVar("e2e_current_update", default=None)


@dataclass
class TgCall:
    """Один запрос бота к Bot API и ответ подделки на него."""

    method: Any
    result: Any
    update_id: Optional[int]

    @property
    def name(self) -> str:
        return type(self.method).__name__

    @property
    def text(self) -> Optional[str]:
        """Текст как его отправил бот, с разметкой."""
        return getattr(self.method, "text", None) or getattr(self.method, "caption", None)

    @property
    def markup(self) -> Any:
        return getattr(self.method, "reply_markup", None)

    @property
    def buttons(self) -> list[InlineKeyboardButton]:
        if not isinstance(self.markup, InlineKeyboardMarkup):
            return []
        return [button for row in self.markup.inline_keyboard for button in row]

    @property
    def message(self) -> Optional[Message]:
        return self.result if isinstance(self.result, Message) else None


@dataclass
class _StoredMessage:
    """Что Telegram помнит о сообщении: нужно для «not modified» и для тапов."""

    message: Message
    raw_text: Optional[str]
    markup_json: Optional[str]


def _markup_json(markup: Any) -> Optional[str]:
    return markup.model_dump_json() if isinstance(markup, InlineKeyboardMarkup) else None


class FakeTelegram(BaseSession):
    """Сессия aiogram вместо сети: проверяет запросы как Bot API и пишет журнал."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TgCall] = []
        self.violations: list[str] = []
        self._messages: dict[tuple[int, int], _StoredMessage] = {}
        self._next_message_id = 1000
        self._handlers: dict[type, Callable[[Bot, Any], Any]] = {
            methods.SendMessage: self._send_message,
            methods.SendPhoto: self._send_photo,
            methods.EditMessageText: self._edit_text,
            methods.EditMessageReplyMarkup: self._edit_markup,
            methods.AnswerCallbackQuery: self._answer_callback,
            methods.DeleteMessage: self._delete_message,
            methods.GetMe: lambda bot, method: User(
                id=BOT_ID, is_bot=True, first_name="e2e", username="e2e_bot"
            ),
            methods.SetMyCommands: lambda bot, method: True,
        }

    def on_method(self, method_type: type, handler: Callable[[Bot, Any], Any]) -> None:
        """Научить подделку ещё одному методу Bot API (ответ должен быть реалистичным)."""
        self._handlers[method_type] = handler

    async def close(self) -> None:
        pass

    async def stream_content(self, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes, None]:
        raise TelegramContractViolation("FakeTelegram не умеет скачивать файлы")
        yield b""  # pragma: no cover -- делает функцию генератором

    async def make_request(self, bot: Bot, method: Any, timeout: Any = None) -> Any:
        handler = self._handlers.get(type(method))
        try:
            if handler is None:
                raise TelegramContractViolation(
                    f"FakeTelegram не знает метод {type(method).__name__}: "
                    "добавьте его через on_method с реалистичным ответом"
                )
            result = handler(bot, method)
        except TelegramContractViolation as violation:
            self.violations.append(f"{type(method).__name__}: {violation}")
            raise
        except TelegramBadRequest as error:
            self.calls.append(TgCall(method, error, _CURRENT_UPDATE.get()))
            raise
        self.calls.append(TgCall(method, result, _CURRENT_UPDATE.get()))
        return result

    def store_incoming(self, message: Message) -> None:
        """Сообщение пользователя: бот тоже может его удалить или на него ответить."""
        self._messages[(message.chat.id, message.message_id)] = _StoredMessage(message, message.text, None)

    def known_message(self, chat_id: int, message_id: int) -> Optional[Message]:
        stored = self._messages.get((chat_id, message_id))
        return stored.message if stored else None

    def new_message_id(self) -> int:
        self._next_message_id += 1
        return self._next_message_id

    # --- разбор запроса ------------------------------------------------------

    @staticmethod
    def _plain_text(bot: Bot, method: Any, raw: Optional[str], limit: int) -> str:
        """Текст после разбора разметки, с проверкой длины."""
        parse_mode = method.parse_mode
        if isinstance(parse_mode, Default):
            parse_mode = bot.default[parse_mode.name]
        if parse_mode is None:
            plain = raw or ""
        elif parse_mode == "HTML":
            plain = parse_telegram_html(raw or "")
        else:
            raise TelegramContractViolation(f"нет проверки для parse_mode={parse_mode!r}")
        if len(plain) > limit:
            raise TelegramContractViolation(f"{len(plain)} символов после разбора при лимите {limit}")
        return plain

    def _bot_message(self, bot: Bot, chat_id: int, message_id: int, **fields: Any) -> Message:
        return Message(
            message_id=message_id,
            date=datetime.now(timezone.utc),
            chat=Chat(id=chat_id, type="private"),
            from_user=User(id=BOT_ID, is_bot=True, first_name="e2e", username="e2e_bot"),
            **fields,
        ).as_(bot)

    def _check_reply_target(self, bot: Bot, method: Any) -> None:
        """Ответ на несуществующее сообщение: 400, если не разрешено явно.

        Обе формы запроса: `reply_parameters` и устаревшая `reply_to_message_id`.
        """
        parameters = method.reply_parameters
        target = parameters.message_id if parameters else method.reply_to_message_id
        if target is None:
            return
        allowed = parameters.allow_sending_without_reply if parameters else method.allow_sending_without_reply
        if isinstance(allowed, Default):
            allowed = bot.default[allowed.name]
        chat_id = parameters.chat_id if parameters and parameters.chat_id is not None else method.chat_id
        if not allowed and (chat_id, target) not in self._messages:
            raise TelegramBadRequest(method=method, message="Bad Request: message to be replied not found")

    def _stored_for_edit(self, method: Any) -> _StoredMessage:
        stored = self._messages.get((method.chat_id, method.message_id))
        if stored is None:
            raise TelegramBadRequest(method=method, message="Bad Request: message to edit not found")
        author = stored.message.from_user
        if author is None or author.id != BOT_ID:
            raise TelegramBadRequest(method=method, message="Bad Request: message can't be edited")
        return stored

    # --- методы --------------------------------------------------------------

    def _send_message(self, bot: Bot, method: methods.SendMessage) -> Message:
        plain = self._plain_text(bot, method, method.text, MAX_TEXT_LENGTH)
        if not plain.strip():
            raise TelegramContractViolation("пустой текст после разбора")
        validate_keyboard(method.reply_markup)
        self._check_reply_target(bot, method)
        return self._remember(bot, method.chat_id, self.new_message_id(), plain, method.text, method.reply_markup)

    def _send_photo(self, bot: Bot, method: methods.SendPhoto) -> Message:
        caption = self._plain_text(bot, method, method.caption, MAX_CAPTION_LENGTH)
        validate_keyboard(method.reply_markup)
        self._check_reply_target(bot, method)
        message_id = self.new_message_id()
        markup = method.reply_markup if isinstance(method.reply_markup, InlineKeyboardMarkup) else None
        message = self._bot_message(
            bot,
            method.chat_id,
            message_id,
            photo=[PhotoSize(file_id="e2e-photo", file_unique_id="e2e", width=100, height=100)],
            caption=caption or None,
            reply_markup=markup,
        )
        self._messages[(method.chat_id, message_id)] = _StoredMessage(message, method.caption, _markup_json(markup))
        return message

    def _edit_text(self, bot: Bot, method: methods.EditMessageText) -> Message:
        plain = self._plain_text(bot, method, method.text, MAX_TEXT_LENGTH)
        if not plain.strip():
            raise TelegramContractViolation("пустой текст после разбора")
        validate_keyboard(method.reply_markup)
        stored = self._stored_for_edit(method)
        if stored.message.photo:
            raise TelegramBadRequest(method=method, message="Bad Request: there is no text in the message to edit")
        if stored.raw_text == method.text and stored.markup_json == _markup_json(method.reply_markup):
            raise TelegramBadRequest(method=method, message=NOT_MODIFIED)
        return self._remember(bot, method.chat_id, method.message_id, plain, method.text, method.reply_markup)

    def _edit_markup(self, bot: Bot, method: methods.EditMessageReplyMarkup) -> Message:
        validate_keyboard(method.reply_markup)
        stored = self._stored_for_edit(method)
        if stored.markup_json == _markup_json(method.reply_markup):
            raise TelegramBadRequest(method=method, message=NOT_MODIFIED)
        message = stored.message.model_copy(update={"reply_markup": method.reply_markup}).as_(bot)
        stored.message = message
        stored.markup_json = _markup_json(method.reply_markup)
        return message

    def _remember(
        self, bot: Bot, chat_id: int, message_id: int, plain: str, raw: Optional[str], markup: Any
    ) -> Message:
        inline = markup if isinstance(markup, InlineKeyboardMarkup) else None
        message = self._bot_message(bot, chat_id, message_id, text=plain, reply_markup=inline)
        self._messages[(chat_id, message_id)] = _StoredMessage(message, raw, _markup_json(inline))
        return message

    def _answer_callback(self, bot: Bot, method: methods.AnswerCallbackQuery) -> bool:
        if method.text is not None and len(method.text) > MAX_CALLBACK_ANSWER_LENGTH:
            raise TelegramContractViolation(f"ответ на колбэк {len(method.text)} символов: {method.text!r}")
        return True

    def _delete_message(self, bot: Bot, method: methods.DeleteMessage) -> bool:
        if self._messages.pop((method.chat_id, method.message_id), None) is None:
            raise TelegramBadRequest(method=method, message="Bad Request: message to delete not found")
        return True


# --------------------------------------------------------------------- Сценарии


@dataclass
class FoundButton:
    """Кнопка и звонок, которым бот её прислал."""

    call: TgCall
    button: InlineKeyboardButton


def buttons(calls: Iterable[TgCall]) -> tuple[Optional[TgCall], list[InlineKeyboardButton]]:
    """Последний ответ с инлайн-клавиатурой и его кнопки."""
    for call in reversed(list(calls)):
        if call.buttons:
            return call, call.buttons
    return None, []


def find_button(calls: Iterable[TgCall], fragment: str) -> FoundButton:
    """Кнопка по куску текста, начиная с последних. Нет такой: тест падает со списком."""
    call_list = list(calls)
    for call in reversed(call_list):
        for button in call.buttons:
            if fragment in button.text:
                return FoundButton(call, button)
    existing = [f"{b.text!r} -> {b.callback_data or b.url}" for call in call_list for b in call.buttons]
    pytest.fail(f"Нет кнопки с {fragment!r}. Есть: {existing or 'ни одной'}")


def texts(calls: Iterable[TgCall]) -> list[str]:
    """Тексты всех ответов бота, включая ответы на колбэки."""
    return [call.text for call in calls if call.text]


class BotEnv:
    """Бот целиком в одном процессе: диалог с ним ведётся как из клиента Telegram.

    `say` и `tap` кормят настоящий диспетчер и возвращают вызовы Bot API, которые
    сделал именно этот апдейт. Падают сразу, если бот нарушил контракт Telegram
    или хендлер упал.
    """

    ADMIN = 1001
    USER = 1002
    STRANGER = 666

    buttons = staticmethod(buttons)
    find_button = staticmethod(find_button)
    texts = staticmethod(texts)

    def __init__(
        self,
        dp: Dispatcher,
        bot: Bot,
        tg: FakeTelegram,
        db: Any,
        backends: dict[str, "FakeBackend"],
    ) -> None:
        self.dp = dp
        self.bot = bot
        self.tg = tg
        self.db = db
        self.backends = backends
        self.handler_errors: list[BaseException] = []
        #: Сценарий про реакцию бота на 404 ставит True, иначе разбор фикстуры упадёт.
        self.allow_unmatched = False
        self._crash_expected = False
        self._update_id = 0
        dp.errors.outer_middleware(self._record_handler_error)

    @property
    def radarr(self) -> "FakeBackend":
        return self.backends["radarr"]

    @property
    def sonarr(self) -> "FakeBackend":
        return self.backends["sonarr"]

    @property
    def prowlarr(self) -> "FakeBackend":
        return self.backends["prowlarr"]

    async def _record_handler_error(
        self, handler: Callable[[Any, dict[str, Any]], Awaitable[Any]], event: Any, data: dict[str, Any]
    ) -> Any:
        self.handler_errors.append(event.exception)
        return await handler(event, data)

    @contextmanager
    def expect_handler_error(self) -> Iterator[None]:
        """Сценарий про обработчик ошибок: падение хендлера здесь ожидается."""
        self._crash_expected = True
        try:
            yield
        finally:
            self._crash_expected = False
        if not self.handler_errors:
            pytest.fail("Ожидалось падение хендлера, но его не было")
        self.handler_errors.clear()

    def check_contract(self) -> None:
        """Уронить тест, если бот нарушил контракт Telegram или хендлер упал."""
        if self.tg.violations:
            pytest.fail("Telegram отверг бы запросы бота:\n" + "\n".join(self.tg.violations))
        if self.handler_errors and not self._crash_expected:
            details = "".join(traceback.format_exception(self.handler_errors[0]))
            pytest.fail(f"Хендлер упал ({len(self.handler_errors)}):\n{details}")

    def check_unmatched(self) -> None:
        """Уронить тест, если бот ходил на маршрут, которого нет: молчаливый 404 прячет
        и опечатку в пути, и несозданный маршрут. Зовётся при разборе фикстуры."""
        if self.allow_unmatched:
            return
        unmatched = [
            f"{service}: {request.method} {request.path}"
            for service, backend in self.backends.items()
            for request in backend.unmatched
        ]
        if unmatched:
            pytest.fail(
                "Бот ходил на незаданные маршруты (ответ 404):\n"
                + "\n".join(unmatched)
                + "\nЗадайте маршрут или поставьте env.allow_unmatched = True"
            )

    def _next_update_id(self) -> int:
        self._update_id += 1
        return self._update_id

    def _user(self, uid: int) -> User:
        return User(id=uid, is_bot=False, first_name=f"u{uid}")

    async def _feed(self, update: Update) -> list[TgCall]:
        token = _CURRENT_UPDATE.set(update.update_id)
        try:
            await self.dp.feed_update(self.bot, update)
        finally:
            _CURRENT_UPDATE.reset(token)
        self.check_contract()
        return [call for call in self.tg.calls if call.update_id == update.update_id]

    async def say(self, uid: int, text: str, reply_to: "TgCall | Message | None" = None) -> list[TgCall]:
        """Пользователь пишет боту. `reply_to` делает сообщение ответом на указанное."""
        reply_message = reply_to.message if isinstance(reply_to, TgCall) else reply_to
        message = Message(
            message_id=self.tg.new_message_id(),
            date=datetime.now(timezone.utc),
            chat=Chat(id=uid, type="private"),
            from_user=self._user(uid),
            text=text,
            reply_to_message=reply_message,
        ).as_(self.bot)
        self.tg.store_incoming(message)
        update_id = self._next_update_id()
        return await self._feed(Update(update_id=update_id, message=message))

    async def tap(
        self,
        uid: int,
        sent: "TgCall | Message | None",
        data: "InlineKeyboardButton | FoundButton | str",
        *,
        inaccessible: bool = False,
    ) -> list[TgCall]:
        """Пользователь жмёт кнопку под сообщением `sent`.

        Сообщение в колбэке берётся таким, каким его сейчас помнит Telegram.
        `sent=None` годится для подделанного колбэка: сообщения как будто нет в
        журнале. Под уже удалённым сообщением колбэк несёт его старую копию, а правка
        получит «message to edit not found». `inaccessible=True` эмулирует кнопку
        старше 48 часов.
        """
        callback_data = _callback_data_of(data)
        message = self._callback_message(uid, sent, inaccessible)
        update_id = self._next_update_id()
        callback = CallbackQuery(
            id=str(update_id),
            from_user=self._user(uid),
            chat_instance="e2e",
            message=message,
            data=callback_data,
        ).as_(self.bot)
        return await self._feed(Update(update_id=update_id, callback_query=callback))

    async def press(self, uid: int, calls: Iterable[TgCall], fragment: str) -> list[TgCall]:
        """Найти кнопку по тексту среди ответов бота и нажать её."""
        found = find_button(calls, fragment)
        return await self.tap(uid, found.call, found.button)

    def _callback_message(
        self, uid: int, sent: "TgCall | Message | None", inaccessible: bool
    ) -> "Message | InaccessibleMessage":
        original = sent.message if isinstance(sent, TgCall) else sent
        if sent is not None and original is None:
            pytest.fail(f"Вызов {sent.name} не вернул сообщение: нажимать нечего")
        chat_id = original.chat.id if original else uid
        message_id = original.message_id if original else self.tg.new_message_id()
        if inaccessible:
            return InaccessibleMessage(chat=Chat(id=chat_id, type="private"), message_id=message_id)
        known = self.tg.known_message(chat_id, message_id)
        if known is not None:
            return known.as_(self.bot)
        if original is not None:
            return original.as_(self.bot)
        message = Message(
            message_id=message_id,
            date=datetime.now(timezone.utc),
            chat=Chat(id=chat_id, type="private"),
            from_user=User(id=BOT_ID, is_bot=True, first_name="e2e"),
            text="(сообщение вне журнала)",
        ).as_(self.bot)
        self.tg.store_incoming(message)
        return message


def _callback_data_of(data: "InlineKeyboardButton | FoundButton | str") -> str:
    if isinstance(data, FoundButton):
        data = data.button
    if isinstance(data, InlineKeyboardButton):
        if data.callback_data is None:
            pytest.fail(f"Кнопка {data.text!r} не шлёт колбэк: url={data.url}")
        return data.callback_data
    return data


# ------------------------------------------------------------------ Бэкенды *arr


@dataclass
class RecordedRequest:
    """Запрос, дошедший до поддельного бэкенда."""

    method: str
    path: str
    query: dict[str, Any]
    json: Any
    body: str
    headers: dict[str, str]
    matched: bool


@dataclass
class _Route:
    method: str
    pattern: "re.Pattern[str]"
    responder: Callable[[RecordedRequest], Any] | None
    payload: Any
    status: int
    hang: bool
    headers: dict[str, str] | None = None


def _path_pattern(path: str) -> "re.Pattern[str]":
    """`/api/v3/movie/{id}` в регулярку: `{...}` ловит один сегмент пути."""
    parts = re.split(r"\{[^{}/]+\}", path)
    return re.compile("[^/]+".join(re.escape(part) for part in parts) + r"\Z")


#: Умолчания, которые иначе пришлось бы повторять в каждом тесте. Всё остальное
#: тест задаёт сам: неизвестный маршрут это 404 и запись в журнале, а не [].
_ARR_DEFAULTS: dict[str, Any] = {
    "/qualityprofile": [{"id": 1, "name": "Any"}],
    "/rootfolder": [{"id": 1, "path": "/data", "freeSpace": 10**12, "accessible": True}],
}
_SERVICE_DEFAULTS: dict[str, tuple[str, dict[str, Any]]] = {
    "radarr": ("/api/v3", _ARR_DEFAULTS),
    "sonarr": ("/api/v3", _ARR_DEFAULTS),
    "lidarr": ("/api/v1", {**_ARR_DEFAULTS, "/metadataprofile": [{"id": 1, "name": "Standard"}]}),
    "prowlarr": ("/api/v1", {"/indexer": []}),
}


class FakeBackend:
    """Поддельный сервер на настоящем сокете 127.0.0.1, порт выбирает система."""

    def __init__(self, service: str) -> None:
        self.service = service
        self.requests: list[RecordedRequest] = []
        self._routes: list[_Route] = []
        self._mode_status: Optional[int] = None
        self._mode_hang = False
        self._release_hung = asyncio.Event()
        self._runner: Optional[web.AppRunner] = None
        self.url = ""
        prefix, defaults = _SERVICE_DEFAULTS.get(service, ("", {}))
        if service in _SERVICE_DEFAULTS:
            self.route("GET", f"{prefix}/system/status", {"version": "6.0.0", "appName": service})
        for path, payload in defaults.items():
            self.route("GET", prefix + path, payload)
        if service == "qbittorrent":
            self._stock_qbittorrent()

    def _stock_qbittorrent(self) -> None:
        """Вход с сессионной кукой и пустая сводка: без них не открыть ни один список."""
        self.route("POST", "/api/v2/auth/login", "Ok.", headers={"Set-Cookie": "SID=e2e; Path=/"})
        self.route("GET", "/api/v2/app/version", "v5.0.0")
        self.route("GET", "/api/v2/transfer/info", {"connection_status": "connected"})
        self.route("GET", "/api/v2/sync/maindata", {"rid": 1, "server_state": {}})
        self.route("GET", "/api/v2/torrents/info", [])

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._dispatch)
        self._runner = web.AppRunner(app, shutdown_timeout=1.0, access_log=None)
        await self._runner.setup()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        await web.SockSite(self._runner, sock).start()
        self.url = f"http://127.0.0.1:{sock.getsockname()[1]}"

    async def stop(self) -> None:
        self._release_hung.set()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # --- настройка из теста --------------------------------------------------

    def route(
        self,
        method: str,
        path: str,
        payload: Any = None,
        *,
        status: int = 200,
        hang: bool = False,
        responder: Callable[[RecordedRequest], Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Задать ответ. Последний заданный маршрут важнее: тест перекрывает умолчания.

        `responder(request)` считает тело по запросу, `hang=True` принимает запрос и
        не отвечает, `status` задаёт код, `headers` добавляет заголовки ответа (так
        qBittorrent выдаёт сессионную куку). `{id}` в пути ловит один сегмент.
        """
        self._routes.insert(
            0, _Route(method.upper(), _path_pattern(path), responder, payload, status, hang, headers)
        )

    def hang_all(self) -> None:
        """Все запросы приняты, ни на один нет ответа."""
        self._mode_hang = True

    def fail_all(self, status: int = 500) -> None:
        """На все запросы один код ответа, маршруты не важны."""
        self._mode_status = status

    def recover(self) -> None:
        """Снять `hang_all` и `fail_all`. Зависшие запросы получат обычный ответ."""
        self._mode_hang = False
        self._mode_status = None
        self._release_hung.set()
        self._release_hung = asyncio.Event()

    # --- проверки из теста ---------------------------------------------------

    def calls(self, method: str, path: str) -> list[RecordedRequest]:
        """Запросы с этим методом и путём (`{id}` ловит сегмент)."""
        pattern = _path_pattern(path)
        return [r for r in self.requests if r.method == method.upper() and pattern.match(r.path)]

    @property
    def unmatched(self) -> list[RecordedRequest]:
        """Запросы, для которых нет маршрута: бэкенд ответил им 404."""
        return [r for r in self.requests if not r.matched]

    # --- сервер --------------------------------------------------------------

    def _find_route(self, method: str, path: str) -> Optional[_Route]:
        for route in self._routes:
            if route.method == method and route.pattern.match(path):
                return route
        return None

    async def _dispatch(self, request: web.Request) -> web.StreamResponse:
        body = await request.text()
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = None
        route = self._find_route(request.method, request.path)
        query: dict[str, Any] = {}
        for key in request.query:
            values = request.query.getall(key)
            query[key] = values if len(values) > 1 else values[0]
        record = RecordedRequest(
            method=request.method,
            path=request.path,
            query=query,
            json=parsed,
            body=body,
            headers=dict(request.headers),
            # При fail_all и hang_all бэкенд отвечает не 404: маршрут тут ни при чём.
            matched=route is not None or self._mode_hang or self._mode_status is not None,
        )
        self.requests.append(record)

        if self._mode_hang or (route is not None and route.hang):
            await self._release_hung.wait()
        if self._mode_status is not None:
            return web.json_response({"message": "e2e: fixed status"}, status=self._mode_status)
        if route is None:
            return web.json_response({"message": f"e2e: нет маршрута {request.method} {request.path}"}, status=404)
        payload = route.responder(record) if route.responder is not None else route.payload
        return web.json_response(payload, status=route.status, headers=route.headers)


# ------------------------------------------------------------------------ Данные


def movie(**overrides: Any) -> dict[str, Any]:
    """Фильм, как его отдаёт Radarr. Разметка в overview нарочно: бот обязан экранировать."""
    return {
        "title": "Dune",
        "year": 2021,
        "tmdbId": 438631,
        "imdbId": "tt1160419",
        "titleSlug": "dune-438631",
        "overview": "Paul <Atreides>",
        "runtime": 155,
        "qualityProfileId": 1,
        "monitored": False,
        "images": [],
        **overrides,
    }


def release(**overrides: Any) -> dict[str, Any]:
    """Релиз из `GET /release`."""
    return {
        "guid": "e2e-guid-1",
        "title": "Dune.2021.2160p.WEB-DL.DDP5.1.H.265",
        "size": 20_000_000_000,
        "seeders": 50,
        "leechers": 2,
        "indexer": "Tracker",
        "indexerId": 1,
        "protocol": "torrent",
        "rejected": False,
        "rejections": [],
        "approved": True,
        "quality": {"quality": {"id": 19, "name": "WEBDL-2160p"}},
        "customFormatScore": 100,
        "languages": [{"id": 1, "name": "English"}],
        **overrides,
    }


def stock_radarr_movie(
    radarr: FakeBackend,
    movie_data: Optional[dict[str, Any]] = None,
    releases: Optional[list[dict[str, Any]]] = None,
    *,
    movie_id: int = 7,
) -> dict[str, Any]:
    """Настроить Radarr на поток «/movie → список → карточка → Скачать».

    Возвращает фильм с присвоенным id. Отдельные маршруты тест может перекрыть
    после вызова.
    """
    base = movie_data if movie_data is not None else movie()
    added = {**base, "id": movie_id}
    radarr.route("GET", "/api/v3/movie/lookup", [base])
    radarr.route("GET", "/api/v3/movie", [])
    radarr.route("POST", "/api/v3/movie", added, status=201)
    radarr.route("GET", f"/api/v3/movie/{movie_id}", added)
    # После граба бот включает мониторинг фильма.
    radarr.route("PUT", f"/api/v3/movie/{movie_id}", responder=lambda request: request.json)
    radarr.route("GET", "/api/v3/release", releases if releases is not None else [release()])
    radarr.route("POST", "/api/v3/release", responder=lambda request: request.json)
    return added
