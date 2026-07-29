"""`accessible_message` — the single guard every callback handler goes through.

Telegram delivers a callback whose `message` is an `InaccessibleMessage` once
the original message is too old to edit (48h+) or was deleted. Editing it
raises at runtime, so the handlers must all drop out before touching it.
"""

from unittest.mock import MagicMock

from aiogram.types import CallbackQuery, Chat, InaccessibleMessage, Message

from bot.handlers.common import accessible_message


def _callback(message) -> CallbackQuery:
    cb = MagicMock(spec=CallbackQuery)
    cb.message = message
    return cb


def test_inaccessible_message_is_rejected():
    """The whole point: an expired/deleted message must not be edited."""
    chat = Chat(id=1, type="private")
    inaccessible = InaccessibleMessage(chat=chat, message_id=1, date=0)

    assert accessible_message(_callback(inaccessible)) is None


def test_missing_message_is_rejected():
    """`message` is absent for inline-mode callbacks."""
    assert accessible_message(_callback(None)) is None


def test_real_message_passes_through():
    real = MagicMock(spec=Message)
    cb = _callback(real)

    assert accessible_message(cb) is real


def test_handler_test_doubles_pass_through():
    """The guard tests for *inaccessible*, not for "exactly a Message".

    Handler tests build their callbacks out of plain MagicMocks; an
    `isinstance(message, Message)` check would silently turn every one of them
    into an early return, and the handler bodies would stop being exercised at
    all while the suite still went green.
    """
    double = MagicMock()

    assert accessible_message(_callback(double)) is double
