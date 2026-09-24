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
from pydantic import Field, field_validator


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
    """Search-result release selection (was ``rel:N`` string prefix).

    ``tok`` — the list token (``bot/ui/list_token.py``, audit 2026-09-23
    GRAB-03): a button from an older list must not resolve ``idx`` in the
    current one. A legacy ``rel:N`` does not unpack and lands in the
    "устарел" handler.
    """

    tok: str
    idx: int = Field(ge=0)


class GrabBestCB(CallbackData, prefix="gb"):
    """"⚡ Лучший" on a results list (was the plain ``grab_best`` string)."""

    tok: str


class GrabCB(CallbackData, prefix="gr"):
    """"✅ Скачать" on a release card (was the shared ``confirm_grab`` string).

    Carries the release index, so the card grabs the release it shows — not
    whatever ``selected_result`` points at now. ``ok=1`` is the explicit
    "download anyway" confirmation for a release *arr rejected (Task B3).
    """

    tok: str
    idx: int = Field(ge=0)
    ok: int = 0


class ForceGrabCB(CallbackData, prefix="gq"):
    """"⚡ Принудительно (qBit)" on a release card (was ``force_grab``)."""

    tok: str
    idx: int = Field(ge=0)


class AutoSearchCB(CallbackData, prefix="as"):
    """"🔍 Пусть *arr ищет сам" after a grab that never reached *arr's queue
    (audit 2026-09-23 GRAB-08: this used to happen silently)."""

    tok: str
    idx: int = Field(ge=0)


class MusicAddCB(CallbackData, prefix="ma"):
    """"🔍 Искать" on an artist card (was the shared ``confirm_grab`` string,
    which a movie session would have routed to a movie grab)."""

    tok: str


class ArtistCB(CallbackData, prefix="art"):
    """Artist selection from a Lidarr lookup list (was ``artist:N``).

    Distinct prefix from the (still string-based, out of scope) ``art_page:``
    pagination family — no collision risk.
    """

    idx: int


