"""Typed CallbackData factories (feature #1).

aiogram 3's CallbackData factory gives pack()/unpack()/filter() with a required
prefix, so inline-button routing is type-safe and different callback families
cannot collide by string prefix (the root cause of BUG-01 and LOGIC-14).

This module starts the migration with the pagination family — the one that
actually collided (search `page:` vs music `art_page:`). A single ``PageCB``
with an explicit ``scope`` field keeps the two structurally distinct. Other
callback families can be migrated the same way incrementally.
"""

from aiogram.filters.callback_data import CallbackData


class PageCB(CallbackData, prefix="pg"):
    """Pagination callback. ``scope`` distinguishes e.g. 'search' vs 'artist'."""

    scope: str
    page: int


class TorrentPageCB(CallbackData, prefix="tpg"):
    """Torrent-list pagination callback (LOGIC-01).

    Carries the active filter (a ``TorrentFilter`` value, field name ``flt``
    — plain ``filter`` shadows ``CallbackData.filter()`` and triggers a
    pydantic ``UserWarning`` on class creation) alongside the page number so
    pagination/refresh/back round-trips no longer silently drop the user's
    filter selection — the previous plain ``t_page:N`` string callback had
    nowhere to carry that state.
    """

    page: int
    flt: str
