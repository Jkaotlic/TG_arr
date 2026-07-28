"""Service for adding content to Scryer and queueing downloads.

Migration 2026-07-28. The old flow was "push a release URL at Radarr/Sonarr,
fall back to qBittorrent when the profile rejected it". Scryer inverts that:
a release search is scoped to a *title*, and each candidate comes back with a
short-lived `candidateToken` that is redeemed via `queueExistingTitleDownload`.

So the flow is now:

    ensure_title()   → title exists in the catalog (added unmonitored if new)
    search_releases()→ candidates, each with a token and Scryer's verdict
    grab_release()   → redeem the token, then start monitoring the title

`add_and_queue_best()` is the one-shot variant: Scryer picks the release itself
using its profile and rules. qBittorrent remains only as the "force download
anyway" escape hatch for a release Scryer's profile blocks.
"""

import asyncio
import ipaddress
import re
import socket
import urllib.parse
from typing import Optional

import structlog

from bot.clients.scryer import ScryerClient, ScryerGraphQLError, mask_release_secrets
from bot.config import get_settings
from bot.clients.qbittorrent import QBittorrentClient
from bot.models import (
    ActionLog,
    ActionType,
    ArtistInfo,
    ContentType,
    MetadataProfile,
    QualityProfile,
    RootFolder,
    SearchResult,
)

logger = structlog.get_logger()

_ALLOWED_SCHEMES = {"http", "https", "magnet"}

# SEC-04/SEC-03: parameters in indexer download URLs commonly contain private
# trackers' credentials. `link`/`file`/`r`/`rss` are how Prowlarr's own
# download proxy embeds the ORIGINAL tracker URL (which itself carries a
# passkey/apikey) as a nested, url-encoded query value — masking only
# `apikey` leaves that nested secret in the clear.
_SENSITIVE_QUERY_PARAMS = {
    "apikey", "api_key", "token", "passkey", "auth", "authkey",
    "link", "file", "r", "rss",
}

# SEC-03: many private trackers embed the passkey directly as a path segment
# instead of (or in addition to) a query param, e.g.
# https://tracker/download/123/<32-char-hex-passkey>/name.torrent. Any long
# hex/base64-ish path segment is treated as a credential and masked.
_SECRET_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{16,}$")

# qBittorrent categories used by the force-download escape hatch. They mirror
# the folder layout Scryer imports from, so a forced grab still lands where the
# library expects it.
_QBIT_CATEGORIES = {
    ContentType.MOVIE: "radarr",
    ContentType.SERIES: "tv-sonarr",
    ContentType.ANIME: "anime",
}


def _mask_path(path: str) -> str:
    """Mask path segments that look like a passkey/token (long hex/base64-ish)."""
    segments = path.split("/")
    masked = [
        "***" if _SECRET_PATH_SEGMENT_RE.match(seg) else seg
        for seg in segments
    ]
    return "/".join(masked)


def _mask_url(url: str, max_len: int = 100) -> str:
    """Return a safe representation of a download URL for logs (strips secrets)."""
    if not url:
        return ""
    if url.startswith("magnet:"):
        return url[:max_len]
    parsed = urllib.parse.urlparse(url)
    masked_path = _mask_path(parsed.path)
    if not parsed.query:
        base = f"{parsed.scheme}://{parsed.netloc}{masked_path}"
        return base[:max_len]
    parts = []
    for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if k.lower() in _SENSITIVE_QUERY_PARAMS:
            parts.append(f"{k}=***")
        else:
            parts.append(f"{k}={v}")
    base = f"{parsed.scheme}://{parsed.netloc}{masked_path}?{'&'.join(parts)}"
    return base[:max_len]


def _log_grab_completed(
    log,
    *,
    success: bool,
    path: str,
    force_download: bool,
    content_type: ContentType,
    detail: Optional[str] = None,
) -> None:
    """OBS-05: single terminal INFO event for every grab outcome.

    `path` values: queue | qbit | auto | blocked | failed.
    """
    log.info(
        "grab_completed",
        success=success,
        path=path,
        force_download=force_download,
        content_type=content_type.value,
        detail=detail,
    )


