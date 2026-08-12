"""Service for adding titles to and grabbing releases through Radarr/Sonarr.

Rollback 2026-08-10, Task 10. This is the last module in the package that
imported the previous backend's client at module level — that single import
(of models Task 2 deleted) was the root cause of most of the suite's
collection errors mid-rollback. Replaced with the two *arr grab paths:

    grab_release(release, content_type, arr_id=...)
        native  — release.indexer_id is set AND release.origin == "arr" (it
                   came from *arr's own interactive search, Task 9's
                   `get_releases`): grab it with
                   `ArrBaseClient.grab_release(guid, indexer_id)`. *arr
                   resolves Prowlarr's `301 -> magnet` redirect itself, hands
                   the torrent to its own download client and imports the
                   result. This is precisely the capability whose absence
                   killed the previous backend — it could not expand that
                   redirect, so every download failed with a bare
                   "Internal server error".

                   Fix round 1 (2026-08-10 review): `indexer_id` truthiness
                   ALONE is not a safe signal — ProwlarrClient's free-text
                   parser also fills `indexer_id`, with PROWLARR's own
                   numbering, which is meaningless (or actively wrong) to
                   *arr's native endpoint. `origin` (named `source` in round
                   1; renamed in fix round 2 — collided with the unrelated
                   `QualityInfo.source`) records which parser actually built
                   the SearchResult and is what really gates this path; it
                   defaults to "prowlarr" (fail CLOSED — an untagged release
                   is never native by default), see SearchResult's docstring
                   in bot/models.py.
        push chain — release.origin != "arr" (today: a Prowlarr free-text
                   hit, OR anything hand-built without an explicit origin —
                   Prowlarr numbers indexers differently from *arr, so its id
                   is meaningless to the native endpoint): the restored
                   pre-migration three-step chain — release/push
                   (SEC-16-gated, see `_validate_download_url`) ->
                   qBittorrent by downloadUrl -> *arr auto-search command.

The push chain was UNREACHABLE from the rollback until 2026-08-12: every
selectable release came from `SearchService.search_releases_for_title` — *arr's
interactive search — and was therefore tagged `origin="arr"`, while
`ProwlarrClient.search()`, the free-text mode that produces
`origin="prowlarr"` releases, had no caller at all. It has one now:
`SearchService.search_free_text`, behind the `/find` command and the "искать по
названию" button on a dead-end search screen.

Such a hit usually has no library entry behind it, which is why `arr_id` is
optional: with no id, the chain's final auto-search step is skipped rather than
faked — that command takes an id there is none of, and reporting success for a
queue nothing entered would be a lie.

`arr_id` is the movie/series id already in Radarr/Sonarr. `grab_release`'s
caller is expected to have ensured the title exists in the library before
calling this (the same precondition `SearchService.search_releases_for_title`
has, since listing releases needs that id too) — `grab_release` itself does
not add the title. `add_movie`/`add_series` below are what add it, restored
from `f30545d~1` and adapted to the current `RadarrClient`/`SonarrClient`
signatures (fix round 1 — the first version of this file dropped them,
which was too aggressive: real callers already exist in
bot/handlers/{trending.py,search/commands.py,search/grab.py}, even though
fixing those call sites is Task 12's job, not this one's).

Composite flows specific to the previous backend, with no *arr equivalent
specified by any task yet (`ensure_title`, `add_and_queue_best`,
`grab_with_fallback`), are kept as
`NotImplementedError` stubs naming Task 12/13, matching the precedent
`search_service.py` set for `search_metadata`/`get_seasons` — a stub that
fails loudly beats both silently guessing a shape and a bare
`AttributeError` on a name nobody bothered to keep.
"""

import json
from typing import Optional

import structlog

from bot.clients.base import APIError, ArrBaseClient
from bot.clients.lidarr import LidarrClient
from bot.clients.qbittorrent import QBittorrentClient
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

