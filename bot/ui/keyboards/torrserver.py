"""Inline keyboards for the TorrServer section."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.models import TorrServerRelease, TorrServerTorrent
from bot.ui.callbacks import TsAddCB, TsPageCB, TsReleaseCB, TsTorrentCB
from bot.ui.formatters.torrserver import TS_LIST_BUTTON_CAP
from bot.ui.keyboards._constants import CallbackData


class _TorrServerKeyboards:
    """TorrServer section keyboards mixin."""

    @staticmethod
    def torrserver_panel() -> InlineKeyboardMarkup:
        """Main panel under the status card."""
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔎 Найти", callback_data=CallbackData.TS_SEARCH),
                InlineKeyboardButton(text="📋 Раздачи", callback_data=CallbackData.TS_LIST),
            ],
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data=CallbackData.TS_REFRESH),
                InlineKeyboardButton(text="✖️ Закрыть", callback_data=CallbackData.TS_CLOSE),
            ],
        ])

    @staticmethod
    def torrserver_results(
        releases: list[TorrServerRelease], page: int, total_pages: int, offset: int = 0
    ) -> InlineKeyboardMarkup:
        """One button per hit on this page, plus pagination.

        `offset` is the absolute position of the first item on this page: the
        handler resolves a pick against the whole cached result list, so a
        page-relative index would open the wrong release on page two.
        """
        builder = InlineKeyboardBuilder()
        for position, release in enumerate(releases):
            idx = offset + position
            builder.row(InlineKeyboardButton(
                text=f"{idx + 1}. {release.title[:40]}",
                callback_data=TsReleaseCB(idx=idx).pack(),
            ))

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                text="⬅️", callback_data=TsPageCB(page=page - 1).pack()))
        if total_pages > 1:
            nav.append(InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(
                text="➡️", callback_data=TsPageCB(page=page + 1).pack()))
        if nav:
            builder.row(*nav)

        builder.row(InlineKeyboardButton(text="⬅️ В меню", callback_data=CallbackData.TS_BACK))
        return builder.as_markup()

    @staticmethod
    def torrserver_release(idx: int) -> InlineKeyboardMarkup:
        """Card of one hit: watch it, or go back to the list.

        The watch button is its own callback family (`TsAddCB`) rather than a
        suffixed `TsReleaseCB` — appending to a packed payload would break
        aiogram's unpacking.
        """
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Смотреть", callback_data=TsAddCB(idx=idx).pack())],
            [InlineKeyboardButton(text="⬅️ К результатам", callback_data=TsPageCB(page=0).pack())],
        ])

    @staticmethod
    def torrserver_list(
        torrents: list[TorrServerTorrent], is_admin: bool
    ) -> InlineKeyboardMarkup:
        """Torrents on the server; deletion is admin-only.

        Capped at ``TS_LIST_BUTTON_CAP`` buttons — Telegram rejects an
        oversized keyboard the same way it rejects an oversized message,
        which is what ``format_torrserver_torrents`` warns about in the text
        when the list actually gets cut.
        """
        builder = InlineKeyboardBuilder()
        if is_admin:
            for torrent in torrents[:TS_LIST_BUTTON_CAP]:
                builder.row(InlineKeyboardButton(
                    text=f"🗑 {torrent.title[:35]}",
                    callback_data=TsTorrentCB(action="del", h=torrent.hash).pack(),
                ))
        builder.row(InlineKeyboardButton(text="⬅️ В меню", callback_data=CallbackData.TS_BACK))
        return builder.as_markup()

    @staticmethod
    def torrserver_confirm_delete(torrent_hash: str) -> InlineKeyboardMarkup:
        """Deletion is destructive — ask first, same as the qBittorrent flow."""
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Удалить",
                    callback_data=TsTorrentCB(action="delconf", h=torrent_hash).pack()),
                InlineKeyboardButton(text="⬅️ Отмена", callback_data=CallbackData.TS_LIST),
            ],
        ])
