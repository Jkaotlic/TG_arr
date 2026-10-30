"""qBittorrent / torrent-list / torrent-action formatters."""

from bot.models import QBittorrentStatus, TorrentFilter, TorrentInfo
from bot.ui.formatters._common import _e, _progress_bar, _to_local


class _TorrentFormatters:
    """qBittorrent and torrent formatting mixin."""

    @staticmethod
    def format_qbittorrent_status(status: QBittorrentStatus) -> str:
        """Format qBittorrent global status."""
        lines = ["<b>📊 Статус qBittorrent</b>\n"]

        # Version and connection
        lines.append(f"🖥 <b>Версия:</b> {_e(status.version)}")
        conn_emoji = "🟢" if status.connection_status == "connected" else "🔴"
        conn_text = (
            "подключён"
            if status.connection_status == "connected"
            else _e(status.connection_status)
        )
        lines.append(f"{conn_emoji} <b>Соединение:</b> {conn_text}")

        lines.append("")

        # Transfer speeds
        lines.append("<b>📡 Скорость:</b>")
        lines.append(f"  ⬇️ Загрузка: {status.download_speed_formatted}")
        lines.append(f"  ⬆️ Отдача: {status.upload_speed_formatted}")

        # Limits
        if status.download_limit > 0 or status.upload_limit > 0:
            from bot.models import format_speed

            dl_limit = (
                format_speed(status.download_limit)
                if status.download_limit > 0
                else "∞"
            )
            ul_limit = (
                format_speed(status.upload_limit)
                if status.upload_limit > 0
                else "∞"
            )
            lines.append(f"  📉 Лимиты: ⬇️ {dl_limit} | ⬆️ {ul_limit}")

        lines.append("")

        # Torrents
        lines.append("<b>📋 Торренты:</b>")
        lines.append(f"  Всего: {status.total_torrents}")
        lines.append(
            f"  Активных: ⬇️ {status.active_downloads} | ⬆️ {status.active_uploads}"
        )
        if status.paused_torrents > 0:
            lines.append(f"  На паузе: {status.paused_torrents}")

        lines.append("")

        # Disk
        lines.append(f"💾 <b>Свободно:</b> {status.free_space_formatted}")

        # DHT
        if status.dht_nodes > 0:
            lines.append(f"🌐 <b>DHT узлов:</b> {status.dht_nodes}")

        return "\n".join(lines)

    @staticmethod
    def format_torrent_list(
        torrents: list[TorrentInfo],
        page: int,
        total_pages: int,
        current_filter: TorrentFilter,
        total_count: int,
        status: QBittorrentStatus | None = None,
    ) -> str:
        """Format torrent list header.

        ``status`` — qBittorrent's summary (speeds, active downloads, free
        space), shown under the title since "📊 qBit" left the main menu
        (2026-09-24).
        """
        filter_names = {
            TorrentFilter.ALL: "Все",
            TorrentFilter.DOWNLOADING: "Загружаются",
            TorrentFilter.SEEDING: "Раздаются",
            TorrentFilter.COMPLETED: "Завершены",
            TorrentFilter.PAUSED: "На паузе",
            TorrentFilter.ACTIVE: "Активные",
            TorrentFilter.INACTIVE: "Неактивные",
            TorrentFilter.STALLED: "Застряли",
            TorrentFilter.ERRORED: "С ошибками",
        }

        filter_name = filter_names.get(current_filter, "Все")
        header = f"<b>📥 Загрузки</b> — {filter_name}\n"
        header += f"Показано {len(torrents)} из {total_count}"

        if total_pages > 1:
            header += f" (стр. {page + 1}/{total_pages})"

        if status is not None:
            summary = _TorrentFormatters.format_qbit_summary(status)
            header = header.replace("\n", f"\n{summary}\n", 1)

        return header

    @staticmethod
    def format_qbit_summary(status: QBittorrentStatus) -> str:
        """One line: qBit speeds, active downloads, free space."""
        summary = (
            f"⬇️ {status.download_speed_formatted} · ⬆️ {status.upload_speed_formatted}"
            f" · активных: {status.active_downloads}"
        )
        if status.free_space > 0:
            summary += f" · свободно: {status.free_space_formatted}"
        return summary

    @staticmethod
    def format_torrent_details(torrent: TorrentInfo) -> str:
        """Format detailed view of a torrent."""
        lines = [f"<b>{_e(torrent.name)}</b>\n"]

        # State and progress
        state_names = {
            "downloading": "Загрузка",
            "seeding": "Раздача",
            "completed": "Завершён",
            "paused": "Пауза",
            "queued": "В очереди",
            "checking": "Проверка",
            "stalled": "Застрял",
            "error": "Ошибка",
            "moving": "Перемещение",
            "unknown": "Неизвестно",
        }
        state_text = state_names.get(torrent.state.value, torrent.state.value)
        lines.append(f"{torrent.state_emoji} <b>Статус:</b> {state_text}")
        lines.append(f"📊 <b>Прогресс:</b> {torrent.progress_percent}%")

        # Progress bar
        progress_bar = _progress_bar(torrent.progress)
        lines.append(f"<code>{progress_bar}</code>")

        lines.append("")

        # Size info
        from bot.models import format_bytes

        downloaded = format_bytes(torrent.downloaded)
        lines.append(f"💾 <b>Размер:</b> {downloaded} / {torrent.size_formatted}")

        # Speeds
        if torrent.download_speed > 0 or torrent.upload_speed > 0:
            lines.append(f"⬇️ <b>Загрузка:</b> {torrent.download_speed_formatted}")
            lines.append(f"⬆️ <b>Отдача:</b> {torrent.upload_speed_formatted}")

        # ETA
        if torrent.eta is not None and torrent.eta > 0 and torrent.progress < 1.0:
            lines.append(f"⏱ <b>Осталось:</b> {torrent.eta_formatted}")

        lines.append("")

        # Peers
        lines.append("<b>🌐 Пиры:</b>")
        lines.append(f"  Сиды: {torrent.seeds} (всего {torrent.seeds_total})")
        lines.append(f"  Личи: {torrent.peers} (всего {torrent.peers_total})")

        # Ratio
        lines.append(f"\n📈 <b>Рейтинг:</b> {torrent.ratio:.2f}")

        # Category and tags
        if torrent.category:
            lines.append(f"📁 <b>Категория:</b> {_e(torrent.category)}")
        if torrent.tags:
            lines.append(f"🏷 <b>Теги:</b> {_e(', '.join(torrent.tags))}")

        # Save path
        lines.append(f"\n📂 <b>Путь:</b> <code>{_e(torrent.save_path)}</code>")

        # Dates (BUG-06: local timezone)
        if torrent.added_on:
            lines.append(
                f"📅 <b>Добавлен:</b> {_to_local(torrent.added_on).strftime('%d.%m.%Y %H:%M')}"
            )
        if torrent.completion_on and torrent.progress >= 1.0:
            lines.append(
                f"✅ <b>Завершён:</b> {_to_local(torrent.completion_on).strftime('%d.%m.%Y %H:%M')}"
            )

        return "\n".join(lines)

    @staticmethod
    def format_download_complete_notification(torrent: TorrentInfo) -> str:
        """Format notification message for completed download."""
        lines = ["✅ <b>Загрузка завершена!</b>\n"]
        lines.append(f"📥 <b>{_e(torrent.name)}</b>")
        lines.append(f"💾 Размер: {torrent.size_formatted}")
        lines.append(f"📂 Путь: <code>{_e(torrent.save_path)}</code>")

        if torrent.completion_on:
            lines.append(
                f"⏱ Завершено: {_to_local(torrent.completion_on).strftime('%d.%m.%Y %H:%M')}"
            )

        return "\n".join(lines)

    @staticmethod
    def format_speed_limit_changed(limit_type: str, speed_kb: int) -> str:
        """Format message for speed limit change."""
        if speed_kb == 0:
            speed_str = "без ограничений"
        else:
            from bot.models import format_speed

            speed_str = format_speed(speed_kb * 1024)

        direction = "Загрузка" if limit_type == "dl" else "Отдача"
        return f"✅ {direction}: {speed_str}"

    @staticmethod
    def format_speed_screen(dl_speed: int, ul_speed: int, alt_dl: int, alt_ul: int) -> str:
        """The speed screen (round 2 / D1): current transfer + turtle limits.

        All values are bytes/s; a turtle limit of 0 means "no limit". Russian
        units, like the preset grid (fix round 1).
        """
        return (
            "<b>🚀 Скорость</b>\n"
            f"Сейчас: ⬇️ {format_speed_ru(dl_speed)} · ⬆️ {format_speed_ru(ul_speed)}\n"
            f"🐢 Черепаха: ⬇️ {format_limit_ru(alt_dl)} · ⬆️ {format_limit_ru(alt_ul)}"
        )


def format_speed_ru(bytes_per_sec: int | float) -> str:
    """Speed in Russian units — "512 КБ/с", "2 МБ/с", "2.5 МБ/с" (the preset
    grid's spelling). Zero is "0 КБ/с"."""
    value = float(bytes_per_sec)
    if value <= 0:
        return "0 КБ/с"
    for unit in ("Б/с", "КБ/с", "МБ/с"):
        if value < 1024:
            return f"{_trim(value)} {unit}"
        value /= 1024
    return f"{_trim(value)} ГБ/с"


def format_limit_ru(bytes_per_sec: int) -> str:
    """A speed LIMIT: 0 means none."""
    return "∞ без лимита" if bytes_per_sec <= 0 else format_speed_ru(bytes_per_sec)


def _trim(value: float) -> str:
    """One decimal at most, no trailing ".0"."""
    return f"{value:.1f}".rstrip("0").rstrip(".")
