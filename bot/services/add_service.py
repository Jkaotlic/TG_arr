"""Service for adding content to Radarr/Sonarr."""

import asyncio
import ipaddress
import json
import re
import socket
import urllib.parse
from typing import Optional

import structlog

from bot.clients.base import APIError
from bot.config import get_settings
from bot.clients.lidarr import LidarrClient
from bot.clients.prowlarr import ProwlarrClient
from bot.clients.qbittorrent import QBittorrentClient, QBittorrentError
from bot.clients.radarr import RadarrClient
from bot.clients.sonarr import SonarrClient
from bot.models import (
    ActionLog,
    ActionType,
    ArtistInfo,
    ContentType,
    MetadataProfile,
    MovieInfo,
    QualityProfile,
    RootFolder,
    SearchResult,
    SeriesInfo,
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
    rejections: Optional[list] = None,
) -> None:
    """OBS-05: single terminal INFO event for every grab_*_release outcome.

    Successful paths and the two failure terminals ("rejected, no qBit
    fallback" / "could not grab or search") were previously logged with
    5+ different ad-hoc phrases, and two failure terminals had NO log at
    all — diagnosing "нажал Скачать — ничего не скачалось" required
    reconstructing the outcome from ActionLog in the DB. `path` values:
    push | qbit | auto_search | rejected | failed.
    """
    log.info(
        "grab_completed",
        success=success,
        path=path,
        force_download=force_download,
        content_type=content_type.value,
        rejections=rejections or [],
    )


