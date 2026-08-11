"""Data models for the application."""

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, field_validator


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# LOGIC-22: qBittorrent's API returns this exact sentinel value for eta when
# a torrent is stalled/queued with no seeders — i.e. "infinite" ETA. Named so
# the comparison below is self-explanatory instead of a bare magic number.
QBIT_INFINITE_ETA = 8640000


class ContentType(str, Enum):
    """Type of content being searched.

    ANIME is not a separate service or library — rollback 2026-08-10 measured
    the live Sonarr holding 15 `seriesType=standard` series and 1 `anime`, all
    in the same root folders. It still gets its own member because search/UI
    routing cares about the distinction, but it maps onto Sonarr's single
    `seriesType` string rather than a facet of its own.
    """

    MOVIE = "movie"
    SERIES = "series"
    ANIME = "anime"
    MUSIC = "music"
    UNKNOWN = "unknown"

    @property
    def sonarr_series_type(self) -> Optional[str]:
        """Sonarr's `seriesType`, or None for types Sonarr does not hold.

        Rollback 2026-08-10: anime is not a separate service or library — the
        live Sonarr keeps 15 standard series and 1 anime in the same root
        folders, so the facet collapses to this one field.
        """
        return _SONARR_SERIES_TYPES.get(self)

    @classmethod
    def from_sonarr_series_type(cls, value: Optional[str]) -> "ContentType":
        """Map a Sonarr `seriesType` (any case) back to a ContentType."""
        if not value:
            return cls.UNKNOWN
        return _SERIES_TYPE_TO_CONTENT_TYPE.get(value.lower(), cls.UNKNOWN)


_SONARR_SERIES_TYPES: dict["ContentType", str] = {
    ContentType.SERIES: "standard",
    ContentType.ANIME: "anime",
}
_SERIES_TYPE_TO_CONTENT_TYPE: dict[str, "ContentType"] = {
    "standard": ContentType.SERIES,
    "anime": ContentType.ANIME,
}

#: Content types the *arr stack handles (i.e. everything except music/unknown).
VIDEO_CONTENT_TYPES = (ContentType.MOVIE, ContentType.SERIES, ContentType.ANIME)


class ActionType(str, Enum):
    """Type of action performed."""

    SEARCH = "search"
    ADD = "add"
    GRAB = "grab"
    ERROR = "error"
    # Added 2026-07-30: monitoring toggles and catalog removals were both being
    # logged as ADD, so /history showed "добавлено" for a deletion.
    MONITOR = "monitor"
    DELETE = "delete"


class UserRole(str, Enum):
    """User role in the system."""

    USER = "user"
    ADMIN = "admin"


class QualityInfo(BaseModel):
    """Parsed quality information from release title."""

    resolution: Optional[str] = None  # 720p, 1080p, 2160p, etc.
    source: Optional[str] = None  # BluRay, WEB-DL, HDTV, CAM, etc.
    codec: Optional[str] = None  # x264, x265, HEVC, AV1, etc.
    hdr: Optional[str] = None  # HDR, HDR10, DV (Dolby Vision), etc.
    audio: Optional[str] = None  # DTS, Atmos, TrueHD, etc.
    subtitle: Optional[str] = None  # RusSub, MVO, DVO, etc.
    is_remux: bool = False
    is_repack: bool = False
    is_proper: bool = False


