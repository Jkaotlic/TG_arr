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


def _format_episode_range(episodes: object) -> str:
    """LOGIC-18b: Sonarr season-pack imports report ALL episodes in one
    webhook payload; only showing ``episodes[0]`` made a 10-episode season
    pack look like a single episode. Collapse a contiguous/multi-episode
    list into "S01E01-E10"; a single episode stays "S01E02".
    """
    if not isinstance(episodes, list) or not episodes:
        return ""
    nums = []
    season = None
    for ep in episodes:
        if not isinstance(ep, dict):
            continue
        s = ep.get("seasonNumber")
        e = ep.get("episodeNumber")
        if s is None or e is None:
            continue
        try:
            s, e = int(s), int(e)
        except (TypeError, ValueError):
            continue
        if season is None:
            season = s
        if s == season:
            nums.append(e)
    if not nums:
        return ""
    nums.sort()
    if len(nums) == 1:
        return f" S{season:02d}E{nums[0]:02d}"
    return f" S{season:02d}E{nums[0]:02d}-E{nums[-1]:02d}"


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
        ep_str = _format_episode_range(payload.get("episodes"))
        return f"📺 <b>{html.escape(str(series['title']))}</b>{ep_str} — готово, в библиотеке."

    artist = payload.get("artist")
    if isinstance(artist, dict) and artist.get("name"):
        return f"🎵 <b>{html.escape(str(artist['name']))}</b> — готово, в библиотеке."

    return None


def _token_matches(request: web.Request, token: str) -> bool:
    """SEC-02/BUG-08: accept either `?token=` query param or a `/webhook/<token>`
    path segment. Documented matching rule (also in tests/test_feat_webhook.py):
    once a token is configured, `/webhook/{service}` no longer authenticates
    unless `{service}` happens to equal the token — operators who want a
    service label AND auth should use `?token=` instead.
    """
    if request.query.get("token") == token:
        return True
    service = request.match_info.get("service")
    return service is not None and service == token


def build_webhook_app(
    notify: Callable[[str], Awaitable[None]],
    token: Optional[str] = None,
) -> web.Application:
    """Build the aiohttp app; ``notify`` is called with the message on each import.

    SEC-02/BUG-08: when ``token`` is set, requests must present it via
    `?token=<token>` or `/webhook/<token>` — anything else gets 403. When
    ``token`` is None (not configured), requests are accepted unauthenticated
    (a startup warning is emitted separately by Settings' model_validator).
    """

    async def handle(request: web.Request) -> web.Response:
        route_segment = request.match_info.get("service")
        # A configured token may be supplied as the route segment. Never use
        # that segment as a log field: Docker/Portainer logs are not a secret
        # store. Service labels remain available only in unauthenticated mode.
        auth_mode = "none"
        if token:
            auth_mode = "query" if request.query.get("token") == token else "path"
        service = route_segment if token is None else None

        if token and not _token_matches(request, token):
            logger.warning("webhook_rejected_bad_token", remote=request.remote, auth_mode=auth_mode)
            return web.Response(status=403, text="forbidden")

        try:
            payload = await request.json()
        except Exception:
            logger.warning("webhook_invalid_json", remote=request.remote)
            return web.Response(status=400, text="invalid json")

        event_type = payload.get("eventType") if isinstance(payload, dict) else None
        message = parse_arr_event(payload)
        logger.info(
            "webhook_received",
            event_type=event_type,
            service=service,
            auth_mode=auth_mode,
            matched=message is not None,
        )
        if message:
            try:
                await notify(message)
                logger.info("webhook_notified", service=service, auth_mode=auth_mode)
            except Exception as e:  # never let a notify error 500 the *arr side
                logger.warning("webhook_notify_failed", error=str(e))
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_post("/webhook", handle)
    app.router.add_post("/webhook/{service}", handle)  # /webhook/radarr or /webhook/<token>
    return app


async def start_webhook_server(app: web.Application, host: str, port: int) -> web.AppRunner:
    """Start the webhook server; returns the runner (call .cleanup() on shutdown)."""
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("webhook_server_started", host=host, port=port)
    return runner
