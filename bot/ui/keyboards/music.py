"""Music (Lidarr/Deezer artist lookup) keyboards."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.models import AlbumInfo, ArtistInfo
from bot.ui.callbacks import (
    AlbumGrabCB,
    AlbumPageCB,
    AlbumScopeCB,
    AlbumSourceCB,
    ArtistCB,
)
from bot.ui.keyboards._constants import CallbackData


class _MusicKeyboards:
    """Artist lookup / details keyboard mixin."""

    @staticmethod
    def artist_list(
        artists: list[ArtistInfo],
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
                    callback_data=ArtistCB(idx=idx).pack(),
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
    def artist_details(artist: ArtistInfo, already_in_library: bool = False) -> InlineKeyboardMarkup:
        """Create keyboard for artist details (add/search)."""
        keyboard = []
        if already_in_library:
            keyboard.append([
                InlineKeyboardButton(text="🔍 Запустить поиск", callback_data=CallbackData.CONFIRM_GRAB),
            ])
        else:
            keyboard.append([
                InlineKeyboardButton(text="➕ Добавить и искать", callback_data=CallbackData.CONFIRM_GRAB),
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

        ordered = sorted(albums, key=lambda a: a.release_date or "", reverse=True)
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

        rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL)])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def album_sources(album_id: int) -> InlineKeyboardMarkup:
        """Where to look for this album: torrents via Lidarr, or Soulseek."""
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
                InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.MUSIC_BACK),
                InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
            ],
        ])

    @staticmethod
    def album_releases(releases: list, album_id: int, per_page: int = 5) -> InlineKeyboardMarkup:
        """Pick one torrent release of an album (see `AlbumGrabCB`).

        📚 на кнопке, а не только в тексте: тапают по кнопке, и раздача-
        дискография весит десятки гигабайт вместо одного альбома.
        """
        rows = []
        for idx, release in enumerate(releases[:per_page]):
            pack = " 📚" if release.is_season_pack else ""
            quality = release.quality.resolution or release.quality.source or ""
            quality = f" · {quality}" if quality else ""
            label = f"{idx + 1}. {release.size_formatted}{quality}{pack}"
            rows.append([InlineKeyboardButton(
                text=label, callback_data=AlbumGrabCB(idx=idx, album_id=album_id).pack(),
            )])

        rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL)])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def slskd_results(results: list, per_page: int = 5) -> InlineKeyboardMarkup:
        """Keyboard for picking one Soulseek candidate (see `SlskdCB`)."""
        from bot.ui.callbacks import SlskdCB

        keyboard = []
        for idx, result in enumerate(results[:per_page]):
            fmt = result.dominant_format.upper() or "?"
            label = f"{idx + 1}. {fmt} · {result.track_count} трек. · {result.size_formatted}"
            if len(label) > 60:
                label = label[:57] + "..."
            keyboard.append([
                InlineKeyboardButton(text=label, callback_data=SlskdCB(idx=idx).pack())
            ])

        keyboard.append([
            InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
        ])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)