class SearchResult(BaseModel):
    """Normalized release candidate (from *arr's or Prowlarr's indexer search)."""

    guid: str = Field(..., description="Unique identifier for the release")
    indexer: str = Field(default="Unknown", description="Indexer name")
    # Rollback 2026-08-10 (Task 10): Optional, default None — distinguishes
    # a release from *arr's own interactive search (which always carries its
    # indexer id alongside the guid) from a Prowlarr free-text hit, which
    # doesn't. `AddService.grab_release` uses "is this set?" to pick the grab
    # path: set -> native `POST {prefix}/release`, unset -> the push/qBit/
    # auto-search fallback chain. NOTE: Prowlarr's OWN client
    # (bot/clients/prowlarr.py `_normalize_result`) still fills this with
    # PROWLARR's indexer numbering (untouched, out of this task's scope) —
    # that numbering is NOT interchangeable with *arr's, so a result built
    # from Prowlarr's free-text search must not be routed to the native path
    # by this field alone if that client ever stops leaving it unset.
    indexer_id: Optional[int] = Field(
        default=None, description="*arr's indexer id, paired with guid — required for the native grab path"
    )
    title: str = Field(..., description="Release title")
    size: int = Field(default=0, description="Size in bytes")
    seeders: Optional[int] = Field(default=None, description="Number of seeders")
    leechers: Optional[int] = Field(default=None, description="Number of leechers")
    protocol: str = Field(default="torrent", description="Protocol: torrent or usenet")
    download_url: Optional[str] = Field(default=None, description="Direct download URL")
    info_url: Optional[str] = Field(default=None, description="Info page URL")
    magnet_url: Optional[str] = Field(default=None, description="Magnet URL if available")
    publish_date: Optional[datetime] = Field(default=None, description="Publication date")
    categories: list[int] = Field(default_factory=list, description="Category IDs")
    category_names: list[str] = Field(default_factory=list, description="Category names")

    # Parsed quality info
    quality: QualityInfo = Field(default_factory=QualityInfo)

    # Rollback 2026-08-10: *arr's interactive search returns releases it has
    # already judged against the user's quality profile and custom formats.
    # That verdict is authoritative — the local scoring only breaks ties.
    # Prowlarr's free-text path leaves these at their defaults.
    custom_format_score: int = Field(default=0, description="*arr customFormatScore")
    rejected: bool = Field(default=False, description="*arr refuses this release")
    rejections: list[str] = Field(default_factory=list, description="Why *arr refuses it, verbatim")
    languages: list[str] = Field(default_factory=list, description="Languages *arr parsed")

    # Scoring
    prowlarr_score: Optional[int] = Field(default=None, description="Legacy Prowlarr score; unset since the Scryer migration")
    calculated_score: int = Field(default=0, description="Our calculated score")

    # Scryer release candidate (migration 2026-07-28). Scryer already applies
    # the quality profile and the Rego rules, so its verdict is authoritative
    # and the bot's own `calculated_score` is only a display/tie-break aid.
    scryer_title_id: Optional[str] = Field(default=None, description="Scryer title id this release belongs to")
    candidate_token: Optional[str] = Field(
        default=None,
        description="Short-lived Scryer token identifying this candidate. SECRET: embeds the "
                    "indexer download URL (with the tracker passkey) — never log it.",
    )
    queue_scope: Optional[dict[str, Any]] = Field(
        default=None, description="QueueDownloadScopeInput to reuse when queueing this candidate"
    )
    scryer_score: Optional[int] = Field(default=None, description="Scryer releaseScore (profile + rules)")
    scryer_preference_score: Optional[int] = Field(default=None, description="Scryer preferenceScore")
    scryer_allowed: Optional[bool] = Field(default=None, description="Whether Scryer's profile allows this release")
    block_codes: list[str] = Field(default_factory=list, description="Scryer block codes when not allowed")
    policy_codes: list[str] = Field(
        default_factory=list,
        description="Rule codes from Scryer's scoringLog (e.g. english_audio_bonus). Empty means "
                    "the policy said nothing — which is not the same as 'it said no'.",
    )
    auto_eligible: bool = Field(default=False, description="Scryer would grab this release automatically")
    auto_decision_summary: Optional[str] = Field(default=None)

    # Content detection
    detected_type: ContentType = Field(default=ContentType.UNKNOWN)
    detected_year: Optional[int] = Field(default=None)
    detected_season: Optional[int] = Field(default=None)
    detected_episode: Optional[int] = Field(default=None)
    is_season_pack: bool = Field(default=False)

    @property
    def size_formatted(self) -> str:
        """Return human-readable size."""
        if self.size == 0:
            return "N/A"
        return format_bytes(self.size)

    def get_size_gb(self) -> float:
        """Get size in gigabytes."""
        return self.size / (1024 ** 3)

    model_config = ConfigDict(from_attributes=True)


