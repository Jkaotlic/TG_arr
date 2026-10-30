"""Music (Lidarr/Deezer artist lookup) keyboards."""

from collections.abc import Sequence
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.models import AlbumInfo, ArtistInfo
from bot.ui.callbacks import (
    AlbumGrabCB,
    AlbumPageCB,
    AlbumRefreshCB,
    AlbumRelPageCB,
    AlbumReleaseCB,
    AlbumScopeCB,
    AlbumSourceCB,
    ArtistCB,
    DiscographyCB,
    MusicAddCB,
    MusicRetryCB,
    SlskdCardCB,
    SlskdCB,
    SlskdPageCB,
)
from bot.ui.keyboards._constants import CallbackData

if TYPE_CHECKING:
    from bot.services.music_retry import RetryOffer


class _MusicKeyboards:
    """Artist lookup / details keyboard mixin."""

    @staticmethod
    def artist_list(
        artists: list[ArtistInfo],
        list_token: str,
        current_page: int = 0,
        per_page: int = 5,
    ) -> InlineKeyboardMarkup:
        """Create keyboard for artist selection from lookup results."""
        total_pages = max(1, (len(artists) + per_page - 1) // per_page)
        start_idx = current_page * per_page
        page_artists = artists[start_idx:start_idx + per_page]

        keyboard = []
        for i, a in enumerate(page_artists):
            idx = start_idx + i
            disamb = f" [{a.disambiguation}]" if a.disambiguation else ""
            label = f"{a.name}{disamb}"
            if len(label) > 40:
                label = label[:37] + "..."
            keyboard.append([
                InlineKeyboardButton(
                    text=label,
                    callback_data=ArtistCB(tok=list_token, idx=idx).pack(),
                )
            ])

        if total_pages > 1:
            nav_buttons = []
            if current_page > 0:
                nav_buttons.append(
                    InlineKeyboardButton(text="◀️", callback_data=f"{CallbackData.ARTIST_PAGE}{current_page - 1}")
                )
            nav_buttons.append(
                InlineKeyboardButton(text=f"{current_page + 1}/{total_pages}", callback_data="noop")
            )
            if current_page < total_pages - 1:
                nav_buttons.append(
                    InlineKeyboardButton(text="▶️", callback_data=f"{CallbackData.ARTIST_PAGE}{current_page + 1}")
                )
            keyboard.append(nav_buttons)

        keyboard.append([
            InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
        ])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def artist_details(
        artist: ArtistInfo, already_in_library: bool = False, list_token: str = "",
    ) -> InlineKeyboardMarkup:
        """Create keyboard for artist details (add/search).

        Audit 2026-09-23: the button is ``MusicAddCB(tok)``, not the shared
        ``confirm_grab`` — a movie session used to route that to a movie grab.
        """
        keyboard = []
        if already_in_library:
            keyboard.append([
                InlineKeyboardButton(text="🔍 Искать", callback_data=MusicAddCB(tok=list_token).pack()),
            ])
        else:
            # Кнопка больше не «добавить всю дискографию»: она резолвит артиста
            # в Lidarr (немониторимым) и спрашивает, что именно искать.
            keyboard.append([
                InlineKeyboardButton(
                    text="🔍 Искать (добавит в Lidarr)",
                    callback_data=MusicAddCB(tok=list_token).pack(),
                ),
            ])
        keyboard.append([
            # LOGIC-24: dedicated music-back so search.handle_back doesn't reply
            # "сессия истекла" on a music session (which has no .results).
            InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.MUSIC_BACK),
            InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
        ])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def album_scope(
        albums: list[AlbumInfo],
        artist_id: int,
        current_page: int = 0,
        per_page: int = 5,
    ) -> InlineKeyboardMarkup:
        """Pick what to fetch for an artist: the whole discography or one album.

        Прямой аналог `season_scope` у сериалов. Порядок — новые сверху: обычно
        ищут свежий альбом. ✅ значит «треки уже на диске» — чтобы не качать
        второй раз, не выходя в веб-морду Lidarr.
        """
        rows = [[InlineKeyboardButton(
            text="💿 Вся дискография",
            callback_data=AlbumScopeCB(album_id=0, artist_id=artist_id).pack(),
        )]]

        ordered = _MusicKeyboards.album_order(albums)
        total_pages = max(1, (len(ordered) + per_page - 1) // per_page)
        start = current_page * per_page
        for album in ordered[start:start + per_page]:
            year = f" · {album.year}" if album.year else ""
            mark = " ✅" if album.has_files else ""
            label = f"{album.title}{year}{mark}"
            if len(label) > 55:
                label = label[:52] + "..."
            rows.append([InlineKeyboardButton(
                text=label,
                callback_data=AlbumScopeCB(album_id=album.lidarr_id, artist_id=artist_id).pack(),
            )])

        if total_pages > 1:
            nav = []
            if current_page > 0:
                nav.append(InlineKeyboardButton(
                    text="◀️",
                    callback_data=AlbumPageCB(page=current_page - 1, artist_id=artist_id).pack(),
                ))
            nav.append(InlineKeyboardButton(
                text=f"{current_page + 1}/{total_pages}", callback_data="noop",
            ))
            if current_page < total_pages - 1:
                nav.append(InlineKeyboardButton(
                    text="▶️",
                    callback_data=AlbumPageCB(page=current_page + 1, artist_id=artist_id).pack(),
                ))
            rows.append(nav)

        if not albums:
            # Дискографии ещё нет: Lidarr тянет её фоновым RefreshArtist уже
            # после ответа на add_artist (живой пробник 2026-08-12 — ноль
            # альбомов сразу после добавления). Без этой кнопки единственный
            # выход — начинать поиск заново.
            rows.append([InlineKeyboardButton(
                text="🔄 Обновить дискографию",
                callback_data=AlbumRefreshCB(artist_id=artist_id).pack(),
            )])

        rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL)])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def album_sources(album_id: int, artist_id: int = 0, page: int = 0) -> InlineKeyboardMarkup:
        """Where to look for this album: torrents via Lidarr, or Soulseek.

        "◀️ Назад" returns to the album list page the album was picked from
        (audit UI-08: it used to jump to the artist list, page 1).
        """
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🧲 Торренты",
                callback_data=AlbumSourceCB(album_id=album_id, source="tor").pack(),
            )],
            [InlineKeyboardButton(
                text="🎧 Soulseek",
                callback_data=AlbumSourceCB(album_id=album_id, source="sk").pack(),
            )],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AlbumPageCB(page=page, artist_id=artist_id).pack(),
                ),
                InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
            ],
        ])

    @staticmethod
    def album_order(albums: list[AlbumInfo]) -> list[AlbumInfo]:
        """The order ``album_scope`` lists albums in — newest first."""
        return sorted(albums, key=lambda a: a.release_date or "", reverse=True)

    @staticmethod
    def _page_nav(page: int, total_pages: int, make) -> list:
        """◀️ N/M ▶️ row; ``make(page)`` packs the page callback."""
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=make(page - 1)))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=make(page + 1)))
        return nav

    @staticmethod
    def album_releases(
        releases: list,
        album_id: int,
        page: int = 0,
        per_page: int = 5,
        list_token: str = "",
        artist_id: int = 0,
    ) -> InlineKeyboardMarkup:
        """One page of an album's torrent releases (whole list in, absolute
        indices on the buttons).

        Audit 2026-09-23: a row opens the release card (UI-03) — it used to
        grab on tap; pages have ◀️ N/M ▶️ (UI-10); "◀️ Назад" returns to the
        source choice ("Где искать?").

        📚 на кнопке, а не только в тексте: тапают по кнопке, и раздача-
        дискография весит десятки гигабайт вместо одного альбома.
        """
        total_pages = max(1, (len(releases) + per_page - 1) // per_page)
        start = page * per_page
        rows = []
        for idx, release in enumerate(releases[start:start + per_page], start=start):
            pack = " 📚" if release.is_season_pack else ""
            # Живой замер 2026-08-12: у музыки формат (FLAC/MP3-320/WAV) — это
            # главный различитель, и он лежит в `codec`; resolution/source —
            # видео-поля и для аудио пусты. ⛔ значит «Lidarr сам бы не взял»
            # (24 раздачи из 127 живьём) — взять всё равно можно, но осознанно.
            fmt = release.quality.codec or release.quality.source or ""
            fmt = f" · {fmt}" if fmt else ""
            verdict = " ⛔" if release.rejected else ""
            label = f"{idx + 1}. {release.size_formatted}{fmt}{pack}{verdict}"
            rows.append([InlineKeyboardButton(
                text=label,
                callback_data=AlbumReleaseCB(tok=list_token, idx=idx, album_id=album_id).pack(),
            )])

        if total_pages > 1:
            rows.append(_MusicKeyboards._page_nav(
                page, total_pages,
                lambda p: AlbumRelPageCB(tok=list_token, page=p, album_id=album_id).pack(),
            ))
        rows.append([
            InlineKeyboardButton(
                text="◀️ Назад",
                callback_data=AlbumScopeCB(album_id=album_id, artist_id=artist_id).pack(),
            ),
            InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
        ])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def album_release_card(
        release, idx: int, album_id: int, list_token: str, per_page: int = 5,
    ) -> InlineKeyboardMarkup:
        """Card of one album release. A release Lidarr rejects is taken only
        through "⚠️ Всё равно скачать" (audit GRAB-02 pattern, B1)."""
        if release.rejected:
            take = InlineKeyboardButton(
                text="⚠️ Всё равно скачать",
                callback_data=AlbumGrabCB(tok=list_token, idx=idx, album_id=album_id, ok=1).pack(),
            )
        else:
            take = InlineKeyboardButton(
                text="✅ Скачать",
                callback_data=AlbumGrabCB(tok=list_token, idx=idx, album_id=album_id).pack(),
            )
        return InlineKeyboardMarkup(inline_keyboard=[
            [take],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AlbumRelPageCB(
                        tok=list_token, page=idx // per_page, album_id=album_id,
                    ).pack(),
                ),
                InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
            ],
        ])

    @staticmethod
    def slskd_results(
        results: list, page: int = 0, per_page: int = 5, list_token: str = "",
    ) -> InlineKeyboardMarkup:
        """One page of Soulseek candidates; a row opens the candidate's card
        (audit UI-03/UI-10)."""
        total_pages = max(1, (len(results) + per_page - 1) // per_page)
        start = page * per_page
        keyboard = []
        for idx, result in enumerate(results[start:start + per_page], start=start):
            fmt = (result.effective_format or "?").upper()
            label = f"{idx + 1}. {fmt} · {result.track_count} трек. · {result.size_formatted}"
            if len(label) > 60:
                label = label[:57] + "..."
            keyboard.append([
                InlineKeyboardButton(text=label, callback_data=SlskdCardCB(tok=list_token, idx=idx).pack())
            ])
        if total_pages > 1:
            keyboard.append(_MusicKeyboards._page_nav(
                page, total_pages, lambda p: SlskdPageCB(tok=list_token, page=p).pack(),
            ))
        keyboard.append([
            InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
        ])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def slskd_card(idx: int, list_token: str, per_page: int = 5) -> InlineKeyboardMarkup:
        """Card of one Soulseek candidate: enqueue, or back to its page."""
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="✅ Скачать", callback_data=SlskdCB(tok=list_token, idx=idx).pack(),
            )],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=SlskdPageCB(tok=list_token, page=idx // per_page).pack(),
                ),
                InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
            ],
        ])

    @staticmethod
    def discography_confirm(artist_id: int, list_token: str, artist_fp: str = "") -> InlineKeyboardMarkup:
        """Audit UI-11: the whole discography is a big action — confirm it.

        ``artist_fp`` — see ``DiscographyCB.fp`` (round 2 / D3)."""
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="✅ Да, вся дискография",
                callback_data=DiscographyCB(tok=list_token, artist_id=artist_id, fp=artist_fp).pack(),
            )],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AlbumPageCB(page=0, artist_id=artist_id).pack(),
                ),
                InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
            ],
        ])

    @staticmethod
    def music_retry(offers: Sequence["RetryOffer"]) -> InlineKeyboardMarkup:
        """M1 (improvements round 2): one «🔁 Докачать: <album>» per album
        under a music summary (`bot.services.music_retry` caps them at 3)."""
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=o.label, callback_data=MusicRetryCB(tok=o.token).pack())]
            for o in offers
        ])
