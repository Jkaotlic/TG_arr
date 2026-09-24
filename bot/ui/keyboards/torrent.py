"""qBittorrent / downloads-domain keyboards: torrent list/details, filters,
speed-limit menu and delete confirmation.
"""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.models import TorrentFilter, TorrentInfo
from bot.ui.callbacks import TorrentActionCB, TorrentMoreCB, TorrentPageCB
from bot.ui.keyboards._constants import BTN_BACK, BTN_CLOSE, CallbackData


class _TorrentKeyboards:
    """qBittorrent / Download keyboard mixin."""

    @staticmethod
    def torrent_list(
        torrents: list[TorrentInfo],
        current_page: int = 0,
        total_pages: int = 1,
        current_filter: TorrentFilter = TorrentFilter.ALL,
        is_admin: bool = False,
    ) -> InlineKeyboardMarkup:
        """Create keyboard for torrent list with pagination.

        Round 2 / D2: the bottom is exactly Обновить / Фильтр / ⋯ Ещё (+ the
        pagination row). ``is_admin`` is kept for callers; the admin-only
        blanket actions (audit UI-16) now live in ``torrent_more``.

        LOGIC-01: pagination buttons carry ``current_filter`` via the typed
        ``TorrentPageCB`` so paging through a filtered list doesn't silently
        fall back to the unfiltered "all" view.
        """
        keyboard = []

        # Torrent buttons — torrents is already the page slice
        for torrent in torrents:
            # Format: emoji progress% name (speed)
            progress = f"{torrent.progress_percent}%"
            speed = ""
            if torrent.download_speed > 0:
                speed = f" ⬇{torrent.download_speed_formatted}"
            elif torrent.upload_speed > 0:
                speed = f" ⬆{torrent.upload_speed_formatted}"

            name = torrent.name[:25] + "..." if len(torrent.name) > 28 else torrent.name
            label = f"{torrent.state_emoji} {progress} {name}{speed}"
            if len(label) > 50:
                label = label[:47] + "..."

            keyboard.append([
                InlineKeyboardButton(
                    text=label,
                    # PERF-05: full 40-hex hash fits comfortably under the 64-byte
                    # callback_data limit (worst case "ta:delfc:" + 40 hex = 49
                    # bytes — see TorrentActionCB), so lookups can use the
                    # targeted get_torrent(hash) instead of scanning the whole
                    # list for a short-hash prefix match.
                    callback_data=TorrentActionCB(action="view", h=torrent.hash).pack(),
                )
            ])

        # Round 2 / D2: exactly Обновить / Фильтр / ⋯ Ещё; the blanket
        # actions, speed and Close moved into the "⋯ Ещё" panel.
        keyboard.append([
            InlineKeyboardButton(
                text="🔄 Обновить",
                callback_data=TorrentPageCB(page=current_page, flt=current_filter.value).pack(),
            ),
            InlineKeyboardButton(text="🔍 Фильтр", callback_data=f"{CallbackData.TORRENT_FILTER}menu"),
            InlineKeyboardButton(
                text="⋯ Ещё",
                callback_data=TorrentMoreCB(page=current_page, flt=current_filter.value).pack(),
            ),
        ])

        # Pagination row
        if total_pages > 1:
            nav_buttons = []
            if current_page > 0:
                nav_buttons.append(
                    InlineKeyboardButton(
                        text="◀️",
                        callback_data=TorrentPageCB(page=current_page - 1, flt=current_filter.value).pack(),
                    )
                )
            nav_buttons.append(
                InlineKeyboardButton(text=f"{current_page + 1}/{total_pages}", callback_data="noop")
            )
            if current_page < total_pages - 1:
                nav_buttons.append(
                    InlineKeyboardButton(
                        text="▶️",
                        callback_data=TorrentPageCB(page=current_page + 1, flt=current_filter.value).pack(),
                    )
                )
            keyboard.append(nav_buttons)

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def torrent_more(
        current_filter: TorrentFilter = TorrentFilter.ALL,
        page: int = 0,
        is_admin: bool = False,
    ) -> InlineKeyboardMarkup:
        """The "⋯ Ещё" panel of the torrent list (round 2 / D2).

        Audit UI-16/SEC-06: "pause/resume all" and speed are admin-only, so a
        non-admin sees only the navigation row. "◀️ Назад" redraws the list
        at the same filter/page.
        """
        keyboard: list[list[InlineKeyboardButton]] = []
        if is_admin:
            keyboard.append([
                InlineKeyboardButton(text="⏸ Пауза всех", callback_data=CallbackData.TORRENT_PAUSE_ALL),
                InlineKeyboardButton(text="▶️ Возобновить все", callback_data=CallbackData.TORRENT_RESUME_ALL),
            ])
            # Speed limits are global qBit settings — admin-only (final review 2026-09-24).
            keyboard.append([
                InlineKeyboardButton(text="🚀 Скорость", callback_data=CallbackData.SPEED_MENU),
            ])
        keyboard.append([
            InlineKeyboardButton(
                text=BTN_BACK,
                callback_data=TorrentPageCB(page=page, flt=current_filter.value).pack(),
            ),
            InlineKeyboardButton(text=BTN_CLOSE, callback_data=CallbackData.CLOSE),
        ])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def torrent_details(
        torrent: TorrentInfo,
        current_filter: TorrentFilter = TorrentFilter.ALL,
        page: int = 0,
        is_admin: bool = False,
    ) -> InlineKeyboardMarkup:
        """Create keyboard for torrent details view.

        Audit UI-16: delete buttons are admin-only and shown only to admins.

        LOGIC-01: ``current_filter`` (defaults to ALL when the caller doesn't
        know it — e.g. reached via a legacy ``t_page:`` callback) is threaded
        into the "back to list" button so returning from details doesn't
        silently drop the user's active filter.
        """
        keyboard = []
        # PERF-05: full hash — see torrent_list for the byte-budget rationale.
        full_hash = torrent.hash

        # Pause/Resume based on state
        from bot.models import TorrentState
        if torrent.state in (TorrentState.PAUSED, TorrentState.QUEUED):
            keyboard.append([
                InlineKeyboardButton(text="▶️ Возобновить", callback_data=TorrentActionCB(action="resume", h=full_hash).pack()),
            ])
        else:
            keyboard.append([
                InlineKeyboardButton(text="⏸ Пауза", callback_data=TorrentActionCB(action="pause", h=full_hash).pack()),
            ])

        # Delete options — both ask for confirmation first (audit UI-16).
        if is_admin:
            keyboard.append([
                InlineKeyboardButton(text="🗑 Удалить", callback_data=TorrentActionCB(action="delete", h=full_hash).pack()),
                InlineKeyboardButton(text="🗑 + Файлы", callback_data=TorrentActionCB(action="delf", h=full_hash).pack()),
            ])

        # Back button — carries the filter so the list redraw stays filtered.
        keyboard.append([
            InlineKeyboardButton(
                text=BTN_BACK,
                # Audit UI-09: the page, not only the filter, survives the card.
                callback_data=TorrentPageCB(page=page, flt=current_filter.value).pack(),
            ),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def torrent_filters(
        current_filter: TorrentFilter = TorrentFilter.ALL, page: int = 0,
    ) -> InlineKeyboardMarkup:
        """Create keyboard for selecting torrent filter.

        The current filter is marked with "•"; "◀️ Назад" returns to the list
        view the menu was opened from (audit UI-09).
        """
        filters = [
            (TorrentFilter.ALL, "📋 Все"),
            (TorrentFilter.DOWNLOADING, "⬇️ Загрузка"),
            (TorrentFilter.SEEDING, "⬆️ Раздача"),
            (TorrentFilter.COMPLETED, "✅ Готово"),
            (TorrentFilter.PAUSED, "⏸ Пауза"),
            (TorrentFilter.ACTIVE, "🔥 Активные"),
            (TorrentFilter.STALLED, "⚠️ Застряли"),
            (TorrentFilter.ERRORED, "❌ Ошибки"),
        ]

        keyboard = []
        row = []

        for filter_type, label in filters:
            # Mark current filter
            display_label = f"• {label}" if filter_type == current_filter else label
            row.append(
                InlineKeyboardButton(
                    text=display_label,
                    callback_data=f"{CallbackData.TORRENT_FILTER}{filter_type.value}",
                )
            )
            if len(row) == 2:
                keyboard.append(row)
                row = []

        if row:
            keyboard.append(row)

        keyboard.append([
            InlineKeyboardButton(
                text=BTN_BACK,
                callback_data=TorrentPageCB(page=page, flt=current_filter.value).pack(),
            ),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def _speed_preset_row(
        presets: list[tuple[int, str]],
        direction: str,
        current_limit: int,
    ) -> list[InlineKeyboardButton]:
        """Build one row of speed-limit preset buttons (LOGIC-03 helper).

        Collapses what used to be 4 near-identical copies of this loop (two
        rows each for download/upload) into a single implementation. The
        "✓ " marker is honest: it compares against the caller-supplied
        ``current_limit`` (bytes/s) instead of always defaulting to 0.
        """
        row = []
        for speed_kb, label in presets:
            marker = "✓ " if current_limit == speed_kb * 1024 else ""
            row.append(
                InlineKeyboardButton(
                    text=f"{marker}{label}",
                    callback_data=f"{CallbackData.SPEED_LIMIT}{direction}:{speed_kb}",
                )
            )
        return row

    @staticmethod
    def speed_screen(
        turtle_on: bool,
        current_filter: TorrentFilter = TorrentFilter.ALL,
        page: int = 0,
    ) -> InlineKeyboardMarkup:
        """The speed screen (round 2 / D1): one turtle toggle, the exact
        preset grid behind "⋯", and "◀️ Назад" to the list view it came from."""
        state = "вкл" if turtle_on else "выкл"
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🐢 Черепаха: {state}", callback_data=CallbackData.SPEED_TURTLE)],
            [InlineKeyboardButton(text="⋯ Точные лимиты", callback_data=CallbackData.SPEED_EXACT)],
            [InlineKeyboardButton(
                text=BTN_BACK,
                callback_data=TorrentPageCB(page=page, flt=current_filter.value).pack(),
            )],
        ])

    @staticmethod
    def speed_limits_menu(
        current_dl_limit: int = 0,
        current_ul_limit: int = 0,
    ) -> InlineKeyboardMarkup:
        """Create keyboard for speed limit presets ("⋯ Точные лимиты").

        LOGIC-03: callers must pass the qBittorrent-reported current limits
        (bytes/s) so the "✓" marker reflects reality — previously this was
        always called with the 0/0 defaults, which happened to always match
        the "unlimited" preset regardless of the real setting.

        Round 2 / D1: "◀️ Назад" returns to the speed screen, not the list.
        """
        # Presets in KB/s (0 = unlimited)
        presets = [
            (0, "∞ Без лимита"),
            (512, "512 КБ/с"),
            (1024, "1 МБ/с"),
            (2048, "2 МБ/с"),
            (5120, "5 МБ/с"),
            (10240, "10 МБ/с"),
        ]

        keyboard = [
            [InlineKeyboardButton(text="⬇️ Лимит загрузки:", callback_data="noop")],
            _TorrentKeyboards._speed_preset_row(presets[:3], "dl", current_dl_limit),
            _TorrentKeyboards._speed_preset_row(presets[3:], "dl", current_dl_limit),
            [InlineKeyboardButton(text="⬆️ Лимит отдачи:", callback_data="noop")],
            _TorrentKeyboards._speed_preset_row(presets[:3], "ul", current_ul_limit),
            _TorrentKeyboards._speed_preset_row(presets[3:], "ul", current_ul_limit),
            [InlineKeyboardButton(text=BTN_BACK, callback_data=CallbackData.SPEED_MENU)],
        ]

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def confirm_delete_torrent(torrent_hash: str, with_files: bool = False) -> InlineKeyboardMarkup:
        """Create confirmation keyboard for deleting a torrent.

        BUG-14/DEAD-03: previously generated ``t_delete:confirm:<hash16>`` /
        ``t_delf:confirm:<hash16>``, which the handlers would have parsed as
        a (nonexistent) short-hash "confirm" — this keyboard was unreachable
        AND broken. Now wired for the "delete with files" flow only (the
        irreversible one): confirm uses the dedicated ``t_delfc:`` prefix,
        cancel goes back to the torrent card via ``t:<hash>``. Plain delete
        (keep files, reversible) still fires immediately without this step.
        """
        full_hash = torrent_hash
        if with_files:
            confirm_callback = TorrentActionCB(action="delfc", h=full_hash).pack()
            text = "⚠️ Да, удалить с файлами"
        else:
            # Audit UI-16: "delc" does it; "delete" only asks (this screen).
            confirm_callback = TorrentActionCB(action="delc", h=full_hash).pack()
            text = "🗑 Да, удалить торрент"

        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text=text, callback_data=confirm_callback),
                ],
                [
                    InlineKeyboardButton(
                        text=BTN_BACK, callback_data=TorrentActionCB(action="view", h=full_hash).pack()
                    ),
                ],
            ]
        )