class MovieInfo(BaseModel):
    """Movie information from a TMDb search or Radarr catalog entry."""

    content_model_type: Literal["movie"] = "movie"
    # Rollback 2026-08-10: back to Radarr's shape. Radarr always has both a
    # tmdb_id and a year, and a movie can't be added to Radarr without one, so
    # these are required again (they were relaxed for Scryer's metadata search,
    # which keyed on its own id and didn't always carry a TMDb id).
    tmdb_id: int = Field(..., description="TMDB ID")
    imdb_id: Optional[str] = Field(default=None, description="IMDB ID")
    title: str = Field(..., description="Movie title")
    original_title: Optional[str] = Field(default=None)
    year: int = Field(..., description="Release year")
    overview: Optional[str] = Field(default=None, description="Plot summary")
    runtime: Optional[int] = Field(default=None, description="Runtime in minutes")
    studio: Optional[str] = Field(default=None)
    genres: list[str] = Field(default_factory=list)
    poster_url: Optional[str] = Field(default=None)
    fanart_url: Optional[str] = Field(default=None)
    ratings: dict[str, Any] = Field(default_factory=dict)

    radarr_id: Optional[int] = Field(default=None, description="Movie id in Radarr if already added")
    monitored: bool = Field(default=False)
    is_available: bool = Field(default=False, description="Whether movie is available")
    has_file: bool = Field(default=False, description="Whether movie file exists")
    quality_profile_id: Optional[int] = Field(default=None)
    root_folder_path: Optional[str] = Field(default=None)


class SeriesInfo(BaseModel):
    """Series (or anime) information from a TVDB search or Sonarr catalog entry.

    Anime rides this model too — rollback 2026-08-10 measured the live Sonarr
    holding both `seriesType=standard` and `seriesType=anime` entries in the
    same instance and root folders, so `series_type` is Sonarr's own field
    rather than a separate facet/library concept.
    """

    content_model_type: Literal["series"] = "series"
    tvdb_id: int = Field(default=0, description="TVDB ID (0 when unknown)")
    tmdb_id: Optional[int] = Field(default=None, description="TMDB ID")
    imdb_id: Optional[str] = Field(default=None, description="IMDB ID")
    title: str = Field(..., description="Series title")
    original_title: Optional[str] = Field(default=None)
    year: Optional[int] = Field(default=None, description="First air year")
    overview: Optional[str] = Field(default=None, description="Plot summary")
    runtime: Optional[int] = Field(default=None, description="Episode runtime in minutes")
    network: Optional[str] = Field(default=None)
    status: Optional[str] = Field(default=None, description="continuing, ended, etc.")
    genres: list[str] = Field(default_factory=list)
    poster_url: Optional[str] = Field(default=None)
    fanart_url: Optional[str] = Field(default=None)
    ratings: dict[str, Any] = Field(default_factory=dict)
    season_count: int = Field(default=0)
    total_episode_count: int = Field(default=0)

    sonarr_id: Optional[int] = Field(default=None, description="Series id in Sonarr if already added")
    series_type: str = Field(default="standard", description="Sonarr seriesType: standard or anime")
    monitored: bool = Field(default=False)
    has_file: bool = Field(default=False, description="Whether at least one episode file exists")
    episodes_owned: int = Field(default=0)
    episodes_total: int = Field(default=0)
    quality_profile_id: Optional[int] = Field(default=None)
    root_folder_path: Optional[str] = Field(default=None)
    seasons: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def content_type(self) -> ContentType:
        """The ContentType matching this title's Sonarr seriesType."""
        return ContentType.from_sonarr_series_type(self.series_type)


class ArtistInfo(BaseModel):
    """Artist information from Lidarr lookup."""

    content_model_type: Literal["artist"] = "artist"
    mb_id: str = Field(..., description="MusicBrainz ID (foreignArtistId)")
    name: str = Field(..., description="Artist name")
    sort_name: Optional[str] = Field(default=None)
    overview: Optional[str] = Field(default=None)
    disambiguation: Optional[str] = Field(default=None, description="MB disambiguation comment")
    artist_type: Optional[str] = Field(default=None, description="Person, Group, Orchestra, etc.")
    status: Optional[str] = Field(default=None, description="active, ended, etc.")
    genres: list[str] = Field(default_factory=list)
    poster_url: Optional[str] = Field(default=None)
    fanart_url: Optional[str] = Field(default=None)
    ratings: dict[str, Any] = Field(default_factory=dict)

    album_count: int = Field(default=0)
    track_count: int = Field(default=0)

    # Lidarr-specific
    lidarr_id: Optional[int] = Field(default=None, description="ID in Lidarr if already added")
    quality_profile_id: Optional[int] = Field(default=None)
    metadata_profile_id: Optional[int] = Field(default=None)
    root_folder_path: Optional[str] = Field(default=None)


