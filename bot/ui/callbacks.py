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


# ---------------------------------------------------------------------------
# r5 follow-up: migration of the remaining data-carrying callback families
# (see analysis/r5/05-logic-issues.md "Карта миграции CallbackData"). Each
# class below replaces a manually-`split`/`removeprefix`-parsed string
# family; constant-only callbacks (back/cancel/noop/settings/etc.) are left
# as plain ``CallbackData`` string literals — typing buys nothing there.
# ---------------------------------------------------------------------------


class ReleaseCB(CallbackData, prefix="rel"):
    """Search-result release selection (was ``rel:N`` string prefix)."""

    idx: int


class ArtistCB(CallbackData, prefix="art"):
    """Artist selection from a Lidarr lookup list (was ``artist:N``).

    Distinct prefix from the (still string-based, out of scope) ``art_page:``
    pagination family — no collision risk.
    """

    idx: int


class AddContentCB(CallbackData, prefix="addc"):
    """Add a trending movie/series to Radarr/Sonarr (was ``add_movie:ID`` /
    ``add_series:ID``). ``kind`` distinguishes the two so one class replaces
    both string prefixes without risking a movie/series id collision.
    """

    kind: str  # "movie" | "series"
    tmdb_id: int


class SettingCB(CallbackData, prefix="set"):
    """Settings value picker (was ``set:rp:``/``rf:``/``sp:``/``sf:``/``lp:``/
    ``lm:``/``lf:``/``res:``/``ag:``). ``key`` replaces the old per-setting
    prefix; ``value`` stays a string since resolution ("1080p"/"any") isn't
    numeric while profile/folder ids and the auto-grab flag are.
    """

    key: str
    value: str


class SeasonPresetCB(CallbackData, prefix="ssn"):
    """Season-monitoring preset pick on a series release card (was
    ``season_set:preset``).
    """

    preset: str


class TrendingItemCB(CallbackData, prefix="tri"):
    """Open a trending item's details (was ``trend_m:ID`` / ``trend_s:ID`` /
    ``trend_a:IDX``). ``kind`` keeps movie/series/artist ids from colliding
    the way the three previously-independent string prefixes never could.
    """

    kind: str  # "movie" | "series" | "artist"
    item_id: str


class TorrentActionCB(CallbackData, prefix="ta"):
    """qBittorrent per-torrent action (was ``t:``/``t_pause:``/``t_resume:``/
    ``t_delete:``/``t_delf:``/``t_delfc:`` + hash). ``action`` replaces the
    prefix; ``h`` carries the full 40-hex hash — worst case
    ``ta:delfc:<40 hex>`` packs to 49 bytes, comfortably under the 64-byte
    callback_data limit (PERF-05).
    """

    action: str  # "view" | "pause" | "resume" | "delete" | "delf" | "delfc"
    h: str


class CalCB(CallbackData, prefix="cal"):
    """Calendar period switch (was ``cal_7``/``cal_14``/``cal_30``)."""

    days: int
