"""Feature #8: inbound webhook server for Radarr/Sonarr/Lidarr "on import" events.

Config-gated (off by default). When enabled, a lightweight aiohttp server accepts
the *arr Connect → Webhook POSTs and pushes an instant "X is now in the library"
Telegram notification — more accurate and immediate than the 60s qBittorrent poll.

Set up in each *arr: Settings → Connect → Webhook →
    URL: http://<pi-host>:<WEBHOOK_PORT>/webhook
    Method: POST, triggers: On Import (On Download).
"""

import html
from collections.abc import Awaitable, Callable
from typing import Optional

import structlog
from aiohttp import web

logger = structlog.get_logger()

# *arr eventTypes that mean "media is now available in the library".
_IMPORT_EVENTS = {
    "Download",
    "DownloadFolderImported",
    "MovieFileImported",
    "EpisodeFileImported",
    "AlbumImported",
    "TrackFileImported",
}


def parse_arr_event(payload: Optional[dict]) -> Optional[str]:
    """Turn an *arr webhook payload into a user notification, or None to ignore.

    Handles Radarr (movie), Sonarr (series+episodes) and Lidarr (artist/album)
    import events, plus the "Test" event *arr sends when you click Test. All
    other events (Grab, Rename, Health, ...) are ignored.
    """
    if not isinstance(payload, dict):
        return None

    event = payload.get("eventType")
    if event == "Test":
        instance = payload.get("instanceName") or "*arr"
        return f"✅ Webhook подключён ({html.escape(str(instance))})"

    if event not in _IMPORT_EVENTS:
        return None

    movie = payload.get("movie")
    if isinstance(movie, dict) and movie.get("title"):
        year = movie.get("year")
        year_str = f" ({year})" if year else ""
        return f"🎬 <b>{html.escape(str(movie['title']))}</b>{year_str} — готово, в библиотеке."

    series = payload.get("series")
    if isinstance(series, dict) and series.get("title"):
        ep_str = ""
        episodes = payload.get("episodes")
        if isinstance(episodes, list) and episodes and isinstance(episodes[0], dict):
            season = episodes[0].get("seasonNumber")
            episode = episodes[0].get("episodeNumber")
            if season is not None and episode is not None:
                try:
                    ep_str = f" S{int(season):02d}E{int(episode):02d}"
                except (TypeError, ValueError):
                    ep_str = ""
        return f"📺 <b>{html.escape(str(series['title']))}</b>{ep_str} — готово, в библиотеке."

    artist = payload.get("artist")
    if isinstance(artist, dict) and artist.get("name"):
        return f"🎵 <b>{html.escape(str(artist['name']))}</b> — готово, в библиотеке."

    return None


def build_webhook_app(notify: Callable[[str], Awaitable[None]]) -> web.Application:
    """Build the aiohttp app; ``notify`` is called with the message on each import."""

    async def handle(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid json")
        message = parse_arr_event(payload)
        if message:
            try:
                await notify(message)
            except Exception as e:  # never let a notify error 500 the *arr side
                logger.warning("webhook_notify_failed", error=str(e))
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_post("/webhook", handle)
    app.router.add_post("/webhook/{service}", handle)  # /webhook/radarr etc.
    return app


async def start_webhook_server(app: web.Application, host: str, port: int) -> web.AppRunner:
    """Start the webhook server; returns the runner (call .cleanup() on shutdown)."""
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("webhook_server_started", host=host, port=port)
    return runner