def _is_internal_ip(addr: ipaddress._BaseAddress) -> bool:
    """Classify any non-public IP (private/loopback/link-local/reserved/multicast)."""
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


_DEFAULT_SCHEME_PORTS = {"http": 80, "https": 443}


def _trusted_service_hosts() -> set[tuple[str, int]]:
    """(hostname, port) pairs of the user's OWN configured services.

    A self-hosted single-household stack runs everything on a private LAN, and
    Scryer hands back download URLs that point at Prowlarr's download proxy —
    so a grab URL legitimately targets a private IP. Trust download URLs aimed
    at a configured service host; other internal addresses stay blocked.

    SEC-01: the pair MUST include the port. Trusting a hostname alone would
    trust ANY port on that host — in a typical stack Scryer/qBit/Emby share one
    LAN IP on different ports, so hostname-only trust degrades to "trust every
    port on this IP".
    """
    s = get_settings()
    hosts: set[tuple[str, int]] = set()
    for url in (
        s.scryer_url,
        s.lidarr_url,
        s.slskd_url,
        s.navidrome_url,
        s.qbittorrent_url,
        s.emby_url,
    ):
        if url:
            parsed = urllib.parse.urlparse(url)
            host = parsed.hostname
            if host:
                port = parsed.port or _DEFAULT_SCHEME_PORTS.get(parsed.scheme, 0)
                hosts.add((host.lower(), port))
    return hosts


async def _validate_download_url(url: str) -> bool:
    """
    Validate URL is safe for download (not SSRF).

    Async to avoid blocking the event loop on DNS (SEC-11) and to inspect every
    A/AAAA record returned by getaddrinfo so a hostname with both public and
    private addresses is rejected (SEC-01).

    Exception: a URL pointing at one of the user's OWN configured services is
    trusted even on a private LAN.

    SEC-08: accepted risk — this is a check-then-use validation (TOCTOU). We
    resolve the hostname here, but the actual download happens later inside
    qBittorrent, which performs its OWN resolution. Closing this fully would
    require qBittorrent to accept a pre-resolved IP, which it doesn't support
    — out of scope for a self-hosted single-household deployment.
    """
    if not url:
        return False
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return False
    if parsed.scheme == "magnet":
        return url.startswith("magnet:?xt=urn:btih:")
    if not parsed.hostname:
        return False
    url_port = parsed.port or _DEFAULT_SCHEME_PORTS.get(parsed.scheme, 0)
    if (parsed.hostname.lower(), url_port) in _trusted_service_hosts():
        return True
    try:
        addr = ipaddress.ip_address(parsed.hostname)
        return not _is_internal_ip(addr)
    except ValueError:
        pass  # hostname, resolve below
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, None)
    except socket.gaierror:
        return False
    for family, _t, _p, _c, sockaddr in infos:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        try:
            addr = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if _is_internal_ip(addr):
            return False
    return True