class MetadataProfile(BaseModel):
    """Lidarr metadata profile (controls what album types are grabbed)."""

    id: int
    name: str


class QualityProfile(BaseModel):
    """Quality profile from Scryer or Lidarr.

    Scryer profile ids are slugs ("4k", "1080p"); Lidarr's are integers — hence
    the union type. Compare with `str(...)` when matching a stored preference.
    """

    id: Union[int, str]
    name: str


class RootFolder(BaseModel):
    """Root folder from Scryer or Lidarr.

    Scryer's `RootFolderPayload` has no id of its own — the path *is* the
    identity, so the client mirrors the path into `id`.
    """

    id: Union[int, str]
    path: str
    free_space: Optional[int] = Field(default=None)
    is_default: bool = Field(default=False)

    @property
    def free_space_formatted(self) -> str:
        """Return human-readable free space."""
        if self.free_space is None:
            return "N/A"
        return format_bytes(self.free_space)


# ============================================================================
# Music models — slskd (Soulseek) and Navidrome
# ============================================================================


class SlskdFile(BaseModel):
    """One shared file offered by a Soulseek peer."""

    filename: str = Field(..., description="Full remote path (Windows-style separators)")
    name: str = Field(default="", description="Basename for display")
    size: int = Field(default=0)
    extension: str = Field(default="")
    bitrate: Optional[int] = Field(default=None)
    bit_depth: Optional[int] = Field(default=None)
    sample_rate: Optional[int] = Field(default=None)
    length_seconds: Optional[int] = Field(default=None)

    @property
    def size_formatted(self) -> str:
        return format_bytes(self.size)


class SlskdSearchResult(BaseModel):
    """Audio files from one peer, grouped by their shared parent folder.

    Soulseek has no album entity — this grouping is what makes a result
    downloadable as a unit rather than track by track.
    """

    username: str
    folder: str = ""
    files: list[SlskdFile] = Field(default_factory=list)
    has_free_slot: bool = False
    queue_length: int = 0
    upload_speed: int = 0

    @property
    def track_count(self) -> int:
        return len(self.files)

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def size_formatted(self) -> str:
        return format_bytes(self.total_size)

    @property
    def dominant_format(self) -> str:
        """Most common extension in the group ("flac", "mp3", ...)."""
        counts: dict[str, int] = {}
        for f in self.files:
            if f.extension:
                counts[f.extension] = counts.get(f.extension, 0) + 1
        return max(counts, key=lambda k: counts[k]) if counts else ""

    @property
    def guessed_artist(self) -> str:
        """Artist guessed from the folder name ("Artist - Album (2024)").

        Deliberately crude: Soulseek folder naming is a free-for-all, and this
        only feeds the "does this look like music?" heuristic.
        """
        folder = self.folder or ""
        if " - " in folder:
            return folder.split(" - ", 1)[0].strip()
        return folder.strip()

    @property
    def display_name(self) -> str:
        return self.folder or (self.files[0].name if self.files else self.username)


class SlskdTransfer(BaseModel):
    """One in-flight (or failed) slskd download."""

    username: str
    filename: str = ""
    state: str = "Unknown"
    size: int = 0
    transferred: int = 0
    average_speed: float = 0.0

    @property
    def progress_percent(self) -> int:
        return int(self.transferred / self.size * 100) if self.size else 0

    @property
    def is_errored(self) -> bool:
        return "Errored" in self.state or "Rejected" in self.state or "Cancelled" in self.state

    @property
    def speed_formatted(self) -> str:
        return format_speed(self.average_speed)


class NavidromeAlbum(BaseModel):
    """One album known to Navidrome (i.e. already on disk)."""

    id: str
    name: str
    artist: str = ""
    year: Optional[int] = None
    song_count: int = 0


