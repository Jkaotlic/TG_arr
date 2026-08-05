"""Message formatters for the TorrServer section (HTML parse mode)."""

import html

from bot.models import (
    TorrServerRelease,
    TorrServerStats,
    TorrServerTorrent,
    format_bytes,
)
from bot.ui.formatters._common import _safe_truncate

#: Shown whenever a release will reach Emby by the scheduled pass rather than
#: by our forced sync — the user should know they are not stuck.
_SCHEDULED_FALLBACK = "В Emby попадёт штатной задачей в течение 10 минут."

#: Matches Keyboards.torrserver_list's own cap on delete buttons — a torrent
#: list long enough to need one needs the other, so the two stay in lockstep
#: via this shared constant rather than two independently-tuned magic numbers.
TS_LIST_BUTTON_CAP = 30


class _TorrServerFormatters:
    """TorrServer section formatters mixin."""

    @staticmethod
    def format_torrserver_status(stats: TorrServerStats) -> str:
        cache_mode = "диск" if stats.use_disk else "RAM"
        return (
            "▶️ <b>TorrServer</b>\n\n"
            f"Версия: <code>{html.escape(stats.version)}</code>\n"
            f"Раздач: <b>{stats.torrent_count}</b> ({format_bytes(stats.total_size)})\n"
            f"Кеш: {format_bytes(stats.cache_size)} в {cache_mode}\n"
            f"Источников поиска: {stats.source_count}"
        )

    @staticmethod
    def format_torrserver_results(
        releases: list[TorrServerRelease], page: int, per_page: int, total: int
    ) -> str:
        start = page * per_page
        lines = [f"🔎 <b>Найдено раздач: {total}</b>", ""]
        for idx, release in enumerate(releases, start=start + 1):
            tracker = f" · {html.escape(release.tracker)}" if release.tracker else ""
            lines.append(
                f"<b>{idx}.</b> {html.escape(release.title[:90])}\n"
                f"    {format_bytes(release.size)} · 🌱 {release.seeders}{tracker}"
            )
        return "\n".join(lines)

    @staticmethod
    def format_torrserver_release(release: TorrServerRelease) -> str:
        year = f" ({release.year})" if release.year else ""
        tracker = html.escape(release.tracker) if release.tracker else "неизвестен"
        return (
            f"🎬 <b>{html.escape(release.title)}</b>{year}\n\n"
            f"Размер: {format_bytes(release.size)}\n"
            f"Сиды: {release.seeders} · Пиры: {release.peers}\n"
            f"Трекер: {tracker}\n\n"
            "Раздача не скачивается на диск — TorrServer отдаёт её потоком."
        )

    @staticmethod
    def format_torrserver_torrents(
        torrents: list[TorrServerTorrent], is_admin: bool = False
    ) -> str:
        if not torrents:
            return "📋 <b>Раздачи TorrServer</b>\n\nСписок пуст."
        lines = [f"📋 <b>Раздачи TorrServer: {len(torrents)}</b>", ""]
        for torrent in torrents:
            lines.append(
                f"• <b>{html.escape(torrent.title[:70])}</b>\n"
                f"    {format_bytes(torrent.size)} · файлов: {len(torrent.files)} · "
                f"{html.escape(torrent.stat_string)}"
            )
        text = "\n".join(lines)

        # Delete buttons are capped in the keyboard (Telegram rejects both an
        # oversized message and an oversized keyboard) — an admin scrolling
        # past the cap without a button needs to know why, not just find one
        # missing. Built separately from `text` and appended *after*
        # truncation (with its own length reserved out of the budget first):
        # a long enough list is exactly the case where both the button cap
        # and the truncation kick in together, and `_safe_truncate` cuts from
        # the tail — appending the note before truncating would make it the
        # first thing thrown away on precisely the lists where it matters.
        note = ""
        if is_admin and len(torrents) > TS_LIST_BUTTON_CAP:
            note = (
                f"\n\n⚠️ Кнопки удаления показаны только для первых "
                f"{TS_LIST_BUTTON_CAP} раздач."
            )

        # BUG-11/TEST-07-style hard safety net: a long enough list otherwise
        # blows past Telegram's 4096-char message limit outright.
        return _safe_truncate(text, max_len=3800 - len(note)) + note

    @staticmethod
    def format_torrserver_added(result) -> str:
        """Answer after an add. `result` is services.torrserver_service.AddResult
        (imported lazily by the caller to keep formatters free of service deps).
        """
        torrent = result.torrent
        lines = [f"✅ <b>{html.escape(torrent.title)}</b>", ""]

        if not result.metadata_ready:
            lines.append("Раздача добавлена, файлы ещё подтягиваются.")
            lines.append(_SCHEDULED_FALLBACK)
            return "\n".join(lines)

        videos = torrent.video_files
        lines.append(f"Файлов: {len(torrent.files)} · видео: {len(videos)}")
        for file in videos[:10]:
            name = file.path.rsplit("/", 1)[-1]
            lines.append(f"  🎞 {html.escape(name)} — {format_bytes(file.length)}")
        if len(videos) > 10:
            lines.append(f"  … и ещё {len(videos) - 10}")

        if result.stream_url:
            lines.append("")
            lines.append(f"▶️ <a href=\"{html.escape(result.stream_url)}\">Открыть поток</a>")

        lines.append("")
        status = getattr(result.sync, "status", None)
        if status == "ok":
            lines.append("📺 Опубликовано в Emby — ищи в «Торренты (фильмы/сериалы)».")
        elif status == "already_running":
            lines.append("📺 Синхронизация с Emby уже идёт — раздача попадёт в неё.")
        else:
            reason = getattr(result.sync, "error", None)
            if reason:
                lines.append(f"⚠️ Синхронизация не запустилась: {html.escape(reason)}.")
            else:
                lines.append("⚠️ Хук синхронизации не настроен.")
            lines.append(_SCHEDULED_FALLBACK)

        return "\n".join(lines)
