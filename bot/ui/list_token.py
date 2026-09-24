"""List tokens: a stale button never acts on a different item.

Audit 2026-09-23 GRAB-03 / UI-01 / UI-02. List buttons used to carry only an
index into "the user's current list". After a second search that list is a
different one, so a button left in an OLD message resolved the same index in
the NEW list and grabbed a release the user never saw.

The fix: every time a list is rendered from a fresh result set, a short random
token is issued and stored next to that result set (in ``SearchSession.
list_token`` for session-backed lists, or in a :class:`ListTokenRegistry` for
lists kept in handler-module memory). Each button's callback data carries the
token; the handler compares it with the stored one and, on a mismatch,
answers :data:`STALE_LIST_TEXT` and does nothing.

API
---
- :func:`new_list_token` — 6 base36 chars (≈2·10⁹ values), cheap in the
  64-byte callback_data budget.
- :func:`token_matches(expected, got)` — False for an empty/missing expected
  token, so sessions saved by an older bot version (no token) read as stale.
- :func:`answer_stale(callback)` — the one user-facing answer: alert, stops
  the spinner, no action.
- :class:`ListTokenRegistry` — per-user token store for in-memory lists
  (Soulseek candidates, TorrServer hits): ``issue(user_id)``,
  ``check(user_id, tok)``, ``current(user_id)``, ``drop(user_id)``.

Pagination re-renders the SAME result set, so it re-uses the stored token; only
a new result set issues a new one.
"""

from __future__ import annotations

import secrets
from typing import Any

#: User-facing answer for any button whose list is gone or replaced.
STALE_LIST_TEXT = "⌛ Список устарел — повторите поиск"

TOKEN_LENGTH = 6
_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def new_list_token() -> str:
    """A fresh 6-char base36 token."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(TOKEN_LENGTH))


def token_matches(expected: str | None, got: str | None) -> bool:
    """True only when a token was issued and the button carries that same one."""
    return bool(expected) and expected == got


async def answer_stale(callback: Any) -> None:
    """Answer a stale/legacy button: an alert, and no action."""
    await callback.answer(STALE_LIST_TEXT, show_alert=True)


class ListTokenRegistry:
    """Per-user current token for a list that lives in module memory.

    Bounded like the caches it guards (oldest user evicted first, never a
    ``clear()`` — the contract of ``bot/handlers/_cache.py``). After a bot restart the
    registry is empty, so every old button reads as stale, which matches the
    in-memory list itself being gone.
    """

    def __init__(self, max_users: int = 100) -> None:
        self._tokens: dict[int, str] = {}
        self._max_users = max_users

    def issue(self, user_id: int) -> str:
        """Issue (and remember) a new token for this user's freshly built list."""
        token = new_list_token()
        # Same contract as bot/handlers/_cache.remember_lru (not imported: the
        # bot.handlers package imports every handler, which import this module).
        self._tokens.pop(user_id, None)
        while len(self._tokens) >= self._max_users:
            self._tokens.pop(next(iter(self._tokens)))
        self._tokens[user_id] = token
        return token

    def current(self, user_id: int) -> str:
        """The token of the user's current list, or "" when there is none."""
        return self._tokens.get(user_id, "")

    def check(self, user_id: int, token: str | None) -> bool:
        """Whether a button's token belongs to the user's current list."""
        return token_matches(self._tokens.get(user_id), token)

    def drop(self, user_id: int) -> None:
        """Forget the user's list (e.g. after the action it existed for)."""
        self._tokens.pop(user_id, None)