class AddService:
    """Adds titles to Scryer and turns release candidates into downloads."""

    def __init__(
        self,
        scryer: ScryerClient,
        qbittorrent: Optional[QBittorrentClient] = None,
        lidarr=None,
        slskd=None,
    ):
        self.scryer = scryer
        self.qbittorrent = qbittorrent
        self.lidarr = lidarr
        self.slskd = slskd

    # ------------------------------------------------------------- settings
    async def get_quality_profiles(self) -> list[QualityProfile]:
        """Quality profiles configured in Scryer."""
        return await self.scryer.get_quality_profiles()

    async def get_root_folders(self, content_type: ContentType = ContentType.MOVIE) -> list[RootFolder]:
        """Root folders for a facet."""
        return await self.scryer.get_root_folders(content_type)

    async def get_lidarr_profiles(self) -> list[QualityProfile]:
        """Get Lidarr quality profiles (empty list if Lidarr is not configured)."""
        if self.lidarr is None:
            return []
        return await self.lidarr.get_quality_profiles()

    async def get_lidarr_metadata_profiles(self) -> list[MetadataProfile]:
        """Get Lidarr metadata profiles (empty list if Lidarr is not configured)."""
        if self.lidarr is None:
            return []
        return await self.lidarr.get_metadata_profiles()

    async def get_lidarr_root_folders(self) -> list[RootFolder]:
        """Get Lidarr root folders (empty list if Lidarr is not configured)."""
        if self.lidarr is None:
            return []
        return await self.lidarr.get_root_folders()

    @staticmethod
    def resolve_profile(
        profiles: "list[QualityProfile] | list[MetadataProfile]",
        preferred_id,
    ):
        """LOGIC-11: resolve a quality/metadata profile from the user's
        preference, falling back to the first available one.

        Ids are compared as strings: Scryer profile ids are slugs ("4k"),
        Lidarr's are integers, and the stored preference may be either.
        """
        if preferred_id is not None and preferred_id != "":
            match = next((p for p in profiles if str(p.id) == str(preferred_id)), None)
            if match is not None:
                return match
        return profiles[0]

    @staticmethod
    def resolve_root_folder(folders: list[RootFolder], preferred_id) -> str:
        """LOGIC-11: resolve a root folder PATH from the user's preference or
        the first available folder (preferring the one Scryer marks default).

        Raises ValueError if no folders are available — callers already guard
        with an `if not folders` check, so this is a defensive backstop.
        """
        if not folders:
            raise ValueError("Нет доступных папок для сохранения")
        if preferred_id is not None and preferred_id != "":
            folder = next((f for f in folders if str(f.id) == str(preferred_id)), None)
            if folder is not None:
                return folder.path
        default = next((f for f in folders if f.is_default), None)
        return (default or folders[0]).path

    # ----------------------------------------------------------------- add
    async def ensure_title(
        self,
        content,
        content_type: ContentType,
        *,
        monitored: bool = False,
        quality_profile_id: Optional[str] = None,
        root_folder_path: Optional[str] = None,
    ) -> tuple[object, bool]:
        """Make sure a title exists in Scryer's catalog. Returns (title, created).

        Scryer can only search releases for a title it knows, so a release list
        requires the title to be added first. It is added **unmonitored** by
        default: merely browsing releases must not enrol the title into
        automatic acquisition. `grab_release` flips monitoring on once the user
        actually downloads something.
        """
        existing_id = getattr(content, "scryer_id", None)
        if existing_id:
            return content, False

        found = await self.scryer.find_title(content_type, content.title, getattr(content, "year", None))
        if found is not None:
            return found, False

        outcome = await self.scryer.add_title(
            content,
            content_type,
            monitored=monitored,
            quality_profile_id=quality_profile_id,
            root_folder_path=root_folder_path,
        )
        logger.info(
            "scryer_title_added",
            title=content.title,
            content_type=content_type.value,
            monitored=monitored,
            reused=outcome.reused_existing,
        )
        return outcome.title, not outcome.reused_existing

    async def add_and_queue_best(
        self,
        content,
        content_type: ContentType,
        *,
        quality_profile_id: Optional[str] = None,
        root_folder_path: Optional[str] = None,
        monitor_type: Optional[str] = None,
    ) -> tuple[bool, ActionLog, str]:
        """Add a title and let Scryer choose + queue the best allowed release.

        This is the "Скачать лучшее" path: the profile and the Rego rules make
        the choice, which is exactly what they are configured for.
        """
        action = ActionLog(
            user_id=0,
            action_type=ActionType.ADD,
            content_type=content_type,
            content_title=content.title,
            content_id=str(getattr(content, "metadata_id", None) or getattr(content, "scryer_id", "") or ""),
        )
        log = logger.bind(title=content.title, content_type=content_type.value)

        try:
            outcome = await self.scryer.add_title_and_queue_download(
                content,
                content_type,
                monitored=True,
                quality_profile_id=quality_profile_id,
                root_folder_path=root_folder_path,
                monitor_type=monitor_type,
                timeout=get_settings().scryer_search_timeout,
            )
        except ScryerGraphQLError as e:
            action.success = False
            action.error_message = str(e)
            _log_grab_completed(
                log, success=False, path="failed", force_download=False,
                content_type=content_type, detail=str(e)[:200],
            )
            return False, action, f"Scryer отклонил запрос: {e.message[:200]}"
        except Exception as e:
            log.error("add_and_queue_failed", error=str(e), exc_info=True)
            action.success = False
            action.error_message = str(e)
            return False, action, "Не удалось добавить в Scryer"

        action.success = True
        if outcome.queued:
            _log_grab_completed(
                log, success=True, path="auto", force_download=False, content_type=content_type,
            )
            return True, action, "Добавлено, Scryer подбирает и качает лучший релиз"

        # Added, but nothing matched the profile yet — Scryer will keep looking
        # via its own wanted/RSS cycle, so this is a success, not a failure.
        _log_grab_completed(
            log, success=True, path="auto", force_download=False,
            content_type=content_type, detail="queued_nothing",
        )
        return True, action, "Добавлено в библиотеку. Подходящий релиз пока не найден — Scryer продолжит искать"

    # ---------------------------------------------------------------- grab
    async def grab_release(
        self,
        title,
        release: SearchResult,
        content_type: ContentType,
        *,
        force_download: bool = False,
    ) -> tuple[bool, ActionLog, str]:
        """Queue one specific release candidate for an existing title.

        With `force_download=True` the candidate bypasses Scryer entirely and
        goes straight to qBittorrent — the escape hatch for a release the
        profile blocks but the user wants anyway.
        """
        title_id = getattr(title, "scryer_id", None) or release.scryer_title_id
        action = ActionLog(
            user_id=0,  # set by the caller before logging
            action_type=ActionType.GRAB,
            content_type=content_type,
            content_title=getattr(title, "title", None),
            content_id=str(title_id or ""),
            release_title=release.title,
        )
        log = logger.bind(
            title=getattr(title, "title", None),
            title_id=title_id,
            release_title=release.title[:80],
            indexer=release.indexer,
        )

        if force_download:
            return await self._force_download(log, action, release, content_type)

        if not title_id:
            action.success = False
            action.error_message = "no title id"
            _log_grab_completed(
                log, success=False, path="failed", force_download=False, content_type=content_type,
            )
            return False, action, "Не удалось определить тайтл в Scryer — повторите поиск"

        if not release.candidate_token:
            # Candidate tokens are short-lived; a session restored from an old
            # message (or from before the migration) has none.
            action.success = False
            action.error_message = "no candidate token"
            _log_grab_completed(
                log, success=False, path="failed", force_download=False,
                content_type=content_type, detail="missing_candidate_token",
            )
            return False, action, "Ссылка на релиз устарела — повторите поиск"

        try:
            result = await self.scryer.queue_existing_title_download(
                title_id=title_id,
                candidate_token=release.candidate_token,
                scope=release.queue_scope,
            )
        except ScryerGraphQLError as e:
            action.success = False
            action.error_message = str(e)
            _log_grab_completed(
                log, success=False, path="failed", force_download=False,
                content_type=content_type, detail=e.message[:200],
            )
            return False, action, f"Scryer отклонил релиз: {e.message[:200]}"
        except Exception as e:
            log.error("queue_download_failed", error=str(e), exc_info=True)
            action.success = False
            action.error_message = str(e)
            _log_grab_completed(
                log, success=False, path="failed", force_download=False, content_type=content_type,
            )
            return False, action, "Ошибка постановки в очередь"

        if not result.queued:
            action.success = False
            action.error_message = f"queue status: {result.status}"
            _log_grab_completed(
                log, success=False, path="blocked", force_download=False,
                content_type=content_type, detail=result.status,
            )
            if result.status == "CONFLICT":
                return False, action, "Этот тайтл уже качается — дождитесь окончания или отмените текущую загрузку"
            return False, action, f"Scryer не принял релиз ({result.status})"

        # Only now enrol the title into monitoring: the user committed to it.
        try:
            await self.scryer.set_title_monitored(title_id, True)
        except Exception as e:
            # Non-fatal: the download is already queued.
            log.warning("set_monitored_failed", error=str(e))

        action.success = True
        _log_grab_completed(
            log, success=True, path="queue", force_download=False, content_type=content_type,
        )
        return True, action, "Релиз поставлен в очередь на скачивание"

    async def _force_download(
        self,
        log,
        action: ActionLog,
        release: SearchResult,
        content_type: ContentType,
    ) -> tuple[bool, ActionLog, str]:
        """Bypass Scryer and push the torrent straight into qBittorrent."""
        if self.qbittorrent is None:
            action.success = False
            action.error_message = "qBittorrent not configured"
            return False, action, "qBittorrent не настроен"

        download_url = release.download_url or release.magnet_url
        if not download_url:
            action.success = False
            action.error_message = "no download url"
            return False, action, "У релиза нет ссылки для скачивания"

        if not await _validate_download_url(download_url):
            log.warning(
                "force_download_blocked_unsafe_url",
                download_url=_mask_url(download_url),
            )
            action.success = False
            action.error_message = "unsafe download url"
            return False, action, "Небезопасный URL для скачивания"

        try:
            ok = await self.qbittorrent.add_torrent_url(
                download_url,
                category=_QBIT_CATEGORIES.get(content_type, "radarr"),
            )
        except Exception as e:
            log.error("qbittorrent_force_download_failed", error=str(e), exc_info=True)
            action.success = False
            action.error_message = str(e)
            _log_grab_completed(
                log, success=False, path="failed", force_download=True, content_type=content_type,
            )
            return False, action, "Ошибка загрузки через qBittorrent"

        if not ok:
            log.error("qbittorrent_rejected_torrent", download_url=_mask_url(download_url))
            action.success = False
            action.error_message = "qBittorrent rejected the torrent"
            _log_grab_completed(
                log, success=False, path="failed", force_download=True, content_type=content_type,
            )
            return False, action, "qBittorrent отклонил торрент"

        action.success = True
        _log_grab_completed(
            log, success=True, path="qbit", force_download=True, content_type=content_type,
        )
        logger.debug("force_download_ok", url=mask_release_secrets(download_url))
        return True, action, "Загружено напрямую через qBittorrent (в обход правил Scryer)"

    # --------------------------------------------------------------- music
    async def add_artist(
        self,
        artist: ArtistInfo,
        quality_profile_id: int,
        metadata_profile_id: int,
        root_folder_path: str,
        monitor: str = "all",
        search_for_missing: bool = True,
        tags: Optional[list[int]] = None,
    ) -> tuple[Optional[ArtistInfo], ActionLog]:
        """Add an artist to Lidarr."""
        log = logger.bind(name=artist.name, mb_id=artist.mb_id)
        log.info("Adding artist to Lidarr")

        action = ActionLog(
            user_id=0,
            action_type=ActionType.ADD,
            content_type=ContentType.MUSIC,
            content_title=artist.name,
            content_id=artist.mb_id,
        )

        if self.lidarr is None:
            action.success = False
            action.error_message = "Lidarr не настроен"
            return None, action

        try:
            existing = await self.lidarr.get_artist_by_mbid(artist.mb_id)
            if existing and existing.lidarr_id:
                log.info("Artist already exists", lidarr_id=existing.lidarr_id)
                action.success = True
                return existing, action

            added = await self.lidarr.add_artist(
                artist=artist,
                quality_profile_id=quality_profile_id,
                metadata_profile_id=metadata_profile_id,
                root_folder_path=root_folder_path,
                monitor=monitor,
                search_for_missing=search_for_missing,
                tags=tags,
            )

            action.success = True
            log.info("Artist added successfully", lidarr_id=added.lidarr_id)
            return added, action

        except Exception as e:
            log.error("Failed to add artist", error=str(e), exc_info=True)
            action.success = False
            action.error_message = str(e)
            return None, action