class UserPreferences(BaseModel):
    """User preferences stored in database.

    Migration 2026-07-28: the per-*arr profile/folder preferences collapsed into
    one Scryer pair. Scryer ids are slugs/paths, so these are strings now; the
    Lidarr trio stays integer-keyed. Legacy `radarr_*`/`sonarr_*` keys still
    present in stored JSON are ignored (extra keys are dropped by pydantic).
    """

    scryer_quality_profile_id: Optional[str] = None
    scryer_root_folder_id: Optional[str] = None
    lidarr_quality_profile_id: Optional[int] = None
    lidarr_metadata_profile_id: Optional[int] = None
    lidarr_root_folder_id: Optional[int] = None
    preferred_resolution: Optional[str] = None  # 1080p, 2160p, etc.
    auto_grab_enabled: bool = False

    @field_validator("scryer_quality_profile_id", "scryer_root_folder_id", mode="before")
    @classmethod
    def _coerce_scryer_id(cls, v):
        """Scryer ids are slugs/paths, but a stored preference may still be a
        number (a value written before the migration, or a numeric-looking
        profile id). Coerce rather than reject: a ValidationError here makes
        `_row_to_user` fall back to defaults and silently drop every other
        preference the user had set.
        """
        if v is None or isinstance(v, str):
            return v
        return str(v)


class User(BaseModel):
    """User model."""

    tg_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    role: UserRole = UserRole.USER
    preferences: UserPreferences = Field(default_factory=UserPreferences)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


# Union type with discriminator for content info
# DEAD-09: AlbumInfo was removed from this union — Lidarr's _parse_album (its
# only producer) had no production callers (no album-grab flow exists yet).
ContentInfo = Annotated[
    Union[
        Annotated[MovieInfo, Tag("movie")],
        Annotated[SeriesInfo, Tag("series")],
        Annotated[ArtistInfo, Tag("artist")],
    ],
    Discriminator("content_model_type"),
]


class SearchSession(BaseModel):
    """Active search session for a user."""

    user_id: int
    query: str
    content_type: ContentType
    results: list[SearchResult] = Field(default_factory=list, max_length=500)
    current_page: int = 0
    selected_result: Optional[SearchResult] = None
    selected_content: Optional[ContentInfo] = None
    # LOGIC-06: full Radarr/Sonarr lookup candidates captured during content-type
    # detection (search_service.detect_content_type), so handle_release_selection/
    # _execute_grab can reuse them instead of repeating the same lookup up to
    # 2 more times per grab. None when detection didn't run or produced no
    # usable lookup (music winner, low-confidence, timeout, etc).
    lookup_candidates: Optional[list[ContentInfo]] = None
    # Feature #2: user-chosen season-monitoring preset (all/future/
    # latestSeason/firstSeason/none); None → auto-decide from the release.
    monitor_type: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)


class ActionLog(BaseModel):
    """Action log entry."""

    id: Optional[int] = None
    user_id: int
    action_type: ActionType
    content_type: ContentType
    query: Optional[str] = None
    content_title: Optional[str] = None
    content_id: Optional[str] = None  # TMDB/TVDB ID
    release_title: Optional[str] = None
    success: bool = True
    error_message: Optional[str] = None
    details: Optional[str] = Field(default=None, description="JSON blob: rejections, fallback_used, etc.")
    created_at: datetime = Field(default_factory=_utcnow)


class SystemStatus(BaseModel):
    """System status information."""

    service: str
    available: bool
    version: Optional[str] = None
    error: Optional[str] = None
    response_time_ms: Optional[float] = None


# ============================================================================
# qBittorrent Models
# ============================================================================


class TorrentState(str, Enum):
    """State of a torrent in qBittorrent."""

    DOWNLOADING = "downloading"
    SEEDING = "seeding"
    PAUSED = "paused"
    QUEUED = "queued"
    STALLED = "stalled"
    CHECKING = "checking"
    ERROR = "error"
    COMPLETED = "completed"
    MOVING = "moving"
    UNKNOWN = "unknown"


