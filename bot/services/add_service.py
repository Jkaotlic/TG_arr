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
                   is meaningless to the native endpoint): release/push
                   (SEC-16-gated, see `_validate_download_url`) and *arr's
                   verdict, reported as is. Audit 2026-09-23 GRAB-08 removed
                   the two silent steps that followed (qBittorrent after a
                   rejection, *arr auto-search after a failed push): both are
                   now explicit user choices — `force_download=True` and
                   `auto_search()` — and a failed grab records WHY in
                   `ActionLog.details` (`grab_outcome()`).

The push chain was UNREACHABLE from the rollback until 2026-08-12: every
selectable release came from `SearchService.search_releases_for_title` — *arr's
interactive search — and was therefore tagged `origin="arr"`, while
`ProwlarrClient.search()`, the free-text mode that produces
`origin="prowlarr"` releases, had no caller at all. It has one now:
`SearchService.search_free_text`, behind the `/find` command and the "искать по
названию" button on a dead-end search screen.

Such a hit usually has no library entry behind it, which is why `arr_id` is
optional: with no id there is nothing for *arr to auto-search, and reporting
success for a queue nothing entered would be a lie.

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

Композитные потоки прежнего бэкенда (`ensure_title`, `add_and_queue_best`,
`grab_with_fallback`) не имеют эквивалента в *arr; заглушки
`NotImplementedError` удалены (DEAD-01): вызывать их было некому.
"""

import json
from typing import Optional

import structlog

from bot.clients.base import APIError, ArrBaseClient, ServiceConnectionError
from bot.clients.lidarr import LidarrClient
from bot.clients.qbittorrent import DIRECT_TAG, QBittorrentClient
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
    `ProwlarrClient._normalize_result` puts that in `detected_type`.

    LOGIC-10: музыка и нераспознанный тип получают пустую категорию (qBit
    кладёт торрент без категории). Раньше они уходили в `radarr`: Radarr видел
    чужой торрент в своей категории, а уведомление о завершении подавлялось
    правилом «этим занимается library_watch» (его обходит только тег
    `DIRECT_TAG`). Пустая строка нигде ниже не требуется: клиент qBit её просто
    не отправляет, а `NotificationService` читает `category or ""`.
    """
    if content_type in _QBIT_CATEGORIES:
        return _QBIT_CATEGORIES[content_type]
    return _QBIT_CATEGORIES.get(release.detected_type, "")


#: Audit 2026-09-23 (GRAB-08/14): why a grab did not happen, stored as
#: ``{"outcome": ...}`` in ``ActionLog.details`` (the JSON blob the history
#: already keeps rejections in). Handlers read it with :func:`grab_outcome`
#: to decide what to offer next.
GRAB_OUTCOME_REJECTED = "rejected"        # *arr refused THIS release (reasons attached)
GRAB_OUTCOME_UNREACHABLE = "unreachable"  # never got a verdict/queue — *arr may search itself
GRAB_OUTCOME_GONE = "gone"                # *arr's release cache no longer has it

#: User-facing text for GRAB_OUTCOME_GONE (GRAB-14).
RELEASE_GONE_TEXT = "Релиз больше недоступен — обновите поиск"


def _set_outcome(action: ActionLog, outcome: str, **extra) -> None:
    action.details = json.dumps({"outcome": outcome, **extra}, ensure_ascii=False)


def grab_outcome(action: ActionLog) -> Optional[str]:
    """The ``outcome`` a failed grab recorded, or None (success / unknown)."""
    if not action.details:
        return None
    try:
        data = json.loads(action.details)
    except (TypeError, ValueError):
        return None
    outcome = data.get("outcome") if isinstance(data, dict) else None
    return outcome if isinstance(outcome, str) else None


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


#: GRAB-06: a profile whose name says 4K/2160 is the sensible default for this
#: library (live profile 7 "4k/1080p"), unlike profiles[0] ("Any").
_UHD_PROFILE_MARKERS = ("4k", "2160")