class AddContentCB(CallbackData, prefix="addc"):
    """Add a trending movie/series to the library (was ``add_movie:ID`` /
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


class SlskdCB(CallbackData, prefix="sk"):
    """Pick one Soulseek candidate from a `/album` or `/track` result list.

    Only the index is carried: the candidate itself (remote username + the
    whole file list) is far too large for callback_data's 64-byte budget, so
    the handler resolves it from the per-user in-memory list. ``tok`` ties the
    button to that list (``music._slskd_tokens``).
    """

    tok: str
    idx: int = Field(ge=0)


class TitleCB(CallbackData, prefix="ti"):
    """Pick which metadata candidate the user meant (2026-07-29).

    Only the index is carried: the candidates live in the user's session, and
    the metadata id alone wouldn't tell the handler which facet to add under.
    """

    idx: int


class TitleActionCB(CallbackData, prefix="tm"):
    """Manage a catalog title: monitoring toggle and removal (2026-07-29).

    `action`: pick (chose among several matches) | mon | unmon |
    delete (asks to confirm) | delconf (does it).

    Rollback 2026-08-10: `kind` ("movie"/"series") is its own field, not
    folded into `title_id` as e.g. "movie:15" — aiogram's CallbackData uses
    ':' as its OWN field separator, so a colon-joined value can never
    survive a pack()/unpack() round trip (`ValueError: separator symbol
    ':' can not be used in value`; `RootFolderCB` hit the same wall with
    Windows paths). Radarr and Sonarr have independent id spaces, so `kind`
    is what tells `handle_title_action` which *arr client `title_id`
    belongs to.
    """

    action: str
    kind: str
    title_id: str


class SeasonScopeCB(CallbackData, prefix="ss"):
    """Which season to search for (0 = the whole series). 2026-07-29."""

    season: int
    title_id: str


class SeasonPageCB(CallbackData, prefix="ssp"):
    """Page of the season picker (audit UI-17: only the 12 newest seasons
    used to be reachable). ``title_id`` ties it to one title, like
    ``SeasonScopeCB``."""

    page: int = Field(ge=0)
    title_id: str


class TsReleaseCB(CallbackData, prefix="tsr"):
    """Pick one TorrServer search hit. Only the index travels: the release
    itself (title + Prowlarr link) blows past callback_data's 64-byte budget,
    so the handler resolves it from the per-user result cache.

    ``ge=0``: a hand-crafted negative index would otherwise bypass the
    stale-cache guard (``idx >= len(releases)``) and resolve via Python's
    negative indexing to the wrong cached item.
    """

    tok: str
    idx: int = Field(ge=0)


class TsPageCB(CallbackData, prefix="tsp"):
    """Pagination inside a TorrServer result list."""

    page: int


class TsTorrentCB(CallbackData, prefix="tst"):
    """Per-torrent action on the server. ``action``: del (asks) | delconf (does it).
    ``h`` carries the hash, bounded to 40 hex chars — "tst:delconf:<40 hex>"
    packs to 53 bytes, under the 64-byte limit."""

    action: str
    h: str

    @field_validator("h", mode="after")
    @classmethod
    def _bound_hash_length(cls, v: str) -> str:
        """BitTorrent v2 infohashes are 64 hex chars — long enough to blow
        past callback_data's 64-byte budget outright (confirmed:
        ``TsTorrentCB(action='delconf', h='a'*64).pack()`` raises
        ``ValueError: Resulted callback data is too long!``, which would kill
        the whole "📋 Раздачи" screen for an admin). 40 hex is the v1
        infohash length TorrServer returns in practice, so capping here
        degrades a pathological v2-only hash to a truncated match instead of
        crashing the screen outright.
        """
        return v[:40]


class TsAddCB(CallbackData, prefix="tsa"):
    """Add the selected hit to TorrServer and publish it to Emby.

    Separate from ``TsReleaseCB`` (which only opens the card) so the two
    actions can never be confused by a shared prefix. Same ``ge=0`` guard as
    ``TsReleaseCB.idx``.
    """

    tok: str
    idx: int = Field(ge=0)


class FindPageCB(CallbackData, prefix="fp"):
    """Free-search pagination (2026-08-12).

    Deliberately separate from ``PageCB(scope="search")``: the free flow keeps
    a different session shape (no library entry, no *arr verdict) and its
    handlers must not route into the catalogue flow's, which would walk
    straight into a missing arr_id.
    """

    page: int


class FindReleaseCB(CallbackData, prefix="fr"):
    """Open one free-search hit — index into ``SearchSession.results``."""

    tok: str
    idx: int = Field(ge=0)


class FindGrabCB(CallbackData, prefix="fg"):
    """Grab one free-search hit.

    Carries the index rather than relying on ``selected_result``, so a stale
    card left open in an old message cannot grab whatever the session happens
    to point at now. ``tok`` does the same across searches. ``q=1`` is the
    explicit "download directly via qBittorrent" choice (audit GRAB-08: no
    longer a silent fallback after *arr refuses the push).
    """

    tok: str
    idx: int = Field(ge=0)
    q: int = 0


class AlbumScopeCB(CallbackData, prefix="al"):
    """Which album to search for; ``album_id=0`` = the whole discography.

    Ноль — полноценный ответ, а не «не выбрано». Живой дефект 2026-08-12
    (`analysis/2026-08-12-season-loop-and-packs.md`): у сезонов `season or None`
    превращал 0 в None, то есть в «пользователя ещё не спрашивали», и выбор
    возвращал сам себя бесконечно. Здесь ``album_id`` передаётся как есть, а
    обработчик сравнивает именно с нулём, а не проверяет на ложность.
    """

    album_id: int
    artist_id: int


class AlbumSourceCB(CallbackData, prefix="alsrc"):
    """Where to look for the chosen album.

    ``source``: "tor" — торренты через Lidarr (его собственный вердикт по
    профилю качества), "sk" — Soulseek через slskd. Живой замер 2026-08-12: по
    альбому «The Mindsweep» индексеры дали ОДНУ раздачу, поэтому второй
    источник — не украшение.
    """

    album_id: int
    source: str  # "tor" | "sk"


class AlbumPageCB(CallbackData, prefix="alpg"):
    """Pagination inside a discography picker.

    Своё семейство, а не ``PageCB(scope=...)``: у пикера альбомов свой источник
    данных (per-user кэш дискографии), и он не должен попадать в обработчик
    каталожной пагинации.
    """

    page: int
    artist_id: int


class AlbumGrabCB(CallbackData, prefix="alg"):
    """Grab one torrent release of a chosen album.

    ``album_id`` едет в колбэке, а не берётся из сессии: устаревшая карточка в
    старом сообщении не должна грабить тот альбом, на который сессия показывает
    сейчас (тот же довод, что у ``FindGrabCB``). ``ge=0`` — та же защита, что у
    ``TsReleaseCB``: отрицательный индекс обошёл бы проверку устаревшего кэша и
    разрешился бы через питоновскую отрицательную индексацию в чужой элемент.
    """

    tok: str
    idx: int = Field(ge=0)
    album_id: int
    # Audit 2026-09-23 UI-03/GRAB-02: the grab button sits on a release card
    # now; ``ok=1`` is "⚠️ Всё равно скачать" for a release Lidarr rejects.
    # A B1-era ``alg:tok:idx:album`` (no ``ok``) no longer unpacks → stale.
    ok: int = Field(default=0, ge=0, le=1)


class AlbumReleaseCB(CallbackData, prefix="alr"):
    """Open the card of one album torrent release (audit UI-03): a row tap
    used to grab straight away."""

    tok: str
    idx: int = Field(ge=0)
    album_id: int


class AlbumRelPageCB(CallbackData, prefix="alrp"):
    """Page through an album's torrent releases (audit UI-10: "Стр. 1/26"
    had no buttons). ``tok`` — the same list; a page of a newer list is
    another list."""

    tok: str
    page: int = Field(ge=0)
    album_id: int


class DiscographyCB(CallbackData, prefix="dsc"):
    """"✅ Да, вся дискография" on the confirm screen (audit UI-11): the
    whole-discography grab used to run on the first tap."""

    tok: str
    artist_id: int


class SlskdCardCB(CallbackData, prefix="skc"):
    """Open the card of one Soulseek candidate before enqueueing it
    (audit UI-03)."""

    tok: str
    idx: int = Field(ge=0)


class SlskdPageCB(CallbackData, prefix="skp"):
    """Page through Soulseek candidates (audit UI-10)."""

    tok: str
    page: int = Field(ge=0)


class AlbumRefreshCB(CallbackData, prefix="alrf"):
    """Re-ask Lidarr for an artist's discography.

    Живой пробник 2026-08-12: сразу после `add_artist` дискография пуста —
    Lidarr тянет её фоновой командой RefreshArtist, и это занимает больше, чем
    отпущено окном колбэка Telegram (~15 с). Кнопка превращает «пока не знаю» в
    «спроси ещё раз» вместо «начни поиск заново».
    """

    artist_id: int