# SEC-16 и вся маскировка секретов живут в bot/services/url_guard.py начиная с
# 2026-08-12 — TorrServer'у нужна та же гайка, а тащить ради одной функции весь
# AddService (Radarr + Sonarr + qBittorrent + Lidarr) в стриминговый контур —
# плохой обмен. Реэкспорт держит `bot.services.add_service.<name>` рабочим: и
# для импортов в tests/, и для
# `patch("bot.services.add_service._validate_download_url", ...)`.
from bot.services.url_guard import (  # noqa: F401,E402
    _ALLOWED_SCHEMES,
    _DEFAULT_SCHEME_PORTS,
    _is_internal_ip,
    _mask_path,
    _mask_url,
    _SECRET_PATH_SEGMENT_RE,
    _SENSITIVE_QUERY_PARAMS,
    _trusted_service_hosts,
    _validate_download_url,
)

logger = structlog.get_logger()

# qBittorrent categories used by both the force-download escape hatch and the
# push chain's own qBittorrent fallback step. They mirror the folder layout
# Radarr/Sonarr import from, so a bypassed grab still lands where the library
# expects it.
_QBIT_CATEGORIES = {
    ContentType.MOVIE: "radarr",
    ContentType.SERIES: "tv-sonarr",
    ContentType.ANIME: "anime",
}


def _qbit_category(content_type: ContentType, release: SearchResult) -> str:
    """qBittorrent category for a release.

    Free-text search does not know the facet up front — its `content_type` is
    `UNKNOWN` — but Prowlarr's own category ids usually do, and
    `ProwlarrClient._normalize_result` puts that in `detected_type`. Falls back
    to the movie category, which is what these call sites used unconditionally
    before.
    """
    if content_type in _QBIT_CATEGORIES:
        return _QBIT_CATEGORIES[content_type]
    return _QBIT_CATEGORIES.get(release.detected_type, "radarr")