class TorrentFilter(str, Enum):
    """Filters for torrent list."""

    ALL = "all"
    DOWNLOADING = "downloading"
    SEEDING = "seeding"
    COMPLETED = "completed"
    PAUSED = "paused"
    ACTIVE = "active"
    INACTIVE = "inactive"
    STALLED = "stalled"
    ERRORED = "errored"


class TorrentInfo(BaseModel):
    """Information about a torrent."""

    hash: str = Field(..., description="Torrent hash")
    name: str = Field(..., description="Torrent name")
    size: int = Field(default=0, description="Total size in bytes")
    progress: float = Field(default=0.0, description="Progress 0.0 to 1.0")
    download_speed: int = Field(default=0, description="Download speed in bytes/s")
    upload_speed: int = Field(default=0, description="Upload speed in bytes/s")
    eta: Optional[int] = Field(default=None, description="ETA in seconds, -1 if unknown")
    state: TorrentState = Field(default=TorrentState.UNKNOWN)
    category: Optional[str] = Field(default=None, description="Category name")
    tags: list[str] = Field(default_factory=list)
    added_on: Optional[datetime] = Field(default=None)
    completion_on: Optional[datetime] = Field(default=None)
    save_path: str = Field(default="", description="Save path")

    # Peer info
    seeds: int = Field(default=0, description="Connected seeds")
    seeds_total: int = Field(default=0, description="Total seeds in swarm")
    peers: int = Field(default=0, description="Connected peers")
    peers_total: int = Field(default=0, description="Total peers in swarm")

    # Ratio
    ratio: float = Field(default=0.0)
    uploaded: int = Field(default=0, description="Uploaded bytes")
    downloaded: int = Field(default=0, description="Downloaded bytes")

    # Tracker
    tracker: Optional[str] = Field(default=None)

    @property
    def progress_percent(self) -> int:
        """Get progress as percentage."""
        return int(self.progress * 100)

    @property
    def eta_formatted(self) -> str:
        """Get human-readable ETA."""
        if self.eta is None or self.eta < 0 or self.eta == QBIT_INFINITE_ETA:
            return "∞"
        if self.eta == 0:
            return "0s"
        hours, remainder = divmod(self.eta, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 24:
            days = hours // 24
            hours = hours % 24
            return f"{days}d {hours}h"
        if hours > 0:
            return f"{hours}h {minutes}m"
        elif minutes > 0:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"

    @property
    def size_formatted(self) -> str:
        """Get human-readable size."""
        return format_bytes(self.size)

    @property
    def download_speed_formatted(self) -> str:
        """Get human-readable download speed."""
        return format_speed(self.download_speed)

    @property
    def upload_speed_formatted(self) -> str:
        """Get human-readable upload speed."""
        return format_speed(self.upload_speed)

    @property
    def state_emoji(self) -> str:
        """Get emoji for current state."""
        return {
            TorrentState.DOWNLOADING: "⬇️",
            TorrentState.SEEDING: "⬆️",
            TorrentState.PAUSED: "⏸️",
            TorrentState.QUEUED: "⏳",
            TorrentState.STALLED: "⚠️",
            TorrentState.CHECKING: "🔍",
            TorrentState.ERROR: "❌",
            TorrentState.COMPLETED: "✅",
            TorrentState.MOVING: "📦",
            TorrentState.UNKNOWN: "❓",
        }.get(self.state, "❓")


class QBittorrentStatus(BaseModel):
    """Global qBittorrent status."""

    version: str = Field(default="unknown")
    connection_status: str = Field(default="unknown")

    # Transfer info
    download_speed: int = Field(default=0)
    upload_speed: int = Field(default=0)
    download_limit: int = Field(default=0, description="0 = unlimited")
    upload_limit: int = Field(default=0, description="0 = unlimited")

    # Disk
    free_space: int = Field(default=0)

    # Queue
    active_downloads: int = Field(default=0)
    active_uploads: int = Field(default=0)
    total_torrents: int = Field(default=0)
    paused_torrents: int = Field(default=0)

    # DHT
    dht_nodes: int = Field(default=0)

    @property
    def download_speed_formatted(self) -> str:
        return format_speed(self.download_speed)

    @property
    def upload_speed_formatted(self) -> str:
        return format_speed(self.upload_speed)

    @property
    def free_space_formatted(self) -> str:
        return format_bytes(self.free_space)


# ============================================================================
# Utility functions
# ============================================================================


def format_bytes(size: int | float) -> str:
    """Format bytes to human-readable string."""
    if size == 0:
        return "0 B"
    value = float(size)
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if abs(value) < 1024.0:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} EB"