def resolve_profile_with_source(
    profiles: list[QualityProfile],
    preferred_id,
    default_id=None,
) -> tuple[QualityProfile, str]:
    """Radarr/Sonarr quality profile for an add, and where it came from.

    Audit 2026-09-23 GRAB-06 — fallback order: ``"user"`` (the /settings
    preference) → ``"config"`` (RADARR/SONARR_DEFAULT_PROFILE_ID) → ``"name"``
    (first profile whose name contains 4k/2160) → ``"first"`` (profiles[0];
    /settings warns). Ids compare as strings, like
    ``AddService.resolve_profile``. A module function so handlers call the
    real rule even where tests replace the ``AddService`` class.
    """
    for wanted, source in ((preferred_id, "user"), (default_id, "config")):
        if wanted is not None and wanted != "":
            match = next((p for p in profiles if str(p.id) == str(wanted)), None)
            if match is not None:
                return match, source
    for p in profiles:
        name = (p.name or "").casefold()
        if any(m in name for m in _UHD_PROFILE_MARKERS):
            return p, "name"
    return profiles[0], "first"


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
    def resolve_profile_with_source(
        profiles: list[QualityProfile], preferred_id, default_id=None,
    ) -> tuple[QualityProfile, str]:
        """Alias of the module-level :func:`resolve_profile_with_source` (GRAB-06)."""
        return resolve_profile_with_source(profiles, preferred_id, default_id)

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
        # Музыка — третий клиент, а не «всё, что не фильм». Без этой ветки
        # музыкальный релиз уезжал в Sonarr и «успешно» грабился там.
        # Аннотация обязательна: без неё mypy выводит тип по первой ветке
        # (RadarrClient) и ругается на две остальные.
        arr_client: Optional[ArrBaseClient]
        if content_type is ContentType.MOVIE:
            arr_client = self.radarr
        elif content_type is ContentType.MUSIC:
            arr_client = self.lidarr
        else:
            arr_client = self.sonarr
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

        # Музыка — единственный опциональный клиент из трёх (`get_lidarr()`
        # возвращает None без конфига). Без этой проверки граб уходил в мок
        # отсутствующего клиента и отчитывался успехом.
        if arr_client is None:
            action.success = False
            action.error_message = "Lidarr не настроен"
            _log_grab_completed(
                log, success=False, path="failed", force_download=False,
                content_type=content_type, detail="lidarr_not_configured",
            )
            return False, action

        # Fix round 1 (2026-08-10 review): `indexer_id` alone is not a safe
        # signal — ProwlarrClient's free-text parser also fills it, with
        # PROWLARR's own (incompatible) indexer numbering. `origin == "arr"`
        # is what actually guarantees this release came from *arr's own
        # interactive search, where the id is *arr's own. Fix round 2:
        # `origin` defaults to "prowlarr", so an untagged release fails
        # closed here rather than being trusted as native by omission.
        if release.indexer_id and release.origin == "arr":
            return await self._grab_native(log, action, arr_client, release, content_type, arr_id)

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
        arr_id: Optional[int] = None,
    ) -> tuple[bool, ActionLog]:
        """Native path: hand *arr the guid+indexerId it already scored itself.

        Audit 2026-09-23 GRAB-14, two honest recoveries:
        - 404: *arr keeps interactive-search results ~30 min, the bot's
          session lives 24 h. Re-run the interactive search, find the SAME
          guid and retry once; otherwise say the release is gone.
        - timeout / connection drop: the POST may have landed. Look for the
          release in *arr's queue before calling it a failure.
        Any other failure is "unreachable": nothing was queued, and the
        handler offers *arr's own search as an explicit, labelled choice.
        """
        indexer_id = release.indexer_id or 0
        try:
            await arr_client.grab_release(release.guid, indexer_id)
        except APIError as e:
            if e.status_code == 404:
                return await self._retry_expired_release(
                    log, action, arr_client, release, content_type, arr_id,
                )
            if await self._landed_despite_error(log, action, arr_client, release, content_type, e):
                return True, action
            return self._native_failed(log, action, content_type, e)
        except Exception as e:
            return self._native_failed(log, action, content_type, e)

        action.success = True
        _log_grab_completed(
            log, success=True, path="native", force_download=False, content_type=content_type,
        )
        return True, action

    def _native_failed(
        self, log, action: ActionLog, content_type: ContentType, error: Exception,
    ) -> tuple[bool, ActionLog]:
        log.warning("native_grab_failed", error=str(error))
        action.success = False
        action.error_message = str(error)
        _set_outcome(action, GRAB_OUTCOME_UNREACHABLE)
        _log_grab_completed(
            log, success=False, path="failed", force_download=False, content_type=content_type,
        )
        return False, action

    async def _landed_despite_error(
        self,
        log,
        action: ActionLog,
        arr_client: ArrBaseClient,
        release: SearchResult,
        content_type: ContentType,
        error: Exception,
        path: str = "native",
    ) -> bool:
        """GRAB-14: ответ потерян, а POST мог дойти — релиз уже в очереди *arr?"""
        if not isinstance(error, ServiceConnectionError):
            return False
        if not await self._landed_in_queue(arr_client, release):
            return False
        log.info("grab_found_in_queue_after_error", path=path, error=str(error))
        action.success = True
        _log_grab_completed(
            log, success=True, path=path, force_download=False,
            content_type=content_type, detail="queued_despite_error",
        )
        return True

    @staticmethod
    async def _landed_in_queue(arr_client: ArrBaseClient, release: SearchResult) -> bool:
        try:
            return bool(await arr_client.find_in_queue(release.title))
        except Exception:
            return False

    async def _refetch_releases(
        self, content_type: ContentType, arr_id: int, release: SearchResult,
    ) -> list[SearchResult]:
        """Re-run *arr's interactive search the list came from."""
        if content_type is ContentType.MOVIE:
            return await self.radarr.get_releases(arr_id)
        if content_type is ContentType.MUSIC:
            if self.lidarr is None:
                return []
            return await self.lidarr.get_releases(arr_id)
        return await self.sonarr.get_releases(arr_id, season_number=release.detected_season)

    async def _retry_expired_release(
        self,
        log,
        action: ActionLog,
        arr_client: ArrBaseClient,
        release: SearchResult,
        content_type: ContentType,
        arr_id: Optional[int],
    ) -> tuple[bool, ActionLog]:
        """GRAB-14: *arr forgot the release (404) — look it up again, retry once."""
        fresh: Optional[SearchResult] = None
        if arr_id is not None:
            try:
                releases = await self._refetch_releases(content_type, arr_id, release)
            except Exception as e:
                log.warning("release_refetch_failed", error=str(e))
                releases = []
            fresh = next((r for r in releases if r.guid == release.guid), None)

        if fresh is not None:
            try:
                await arr_client.grab_release(fresh.guid, fresh.indexer_id or 0)
            except Exception as e:
                # Review fix round 1: a second 404 means *arr forgot it again —
                # the release is gone, not "unreachable".
                if not (isinstance(e, APIError) and e.status_code == 404):
                    if await self._landed_despite_error(
                        log, action, arr_client, fresh, content_type, e,
                    ):
                        return True, action
                    return self._native_failed(log, action, content_type, e)
                log.warning("release_retry_404", error=str(e))
            else:
                action.success = True
                _log_grab_completed(
                    log, success=True, path="native", force_download=False,
                    content_type=content_type, detail="retried_after_404",
                )
                return True, action

        action.success = False
        action.error_message = RELEASE_GONE_TEXT
        _set_outcome(action, GRAB_OUTCOME_GONE)
        _log_grab_completed(
            log, success=False, path="failed", force_download=False,
            content_type=content_type, detail="release_gone",
        )
        return False, action

    async def _grab_via_push_chain(
        self,
        log,
        action: ActionLog,
        arr_client: ArrBaseClient,
        release: SearchResult,
        content_type: ContentType,
        arr_id: Optional[int],
    ) -> tuple[bool, ActionLog]:
        """Push a release with no *arr indexer id to *arr and report its verdict.

        Audit 2026-09-23 GRAB-08 — the chain used to end in two silent
        substitutions: a profile rejection went on to qBittorrent anyway, and a
        push that never reached a verdict turned into *arr's own auto-search,
        reported to the user as if THIS release had been taken. Now:
        - approved → success;
        - rejected → stop, reasons in ``error_message`` and ``details``
          (qBittorrent stays an explicit button, never a fallback);
        - no verdict (transport error, no/unsafe URL) → failure with outcome
          "unreachable"; the handler offers :meth:`auto_search` as a labelled
          choice ("релиз может быть другим").
        """
        try:
            if release.download_url:
                # SEC-16 (fix round 1, restored): validate the URL BEFORE
                # handing it to Radarr/Sonarr. *arr fetches whatever URL it
                # is given, from inside the LAN — handing it a private/
                # loopback URL would turn *arr itself into an SSRF proxy.
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
                    except APIError as e:
                        # Сбой транспорта — ещё не вердикт, но push мог дойти, а
                        # «нет вердикта» ведёт к предложению автопоиска.
                        log.warning("push_release_failed", error=str(e))
                        if await self._landed_despite_error(
                            log, action, arr_client, release, content_type, e, path="push",
                        ):
                            return True, action
                    else:
                        log.info("push_release_result", result=_safe_push_result(result))
                        if result and result.get("approved") is True:
                            action.success = True
                            _log_grab_completed(
                                log, success=True, path="push", force_download=False, content_type=content_type,
                            )
                            return True, action
                        raw = result.get("rejections") if isinstance(result, dict) else None
                        rejections = [str(r) for r in raw] if isinstance(raw, list) else []
                        action.success = False
                        rejection_msg = ", ".join(rejections) if rejections else "Отклонено"
                        action.error_message = rejection_msg
                        # OBS-03: structured rejection reasons for history forensics.
                        _set_outcome(action, GRAB_OUTCOME_REJECTED, rejections=rejections)
                        _log_grab_completed(
                            log, success=False, path="rejected", force_download=False,
                            content_type=content_type, detail=rejection_msg[:200],
                        )
                        return False, action

            # No verdict at all: nothing was queued.
            action.success = False
            action.error_message = (
                "Не удалось передать релиз в Radarr/Sonarr"
                if arr_id is not None
                else "Тайтла нет в библиотеке — Radarr/Sonarr не может искать сам"
            )
            _set_outcome(action, GRAB_OUTCOME_UNREACHABLE)
            _log_grab_completed(
                log, success=False, path="failed", force_download=False,
                content_type=content_type, detail="no_verdict",
            )
            return False, action

        except Exception as e:
            log.error("push_chain_failed", error=str(e), exc_info=True)
            action.success = False
            action.error_message = str(e)
            _set_outcome(action, GRAB_OUTCOME_UNREACHABLE)
            _log_grab_completed(
                log, success=False, path="failed", force_download=False, content_type=content_type,
            )
            return False, action

    async def auto_search(
        self,
        release: SearchResult,
        content_type: ContentType,
        *,
        arr_id: int,
    ) -> tuple[bool, ActionLog]:
        """Ask *arr to search for the title itself — an explicit user choice.

        Audit 2026-09-23 GRAB-08: this used to run silently at the end of the
        push chain and was reported as a grab of the selected release. It is
        now offered as "🔍 Пусть *arr ищет сам", with the warning that *arr
        may pick a different release. The scope is the narrowest one the
        release names: a season pack → that season; music → that album.
        """
        action = ActionLog(
            user_id=0,
            action_type=ActionType.GRAB,
            content_type=content_type,
            content_id=str(arr_id),
            release_title=release.title,
        )
        log = logger.bind(arr_id=arr_id, release_title=release.title[:80])
        try:
            # `self.radarr`/`self.sonarr`/`self.lidarr`: these commands live
            # on the concrete clients, not on ArrBaseClient.
            if content_type is ContentType.MOVIE:
                await self.radarr.search_movie(arr_id)
            elif content_type is ContentType.MUSIC:
                if self.lidarr is None:
                    action.success = False
                    action.error_message = "Lidarr не настроен"
                    return False, action
                await self.lidarr.search_album(arr_id)
            elif release.is_season_pack and release.detected_season is not None:
                await self.sonarr.search_season(arr_id, release.detected_season)
            else:
                await self.sonarr.search_series(arr_id)
        except Exception as e:
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
                # No *arr grabs this one, so library_watch will never announce
                # it — the tag keeps its qBit completion message (final review).
                tags=[DIRECT_TAG],
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
        monitored: bool = True,
        tags: Optional[list[int]] = None,
    ) -> tuple[Optional[ArtistInfo], ActionLog]:
        """Add an artist to Lidarr.

        `monitored=False` — путь пикера альбомов: артист нужен только затем,
        чтобы узнать его дискографию и спросить релизы, и не должен попадать в
        RSS-петлю Lidarr до того, как что-то реально взяли. Ровно так же
        `_resolve_arr_entry` добавляет фильм/сериал.
        """
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
                monitored=monitored,
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