def _safe_push_result(result: Optional[dict]) -> dict:
    """SEC-03: extract only the safe fields from an *arr push-release response.

    The raw response echoes the pushed release object including
    ``downloadUrl``, which for private trackers embeds a reusable
    passkey/apikey. Logging the raw dict leaks that credential to container
    logs, so only the decision fields (``approved``/``rejections``) that the
    caller actually acts on are kept.
    """
    if not isinstance(result, dict):
        return {"approved": None, "rejections": []}
    return {
        "approved": result.get("approved"),
        "rejections": result.get("rejections", []),
    }


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

    `path` values: native | push | qbit | auto_search | rejected | failed.
    """
    log.info(
        "grab_completed",
        success=success,
        path=path,
        force_download=force_download,
        content_type=content_type.value,
        detail=detail,
    )


class AddService:
    """Grabs release candidates for titles already in Radarr/Sonarr, plus the
    Lidarr artist-add path (unchanged by this rollback).
    """

    def __init__(
        self,
        radarr: RadarrClient,
        sonarr: SonarrClient,
        qbittorrent: Optional[QBittorrentClient] = None,
        lidarr: Optional[LidarrClient] = None,
    ):
        self.radarr = radarr
        self.sonarr = sonarr
        self.qbittorrent = qbittorrent
        self.lidarr = lidarr

    # ------------------------------------------------------ radarr / sonarr
    async def get_radarr_profiles(self) -> list[QualityProfile]:
        """Radarr quality profiles, via `ArrBaseClient.get_quality_profiles`.

        Task 13: mirrors the `get_lidarr_*` shape below so settings.py's
        table-driven picker (`_SettingsEntry.getter`) can drive Radarr and
        Sonarr the same way it already drives Lidarr, instead of a one-off
        direct-client path for just these two.
        """
        return await self.radarr.get_quality_profiles()

    async def get_radarr_root_folders(self) -> list[RootFolder]:
        """Radarr root folders, via `ArrBaseClient.get_root_folders`."""
        return await self.radarr.get_root_folders()

    async def get_sonarr_profiles(self) -> list[QualityProfile]:
        """Sonarr quality profiles, via `ArrBaseClient.get_quality_profiles`."""
        return await self.sonarr.get_quality_profiles()

    async def get_sonarr_root_folders(self) -> list[RootFolder]:
        """Sonarr root folders, via `ArrBaseClient.get_root_folders`."""
        return await self.sonarr.get_root_folders()

    # ------------------------------------------------------------- lidarr
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

        Ids are compared as strings: *arr/Lidarr profile ids are ints but a
        stored preference may still be a string (JSON round-trip, or a
        pre-rollback value) — comparing via `str(...)` is robust to either.
        """
        if preferred_id is not None and preferred_id != "":
            match = next((p for p in profiles if str(p.id) == str(preferred_id)), None)
            if match is not None:
                return match
        return profiles[0]

    @staticmethod
    def resolve_root_folder(folders: list[RootFolder], preferred_id) -> str:
        """LOGIC-11: resolve a root folder PATH from the user's preference or
        the first available folder (preferring the one marked default).

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
    async def add_movie(
        self,
        movie: MovieInfo,
        quality_profile_id: int,
        root_folder_path: str,
        search_for_movie: bool = True,
        monitored: bool = True,
        tags: Optional[list[int]] = None,
    ) -> tuple[Optional[MovieInfo], ActionLog]:
        """Add a movie to Radarr (restored from `f30545d~1`).

        `monitored` is caller-facing because the search flow adds a title just
        to obtain its Radarr id before listing releases. Added monitored, a
        title the user merely looked at and abandoned joins Radarr's RSS loop
        and gets downloaded on the next sync — so that path adds unmonitored
        and flips the flag only when a release is actually grabbed.
        """
        log = logger.bind(title=movie.title, tmdb_id=movie.tmdb_id)
        log.info("Adding movie to Radarr")

        action = ActionLog(
            user_id=0,  # set by the caller before logging
            action_type=ActionType.ADD,
            content_type=ContentType.MOVIE,
            content_title=movie.title,
            content_id=str(movie.tmdb_id),
        )

        try:
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
                monitored=monitored,
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
        content_type: ContentType = ContentType.SERIES,
        monitor: str = "all",
        search_for_missing: bool = True,
        monitored: bool = True,
        tags: Optional[list[int]] = None,
    ) -> tuple[Optional[SeriesInfo], ActionLog]:
        """Add a series (or anime) to Sonarr — restored from `f30545d~1`,
        adapted to the current `SonarrClient.add_series` signature.

        Rollback 2026-08-10: anime is not a separate service/library, just
        Sonarr's own `seriesType` field (`ContentType.sonarr_series_type`) —
        `content_type=ContentType.ANIME` is the one thing that changes
        `series_type` from the pre-migration call. The old caller-facing
        `monitor_type` kwarg is `monitor` here, matching the current client.
        """
        log = logger.bind(title=series.title, tvdb_id=series.tvdb_id)
        log.info("Adding series to Sonarr")

        action = ActionLog(
            user_id=0,  # set by the caller before logging
            action_type=ActionType.ADD,
            content_type=content_type,
            content_title=series.title,
            content_id=str(series.tvdb_id),
        )

        try:
            existing = await self.sonarr.get_series_by_tvdb(series.tvdb_id)
            if existing and existing.sonarr_id:
                log.info("Series already exists", sonarr_id=existing.sonarr_id)
                action.success = True
                return existing, action

            added = await self.sonarr.add_series(
                series=series,
                quality_profile_id=quality_profile_id,
                root_folder_path=root_folder_path,
                monitor=monitor,
                search_for_missing=search_for_missing,
                monitored=monitored,
                series_type=content_type.sonarr_series_type or "standard",
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

    # Fix round 1 (2026-08-10 review): a service whose own docstring promises
    # "adding content and grabbing releases" must be able to add — bare
    # AttributeErrors on these three names is a worse failure mode than a
    # named NotImplementedError, and real callers already exist
    # (bot/handlers/{trending.py,search/commands.py,search/grab.py}) even
    # though repointing them is Task 12's job. Matches the precedent
    # search_service.py set for search_metadata/get_seasons: fail loudly with
    # a message naming the task that owns the replacement, rather than
    # guessing a shape with no driving test.
    _COMPOSITE_FLOW_NOT_CONVERTED = (
        "was specific to the previous backend (its composite "
        "add+queue/redeem-candidate-token flow has no *arr equivalent "
        "specified by any task yet) and was not carried into the *arr "
        "rollback — see Task 12/13, "
        "docs/superpowers/plans/2026-08-10-arr-restore.md"
    )

    async def ensure_title(self, *args, **kwargs):
        """The previous backend's "find or add unmonitored" step. Task 12
        explicitly removes this step from the search flow rather than
        repointing it."""
        raise NotImplementedError(f"ensure_title {self._COMPOSITE_FLOW_NOT_CONVERTED}")

    async def add_and_queue_best(self, *args, **kwargs):
        """The previous backend's "add + let the profile pick + queue
        automatically" one-shot. The nearest *arr equivalent is
        `add_movie`/`add_series` with `search_for_movie`/
        `search_for_missing=True`, but no task has specified the
        caller-facing shape ("Скачать лучшее") yet."""
        raise NotImplementedError(f"add_and_queue_best {self._COMPOSITE_FLOW_NOT_CONVERTED}")

    async def grab_with_fallback(self, *args, **kwargs):
        """The previous backend's multi-candidate retry loop (some indexers'
        download links it couldn't resolve to an info-hash). `grab_release`'s
        native/push split already handles the one release *arr's own verdict
        picked; no task has specified multi-candidate retry for the *arr
        rollback."""
        raise NotImplementedError(f"grab_with_fallback {self._COMPOSITE_FLOW_NOT_CONVERTED}")

    # ---------------------------------------------------------------- grab
    async def grab_release(
        self,
        release: SearchResult,
        content_type: ContentType,
        *,
        arr_id: Optional[int] = None,
        force_download: bool = False,
    ) -> tuple[bool, ActionLog]:
        """Turn one release candidate into a download.

        Path is picked by what the release carries — see the module
        docstring. `force_download=True` bypasses both paths entirely and
        goes straight to qBittorrent: the escape hatch for a release *arr's
        profile would refuse but the user wants anyway.

        `arr_id=None` is the free-text case (`SearchService.search_free_text`):
        no library entry stands behind the release. The native path is
        unreachable there by construction (`origin="prowlarr"`), and the push
        chain's final auto-search step is skipped — that command takes an id
        there is none of.
        """
        arr_client = self.radarr if content_type is ContentType.MOVIE else self.sonarr
        action = ActionLog(
            user_id=0,  # set by the caller before logging
            action_type=ActionType.GRAB,
            content_type=content_type,
            content_id=str(arr_id) if arr_id is not None else "",
            release_title=release.title,
        )
        log = logger.bind(
            arr_id=arr_id,
            release_title=release.title[:80],
            indexer=release.indexer,
        )

        if force_download:
            return await self._force_download(log, action, release, content_type)

        # Fix round 1 (2026-08-10 review): `indexer_id` alone is not a safe
        # signal — ProwlarrClient's free-text parser also fills it, with
        # PROWLARR's own (incompatible) indexer numbering. `origin == "arr"`
        # is what actually guarantees this release came from *arr's own
        # interactive search, where the id is *arr's own. Fix round 2:
        # `origin` defaults to "prowlarr", so an untagged release fails
        # closed here rather than being trusted as native by omission.
        if release.indexer_id and release.origin == "arr":
            return await self._grab_native(log, action, arr_client, release, content_type)

        return await self._grab_via_push_chain(
            log, action, arr_client, release, content_type, arr_id,
        )

    async def _grab_native(
        self,
        log,
        action: ActionLog,
        arr_client: ArrBaseClient,
        release: SearchResult,
        content_type: ContentType,
    ) -> tuple[bool, ActionLog]:
        """Native path: hand *arr the guid+indexerId it already scored itself."""
        try:
            await arr_client.grab_release(release.guid, release.indexer_id)
        except Exception as e:
            log.warning("native_grab_failed", error=str(e))
            action.success = False
            action.error_message = str(e)
            _log_grab_completed(
                log, success=False, path="failed", force_download=False, content_type=content_type,
            )
            return False, action

        action.success = True
        _log_grab_completed(
            log, success=True, path="native", force_download=False, content_type=content_type,
        )
        return True, action

    async def _grab_via_push_chain(
        self,
        log,
        action: ActionLog,
        arr_client: ArrBaseClient,
        release: SearchResult,
        content_type: ContentType,
        arr_id: Optional[int],
    ) -> tuple[bool, ActionLog]:
        """Restored pre-migration fallback for a release with no *arr indexer id.

        release/push -> qBittorrent by downloadUrl -> *arr auto-search
        command. Each step only runs if the previous one did not already
        finish the job. An explicit profile rejection stops the chain rather
        than falling through to auto-search — the user asked for THIS
        release, not "anything the profile likes"; auto-search is reserved
        for when *arr never got to render a verdict at all (e.g. the push
        request itself failed).
        """
        try:
            release_rejected = False
            rejections: list[str] = []

            if release.download_url:
                # SEC-16 (fix round 1, restored): validate the URL BEFORE
                # handing it to Radarr/Sonarr. *arr fetches whatever URL it
                # is given, from inside the LAN — handing it a private/
                # loopback URL would turn *arr itself into an SSRF proxy.
                # An unsafe URL is treated the same as a transport failure
                # below: no verdict was rendered, so this falls through to
                # auto-search rather than being reported as a rejection.
                if not await _validate_download_url(release.download_url):
                    log.warning(
                        "push_skipped_unsafe_url",
                        download_url=_mask_url(release.download_url),
                    )
                else:
                    try:
                        result = await arr_client.push_release(
                            title=release.title,
                            download_url=release.download_url,
                            protocol=release.protocol,
                            publish_date=release.publish_date.isoformat() if release.publish_date else None,
                        )
                        log.info("push_release_result", result=_safe_push_result(result))
                        if result and result.get("approved") is True:
                            action.success = True
                            _log_grab_completed(
                                log, success=True, path="push", force_download=False, content_type=content_type,
                            )
                            return True, action
                        rejections = result.get("rejections", []) if result else []
                        release_rejected = True
                    except APIError as e:
                        # Falls through to auto-search below, same as pre-migration:
                        # a transport failure is not a verdict, so it isn't treated
                        # as one.
                        log.warning("push_release_failed", error=str(e))

            if release_rejected and self.qbittorrent is not None:
                download_url = release.download_url or release.magnet_url
                if download_url and await _validate_download_url(download_url):
                    try:
                        success = await self.qbittorrent.add_torrent_url(
                            download_url, category=_qbit_category(content_type, release),
                        )
                    except Exception as e:
                        log.error("qbittorrent_fallback_failed", error=str(e), exc_info=True)
                        action.success = False
                        action.error_message = f"qBittorrent fallback failed: {e}"
                        _log_grab_completed(
                            log, success=False, path="failed", force_download=False, content_type=content_type,
                        )
                        return False, action

                    if success:
                        action.success = True
                        _log_grab_completed(
                            log, success=True, path="qbit", force_download=False, content_type=content_type,
                        )
                        return True, action

                    log.error("qbittorrent_rejected_torrent", download_url=_mask_url(download_url))
                    action.success = False
                    action.error_message = "qBittorrent rejected the torrent"
                    _log_grab_completed(
                        log, success=False, path="failed", force_download=False, content_type=content_type,
                    )
                    return False, action

            if release_rejected:
                action.success = False
                rejection_msg = ", ".join(rejections) if rejections else "Отклонено"
                action.error_message = rejection_msg
                # OBS-03: keep the structured rejection reasons in details for history forensics.
                action.details = json.dumps({"rejections": rejections}, ensure_ascii=False)
                _log_grab_completed(
                    log, success=False, path="rejected", force_download=False,
                    content_type=content_type, detail=rejection_msg[:200],
                )
                return False, action

            # Push never ran (no download_url) or transport-failed: fall back
            # to *arr's own auto-search rather than giving up outright.
            if arr_id is None:
                # Free-text grab: no library entry for *arr to search against.
                # Reporting success here would be a lie — nothing was queued.
                action.success = False
                action.error_message = (
                    "Тайтла нет в библиотеке — Radarr/Sonarr не может искать сам"
                )
                _log_grab_completed(
                    log, success=False, path="failed", force_download=False,
                    content_type=content_type, detail="no_arr_id_for_auto_search",
                )
                return False, action

            try:
                # `self.radarr`/`self.sonarr` rather than the pre-resolved
                # `arr_client`: these three commands are defined on the
                # concrete clients, not on ArrBaseClient (which carries only
                # what Radarr, Sonarr and Lidarr genuinely share). Going
                # through the base would mean typing `arr_client` loosely
                # enough to hide a typo in any of these names.
                if content_type is ContentType.MOVIE:
                    await self.radarr.search_movie(arr_id)
                elif release.is_season_pack and release.detected_season is not None:
                    await self.sonarr.search_season(arr_id, release.detected_season)
                else:
                    await self.sonarr.search_series(arr_id)
            except APIError as e:
                log.warning("auto_search_failed", error=str(e))
                action.success = False
                action.error_message = str(e)
                _log_grab_completed(
                    log, success=False, path="failed", force_download=False, content_type=content_type,
                )
                return False, action

            action.success = True
            _log_grab_completed(
                log, success=True, path="auto_search", force_download=False, content_type=content_type,
            )
            return True, action

        except Exception as e:
            log.error("push_chain_failed", error=str(e), exc_info=True)
            action.success = False
            action.error_message = str(e)
            _log_grab_completed(
                log, success=False, path="failed", force_download=False, content_type=content_type,
            )
            return False, action

    async def _force_download(
        self,
        log,
        action: ActionLog,
        release: SearchResult,
        content_type: ContentType,
    ) -> tuple[bool, ActionLog]:
        """Bypass *arr entirely and push the torrent straight into qBittorrent."""
        if self.qbittorrent is None:
            action.success = False
            action.error_message = "qBittorrent not configured"
            return False, action

        download_url = release.download_url or release.magnet_url
        if not download_url:
            action.success = False
            action.error_message = "no download url"
            return False, action

        if not await _validate_download_url(download_url):
            log.warning(
                "force_download_blocked_unsafe_url",
                download_url=_mask_url(download_url),
            )
            action.success = False
            action.error_message = "unsafe download url"
            return False, action

        try:
            ok = await self.qbittorrent.add_torrent_url(
                download_url,
                category=_qbit_category(content_type, release),
            )
        except Exception as e:
            log.error("qbittorrent_force_download_failed", error=str(e), exc_info=True)
            action.success = False
            action.error_message = str(e)
            _log_grab_completed(
                log, success=False, path="failed", force_download=True, content_type=content_type,
            )
            return False, action

        if not ok:
            log.error("qbittorrent_rejected_torrent", download_url=_mask_url(download_url))
            action.success = False
            action.error_message = "qBittorrent rejected the torrent"
            _log_grab_completed(
                log, success=False, path="failed", force_download=True, content_type=content_type,
            )
            return False, action

        action.success = True
        _log_grab_completed(
            log, success=True, path="qbit", force_download=True, content_type=content_type,
        )
        logger.debug("force_download_ok", url=_mask_url(download_url))
        return True, action

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
