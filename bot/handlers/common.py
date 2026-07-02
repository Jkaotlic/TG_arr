"""Shared helpers used across multiple handler modules.

LOGIC-13/LOGIC-15: previously ``search.py`` had a safe (but private)
``_strip_command`` helper, while ``downloads.py``/``music.py`` stripped
commands with naive ``str.replace("/cmd", "")`` calls that don't account for
an ``@botname`` suffix (e.g. ``/pause@mybot all`` left ``"@mybot all"`` as the
args, which downloads.py then tried to look up as a torrent hash). Likewise,
the "swallow only 'message is not modified'" try/except around
``message.edit_text`` was copy-pasted verbatim in ~10 call sites across
downloads/emby/music/calendar/search. Both are centralized here.
"""

import structlog
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

logger = structlog.get_logger()


def strip_command(text: str, command: str) -> str:
    """Strip a leading ``/command`` (and an optional ``@botname`` suffix on it).

    BUG-10/33: uses a prefix check + maxsplit=1 rather than a blind
    ``str.replace`` so the command token is only removed once, from the
    front, and an ``@botname`` suffix (e.g. ``/pause@mybot all``) doesn't leak
    into the returned args.
    """
    text = text.strip()
    if text.startswith(command):
        rest = text[len(command):]
        if rest.startswith("@"):
            parts = rest.split(maxsplit=1)
            rest = parts[1] if len(parts) > 1 else ""
        return rest.strip()
    return text


async def swallow_not_modified(coro) -> bool:
    """Await ``coro``, swallowing the harmless "message is not modified"
    TelegramBadRequest Telegram raises when the new text/markup is identical
    to the current one (e.g. a fast double-tap on pagination/refresh).

    Any other ``TelegramBadRequest`` is re-raised. Returns True if the call
    completed, False if it was a no-op swallow.

    This is the shared core behind ``safe_edit`` (the common ``message.edit_text``
    case) and calendar.py's ``_fetch_and_send_calendar`` (which polymorphically
    calls either ``message.answer`` or ``message.edit_text`` via an injected
    ``answer_func``, so it cannot go through ``safe_edit`` directly).
    """
    try:
        await coro
        return True
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
        return False


async def safe_edit(message: Message, text: str, **kwargs) -> bool:
    """Edit ``message`` to ``text``, swallowing the harmless "message is not
    modified" TelegramBadRequest (see ``swallow_not_modified``).

    Returns True if the edit was applied, False if it was a no-op swallow.
    """
    return await swallow_not_modified(message.edit_text(text, **kwargs))