def format_speed(bytes_per_sec: int | float) -> str:
    """Format speed to human-readable string."""
    if bytes_per_sec == 0:
        return "0 B/s"
    value = float(bytes_per_sec)
    for unit in ["B/s", "KB/s", "MB/s", "GB/s"]:
        if abs(value) < 1024.0:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB/s"


# ============================================================================
# TorrServer Models and Parsers
# ============================================================================

#: Extensions Emby will actually play from a `.strm`; everything else in a
#: release (subtitles, posters, BDMV service files) is noise for the user.
VIDEO_FILE_EXTENSIONS = (".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mov", ".wmv")

#: TorrServer renders sizes as e.g. "2.5 GCiB" — a number, a binary-prefix
#: letter, then filler. Only the number and the prefix letter carry meaning.
_TS_SIZE_RE = re.compile(r"([\d]+(?:[.,][\d]+)?)\s*([KMGTP])?", re.IGNORECASE)
_TS_SIZE_POWERS = {"k": 1, "m": 2, "g": 3, "t": 4, "p": 5}


def parse_torrserver_size(value: "str | int | float | None") -> int:
    """Bytes from TorrServer's search-result size.

    The torznab search returns the size as a *string* ("2.5 GCiB"), unlike the
    torrent API which returns plain bytes — so both shapes are accepted.
    Anything unparseable is 0: a wrong size must never abort a search.
    """
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    match = _TS_SIZE_RE.search(str(value))
    if not match:
        return 0
    number = float(match.group(1).replace(",", "."))
    prefix = (match.group(2) or "").lower()
    return int(number * (1024 ** _TS_SIZE_POWERS.get(prefix, 0)))


def sanitize_torrent_title(title: str, max_length: int = 200) -> str:
    """Title safe to use as a directory name in TorrServer's virtual FS.

    A slash in a release title (routine on Russian trackers: "Холодное сердце 2 /
    Frozen II [...]") makes `PROPFIND` on its category return 500 with no
    entries at all — the folder looks empty in WebDAV and DLNA. TorrServer has a
    scheduled sanitizer, but it runs every 15 minutes; adding an already-clean
    title means the release is browsable immediately.
    """
    cleaned = re.sub(r"[\\/]+", " - ", title or "")
    cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_length]


class TorrServerFile(BaseModel):
    """One file inside a torrent."""

    id: int
    path: str
    length: int = 0


class TorrServerRelease(BaseModel):
    """A search hit from TorrServer's torznab endpoint."""

    title: str
    size: int = 0
    seeders: int = 0
    peers: int = 0
    link: str = ""
    tracker: str = ""
    year: Optional[int] = None


class TorrServerTorrent(BaseModel):
    """A torrent known to TorrServer."""

    hash: str
    title: str
    category: str = ""
    poster: str = ""
    size: int = 0
    stat: int = 0
    stat_string: str = ""
    files: list[TorrServerFile] = Field(default_factory=list)

    @property
    def video_files(self) -> list[TorrServerFile]:
        """Playable files, largest first."""
        videos = [f for f in self.files if f.path.lower().endswith(VIDEO_FILE_EXTENSIONS)]
        return sorted(videos, key=lambda f: f.length, reverse=True)


class TorrServerStats(BaseModel):
    """Snapshot for the status card."""

    version: str = "unknown"
    torrent_count: int = 0
    total_size: int = 0
    cache_size: int = 0
    use_disk: bool = False
    source_count: int = 0


class SyncHookResult(BaseModel):
    """Outcome of a forced Emby sync request.

    `status` is one of "ok" (sync ran), "already_running" (a pass was in
    flight, ours was folded into it) or "failed" (the hook could not be
    reached or refused) — a failure here never invalidates the torrent that
    was already added.
    """

    status: str = "failed"
    duration_s: Optional[float] = None
    error: Optional[str] = None