def _safe_push_result(result: Optional[dict]) -> dict:
    """SEC-03: extract only the safe fields from an *arr push-release response.

    The raw response echoes the pushed release object including ``downloadUrl``,
    which for private trackers embeds a reusable passkey/apikey. Logging the raw
    dict leaks that credential to container logs, so we keep only the decision
    fields (``approved``/``rejections``) that we actually act on.
    """
    if not isinstance(result, dict):
        return {"approved": None, "rejections": []}
    return {
        "approved": result.get("approved"),
        "rejections": result.get("rejections", []),
    }


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

    A self-hosted single-household stack runs Prowlarr/*arr/qBit on a private
    LAN, and Prowlarr proxies every ``downloadUrl`` through itself — so the grab
    download URL legitimately points at a private IP. Trust download URLs aimed
    at a configured service host, otherwise the SSRF guard blocks every real
    grab. Other internal addresses stay blocked.

    SEC-01: the pair MUST include the port. Trusting hostname alone would
    trust ANY port on that host — in a typical stack, Prowlarr/*arr/qBit/Emby
    all share one LAN IP on different ports, so hostname-only trust degrades
    to "trust every port on this IP" (e.g. a malicious downloadUrl pointing at
    the same IP's :6379 Redis or :22 SSH would be waved through).
    """
    s = get_settings()
    hosts: set[tuple[str, int]] = set()
    for url in (
        s.prowlarr_url,
        s.radarr_url,
        s.sonarr_url,
        s.lidarr_url,
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

    Exception: a URL pointing at one of the user's OWN configured services
    (Prowlarr's download proxy etc.) is trusted even on a private LAN.

    SEC-08: accepted risk — this is a check-then-use validation (TOCTOU). We
    resolve the hostname here and decide trust/rejection based on THIS
    resolution, but the actual download happens later, inside *arr/qBittorrent,
    which perform their OWN independent DNS resolution. Between the two
    lookups a malicious/compromised DNS record could "rebind" from a public IP
    (passes this check) to a private one (used for the real request), or vice
    versa. Closing this fully would require *arr/qBittorrent to accept a
    pre-resolved IP instead of a hostname, which they don't support — out of
    scope for a self-hosted single-household deployment where the indexer set
    is curated by the admin. Documented here rather than fixed.
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
    # Trust the user's own configured services (Prowlarr proxies downloadUrls).
    # SEC-01: match on (host, port) — a matching hostname on an unexpected
    # port falls through to the normal IP check instead of being trusted.
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
    """Service for adding content and grabbing releases."""

    def __init__(
        self,
        prowlarr: Optional[ProwlarrClient],
        radarr: RadarrClient,
        sonarr: SonarrClient,
        qbittorrent: Optional[QBittorrentClient] = None,
        lidarr: Optional[LidarrClient] = None,
    ):
        # LOGIC-10c: `prowlarr` is accepted (existing call sites all pass it,
        # positionally or by keyword) but intentionally NOT stored — nothing
        # in this class ever reads self.prowlarr; AddService only grabs/adds
        # via radarr/sonarr/lidarr/qbittorrent. Kept as an optional parameter
        # so callers passing it (with or without a keyword) don't break.
        self.radarr = radarr
        self.sonarr = sonarr
        self.lidarr = lidarr
        self.qbittorrent = qbittorrent

    async def get_radarr_profiles(self) -> list[QualityProfile]:
        """Get Radarr quality profiles."""
        return await self.radarr.get_quality_profiles()

    async def get_radarr_root_folders(self) -> list[RootFolder]:
        """Get Radarr root folders."""
        return await self.radarr.get_root_folders()

    async def get_sonarr_profiles(self) -> list[QualityProfile]:
        """Get Sonarr quality profiles."""
        return await self.sonarr.get_quality_profiles()

    async def get_sonarr_root_folders(self) -> list[RootFolder]:
        """Get Sonarr root folders."""
        return await self.sonarr.get_root_folders()

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

    async def add_movie(
        self,
        movie: MovieInfo,
        quality_profile_id: int,
        root_folder_path: str,
        search_for_movie: bool = True,
        tags: Optional[list[int]] = None,
    ) -> tuple[Optional[MovieInfo], ActionLog]:
        """
        Add a movie to Radarr.

        Args:
            movie: MovieInfo object
            quality_profile_id: Quality profile ID
            root_folder_path: Root folder path
            search_for_movie: Whether to trigger search after adding
            tags: Optional tag IDs

        Returns:
            Tuple of (added MovieInfo or None on failure, ActionLog)
        """
        log = logger.bind(title=movie.title, tmdb_id=movie.tmdb_id)
        log.info("Adding movie to Radarr")

        action = ActionLog(
            user_id=0,  # Will be set by handler
            action_type=ActionType.ADD,
            content_type=ContentType.MOVIE,
            content_title=movie.title,
            content_id=str(movie.tmdb_id),
        )

        try:
            # Check if already exists
            existing = await self.radarr.get_movie_by_tmdb(movie.tmdb_id)
            if existing and existing.radarr_id:
                log.info("Movie already exists", radarr_id=existing.radarr_id)
                action.success = True
                return existing, action

            added = await self.radarr.add_movie(
                movie=movie,
                quality_profile_id=quality_profile_id,
                root_folder_path=root_folder_path,
                search_for_movie=search_for_movie,
                tags=tags,
            )

            action.success = True
            log.info("Movie added successfully", radarr_id=added.radarr_id)
            return added, action

        except Exception as e:
            log.error("Failed to add movie", error=str(e), exc_info=True)
            action.success = False
            action.error_message = str(e)
            return None, action

    async def add_series(
        self,
        series: SeriesInfo,
        quality_profile_id: int,
        root_folder_path: str,
        monitor_type: str = "all",
        search_for_missing: bool = True,
        tags: Optional[list[int]] = None,
    ) -> tuple[Optional[SeriesInfo], ActionLog]:
        """
        Add a series to Sonarr.

        Args:
            series: SeriesInfo object
            quality_profile_id: Quality profile ID
            root_folder_path: Root folder path
            monitor_type: What to monitor
            search_for_missing: Whether to search for missing episodes
            tags: Optional tag IDs

        Returns:
            Tuple of (added SeriesInfo or None on failure, ActionLog)
        """
        log = logger.bind(title=series.title, tvdb_id=series.tvdb_id)
        log.info("Adding series to Sonarr")

        action = ActionLog(
            user_id=0,  # Will be set by handler
            action_type=ActionType.ADD,
            content_type=ContentType.SERIES,
            content_title=series.title,
            content_id=str(series.tvdb_id),
        )

        try:
            # Check if already exists
            existing = await self.sonarr.get_series_by_tvdb(series.tvdb_id)
            if existing and existing.sonarr_id:
                log.info("Series already exists", sonarr_id=existing.sonarr_id)
                action.success = True
                return existing, action

            added = await self.sonarr.add_series(
                series=series,
                quality_profile_id=quality_profile_id,
                root_folder_path=root_folder_path,
                monitor_type=monitor_type,
                search_for_missing=search_for_missing,
                tags=tags,
            )

            action.success = True
            log.info("Series added successfully", sonarr_id=added.sonarr_id)
            return added, action

        except Exception as e:
            log.error("Failed to add series", error=str(e), exc_info=True)
            action.success = False
            action.error_message = str(e)
            return None, action

    async def grab_movie_release(
        self,
        movie: MovieInfo,
        release: SearchResult,
        quality_profile_id: int,
        root_folder_path: str,
        force_download: bool = False,
    ) -> tuple[bool, ActionLog, str]:
        """
        Grab a specific release for a movie.

        First adds the movie to Radarr (if not exists), then tries to grab the release.
        Falls back to push release if direct grab fails.
        If force_download=True and release is rejected, downloads directly via qBittorrent.

        Args:
            movie: MovieInfo object
            release: SearchResult to grab
            quality_profile_id: Quality profile ID
            root_folder_path: Root folder path
            force_download: Force download even if rejected by Radarr

        Returns:
            Tuple of (success, ActionLog, message)
        """
        log = logger.bind(
            title=movie.title,
            release_title=release.title,
            indexer=release.indexer,
        )
        log.info("Grabbing movie release")

        # user_id will be set by caller before logging
        action = ActionLog(
            user_id=0,
            action_type=ActionType.GRAB,
            content_type=ContentType.MOVIE,
            content_title=movie.title,
            content_id=str(movie.tmdb_id),
            release_title=release.title,
        )

        try:
            # Ensure movie is in Radarr
            existing = await self.radarr.get_movie_by_tmdb(movie.tmdb_id)
            if not existing or not existing.radarr_id:
                log.info("Movie not in library, adding first")
                added, add_action = await self.add_movie(
                    movie=movie,
                    quality_profile_id=quality_profile_id,
                    root_folder_path=root_folder_path,
                    search_for_movie=False,  # We'll grab manually
                )
                if not added:
                    action.success = False
                    action.error_message = add_action.error_message or "Failed to add movie"
                    _log_grab_completed(
                        log, success=False, path="failed",
                        force_download=force_download, content_type=ContentType.MOVIE,
                    )
                    return False, action, "Не удалось добавить фильм"
                existing = added

            # Try to push the release
            release_rejected = False
            rejections = []
            if release.download_url:
                # SEC-16: validate URL BEFORE handing it to Radarr to prevent
                # SSRF via arr → private network.
                if not await _validate_download_url(release.download_url):
                    log.warning(
                        "Skipping push_release: unsafe/private download URL (SEC-16)",
                        download_url=_mask_url(release.download_url),
                    )
                else:
                    try:
                        log.info("Attempting push_release", download_url=_mask_url(release.download_url))
                        result = await self.radarr.push_release(
                            title=release.title,
                            download_url=release.download_url,
                            protocol=release.protocol,
                            publish_date=release.publish_date.isoformat() if release.publish_date else None,
                        )
                        log.info("Push release result", result=_safe_push_result(result))
                        if result and result.get("approved") is True:
                            action.success = True
                            log.info("Release pushed successfully")
                            _log_grab_completed(
                                log, success=True, path="push",
                                force_download=force_download, content_type=ContentType.MOVIE,
                            )
                            return True, action, "Релиз отправлен на скачивание"
                        else:
                            rejections = result.get("rejections", []) if result else []
                            release_rejected = True
                            rejection_msg = f"rejections: {rejections}" if rejections else "no explicit approval from Radarr/Sonarr"
                            log.warning("Release was not approved", reason=rejection_msg)
                    except APIError as e:
                        # BUG-05: no direct-grab fallback here — Prowlarr's guid/
                        # indexerId are meaningless to Radarr's own /release cache
                        # (always 404). Fall straight through to the qBittorrent
                        # fallback / auto-search below.
                        log.warning("Push release failed, falling back to auto-search", error=str(e))

            # Download via qBittorrent if rejected by Radarr profile or force_download
            if (release_rejected or force_download) and self.qbittorrent:
                download_url = release.download_url or release.magnet_url
                if download_url:
                    if not await _validate_download_url(download_url):
                        raise ValueError("Небезопасный URL для скачивания")
                    try:
                        success = await self.qbittorrent.add_torrent_url(
                            download_url,
                            category="radarr",
                        )
                        if success:
                            action.success = True
                            log.info("Downloaded via qBittorrent (bypassed profile rejection)")
                            _log_grab_completed(
                                log, success=True, path="qbit",
                                force_download=force_download, content_type=ContentType.MOVIE,
                            )
                            return True, action, "Загружено через qBittorrent"
                        else:
                            log.error("qBittorrent rejected torrent", download_url=_mask_url(download_url))
                            raise QBittorrentError("Failed to add torrent to qBittorrent")
                    except Exception as e:
                        log.error("qBittorrent download failed", error=str(e), exc_info=True)
                        action.success = False
                        action.error_message = f"qBittorrent fallback failed: {e}"
                        _log_grab_completed(
                            log, success=False, path="failed",
                            force_download=force_download, content_type=ContentType.MOVIE,
                        )
                        return False, action, "Ошибка загрузки через qBittorrent"

            # If rejected and no qBittorrent fallback available
            if release_rejected:
                action.success = False
                rejection_msg = ", ".join(rejections) if rejections else "Отклонено"
                action.error_message = rejection_msg
                # OBS-03: keep the structured rejection reasons in details for history forensics.
                action.details = json.dumps({"rejections": rejections}, ensure_ascii=False)
                _log_grab_completed(
                    log, success=False, path="rejected",
                    force_download=force_download, content_type=ContentType.MOVIE,
                    rejections=rejections,
                )
                return False, action, f"Релиз отклонён: {rejection_msg}"

            # Fallback: trigger search
            if existing and existing.radarr_id:
                await self.radarr.search_movie(existing.radarr_id)
                action.success = True
                log.info("Triggered automatic search as fallback")
                _log_grab_completed(
                    log, success=True, path="auto_search",
                    force_download=force_download, content_type=ContentType.MOVIE,
                )
                return True, action, "Запущен автопоиск (выбранный релиз не удалось передать)"

            action.success = False
            action.error_message = "Could not grab release or trigger search"
            _log_grab_completed(
                log, success=False, path="failed",
                force_download=force_download, content_type=ContentType.MOVIE,
            )
            return False, action, "Не удалось захватить релиз"

        except Exception as e:
            log.error("Failed to grab release", error=str(e), exc_info=True)
            action.success = False
            action.error_message = str(e)
            _log_grab_completed(
                log, success=False, path="failed",
                force_download=force_download, content_type=ContentType.MOVIE,
            )
            return False, action, "Ошибка захвата релиза"

    async def grab_series_release(
        self,
        series: SeriesInfo,
        release: SearchResult,
        quality_profile_id: int,
        root_folder_path: str,
        monitor_type: str = "all",
        force_download: bool = False,
    ) -> tuple[bool, ActionLog, str]:
        """
        Grab a specific release for a series.

        Args:
            series: SeriesInfo object
            release: SearchResult to grab
            quality_profile_id: Quality profile ID
            root_folder_path: Root folder path
            monitor_type: What to monitor
            force_download: Force download even if rejected by Sonarr

        Returns:
            Tuple of (success, ActionLog, message)
        """
        log = logger.bind(
            title=series.title,
            release_title=release.title,
            indexer=release.indexer,
        )
        log.info("Grabbing series release")

        # user_id will be set by caller before logging
        action = ActionLog(
            user_id=0,
            action_type=ActionType.GRAB,
            content_type=ContentType.SERIES,
            content_title=series.title,
            content_id=str(series.tvdb_id),
            release_title=release.title,
        )

        if not series.tvdb_id:
            return False, action, "Сериал не найден в TVDB"

        try:
            # Ensure series is in Sonarr
            existing = await self.sonarr.get_series_by_tvdb(series.tvdb_id)
            if not existing or not existing.sonarr_id:
                log.info("Series not in library, adding first")
                added, add_action = await self.add_series(
                    series=series,
                    quality_profile_id=quality_profile_id,
                    root_folder_path=root_folder_path,
                    monitor_type=monitor_type,
                    search_for_missing=False,
                )
                if not added:
                    action.success = False
                    action.error_message = add_action.error_message or "Failed to add series"
                    _log_grab_completed(
                        log, success=False, path="failed",
                        force_download=force_download, content_type=ContentType.SERIES,
                    )
                    return False, action, "Не удалось добавить сериал"
                existing = added

            # Try to push the release
            release_rejected = False
            rejections = []
            if release.download_url:
                # SEC-16: validate URL BEFORE handing it to Sonarr to prevent
                # SSRF via arr → private network.
                if not await _validate_download_url(release.download_url):
                    log.warning(
                        "Skipping push_release: unsafe/private download URL (SEC-16)",
                        download_url=_mask_url(release.download_url),
                    )
                else:
                    try:
                        log.info("Attempting push_release", download_url=_mask_url(release.download_url))
                        result = await self.sonarr.push_release(
                            title=release.title,
                            download_url=release.download_url,
                            protocol=release.protocol,
                            publish_date=release.publish_date.isoformat() if release.publish_date else None,
                        )
                        log.info("Push release result", result=_safe_push_result(result))
                        if result and result.get("approved") is True:
                            action.success = True
                            log.info("Release pushed successfully")
                            _log_grab_completed(
                                log, success=True, path="push",
                                force_download=force_download, content_type=ContentType.SERIES,
                            )
                            return True, action, "Релиз отправлен на скачивание"
                        else:
                            rejections = result.get("rejections", []) if result else []
                            release_rejected = True
                            rejection_msg = f"rejections: {rejections}" if rejections else "no explicit approval from Radarr/Sonarr"
                            log.warning("Release was not approved", reason=rejection_msg)
                    except APIError as e:
                        # BUG-05: no direct-grab fallback here — Prowlarr's guid/
                        # indexerId are meaningless to Sonarr's own /release cache
                        # (always 404). Fall straight through to the qBittorrent
                        # fallback / auto-search below.
                        log.warning("Push release failed, falling back to auto-search", error=str(e))

            # Download via qBittorrent if rejected by Sonarr profile or force_download
            if (release_rejected or force_download) and self.qbittorrent:
                download_url = release.download_url or release.magnet_url
                if download_url:
                    if not await _validate_download_url(download_url):
                        raise ValueError("Небезопасный URL для скачивания")
                    try:
                        success = await self.qbittorrent.add_torrent_url(
                            download_url,
                            category="tv-sonarr",
                        )
                        if success:
                            action.success = True
                            log.info("Downloaded via qBittorrent (bypassed profile rejection)")
                            _log_grab_completed(
                                log, success=True, path="qbit",
                                force_download=force_download, content_type=ContentType.SERIES,
                            )
                            return True, action, "Загружено через qBittorrent"
                        else:
                            log.error("qBittorrent rejected torrent", download_url=_mask_url(download_url))
                            raise QBittorrentError("Failed to add torrent to qBittorrent")
                    except Exception as e:
                        log.error("qBittorrent download failed", error=str(e), exc_info=True)
                        action.success = False
                        action.error_message = f"qBittorrent fallback failed: {e}"
                        _log_grab_completed(
                            log, success=False, path="failed",
                            force_download=force_download, content_type=ContentType.SERIES,
                        )
                        return False, action, "Ошибка загрузки через qBittorrent"

            # If rejected and no qBittorrent fallback available
            if release_rejected:
                action.success = False
                rejection_msg = ", ".join(rejections) if rejections else "Отклонено"
                action.error_message = rejection_msg
                # OBS-03: keep the structured rejection reasons in details for history forensics.
                action.details = json.dumps({"rejections": rejections}, ensure_ascii=False)
                _log_grab_completed(
                    log, success=False, path="rejected",
                    force_download=force_download, content_type=ContentType.SERIES,
                    rejections=rejections,
                )
                return False, action, f"Релиз отклонён: {rejection_msg}"

            # Fallback: trigger appropriate search
            if existing and existing.sonarr_id:
                if release.is_season_pack and release.detected_season is not None:
                    await self.sonarr.search_season(existing.sonarr_id, release.detected_season)
                    msg = f"Запущен поиск сезона {release.detected_season} (выбранный релиз не удалось передать)"
                else:
                    await self.sonarr.search_series(existing.sonarr_id)
                    msg = "Запущен полный поиск сериала (выбранный релиз не удалось передать)"

                action.success = True
                log.info("Triggered automatic search as fallback")
                _log_grab_completed(
                    log, success=True, path="auto_search",
                    force_download=force_download, content_type=ContentType.SERIES,
                )
                return True, action, msg

            action.success = False
            action.error_message = "Could not grab release or trigger search"
            _log_grab_completed(
                log, success=False, path="failed",
                force_download=force_download, content_type=ContentType.SERIES,
            )
            return False, action, "Не удалось захватить релиз"

        except Exception as e:
            log.error("Failed to grab release", error=str(e), exc_info=True)
            action.success = False
            action.error_message = str(e)
            _log_grab_completed(
                log, success=False, path="failed",
                force_download=force_download, content_type=ContentType.SERIES,
            )
            return False, action, "Ошибка захвата релиза"

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

