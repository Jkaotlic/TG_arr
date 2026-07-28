"""Data models for the application."""

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

    ANIME is a first-class facet in Scryer (own library, own `1080p` quality
    profile) rather than a flavour of SERIES, so it gets its own member.
    """

    MOVIE = "movie"
    SERIES = "series"
    ANIME = "anime"
    MUSIC = "music"
    UNKNOWN = "unknown"

    @property
    def scryer_facet(self) -> Optional[str]:
        """The `MediaFacetValue` Scryer expects, or None for non-video types."""
        return _SCRYER_FACETS.get(self)

    @classmethod
    def from_scryer_facet(cls, facet: Optional[str]) -> "ContentType":
        """Map a Scryer `MediaFacetValue` (any case) back to a ContentType."""
        if not facet:
            return cls.UNKNOWN
        return _FACET_TO_CONTENT_TYPE.get(facet.upper(), cls.UNKNOWN)


_SCRYER_FACETS: dict["ContentType", str] = {}
_FACET_TO_CONTENT_TYPE: dict[str, "ContentType"] = {}


def _init_facet_maps() -> None:
    pairs = (
        (ContentType.MOVIE, "MOVIE"),
        (ContentType.SERIES, "SERIES"),
        (ContentType.ANIME, "ANIME"),
    )
    for content_type, facet in pairs:
        _SCRYER_FACETS[content_type] = facet
        _FACET_TO_CONTENT_TYPE[facet] = content_type


_init_facet_maps()

#: Content types Scryer handles (i.e. everything except music/unknown).
VIDEO_CONTENT_TYPES = (ContentType.MOVIE, ContentType.SERIES, ContentType.ANIME)


class ActionType(str, Enum):
    """Type of action performed."""

    SEARCH = "search"
    ADD = "add"
    GRAB = "grab"
    ERROR = "error"


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
    """Normalized search result from Prowlarr."""

    guid: str = Field(..., description="Unique identifier for the release")
    indexer: str = Field(default="Unknown", description="Indexer name")
    indexer_id: int = Field(default=0, description="Indexer ID in Prowlarr")
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

    # Scoring
    prowlarr_score: Optional[int] = Field(default=None, description="Original Prowlarr score")
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
    """Movie information from a Scryer metadata search or catalog entry."""

    content_model_type: Literal["movie"] = "movie"
    # Migration 2026-07-28: Scryer's metadata search keys on its own id
    # (`metadata_id`, surfaced as `tvdbId` in the GraphQL payload) and does not
    # always carry a TMDb id, so `tmdb_id`/`year` are no longer required. They
    # stay for TMDb-sourced trending items and the external-link keyboards.
    tmdb_id: int = Field(default=0, description="TMDB ID (0 when unknown)")
    imdb_id: Optional[str] = Field(default=None, description="IMDB ID")
    title: str = Field(..., description="Movie title")
    original_title: Optional[str] = Field(default=None)
    year: Optional[int] = Field(default=None, description="Release year")
    overview: Optional[str] = Field(default=None, description="Plot summary")
    runtime: Optional[int] = Field(default=None, description="Runtime in minutes")
    studio: Optional[str] = Field(default=None)
    genres: list[str] = Field(default_factory=list)
    poster_url: Optional[str] = Field(default=None)
    fanart_url: Optional[str] = Field(default=None)
    ratings: dict[str, Any] = Field(default_factory=dict)

    # Scryer-specific
    scryer_id: Optional[str] = Field(default=None, description="Title id in Scryer if already in the catalog")
    metadata_id: Optional[str] = Field(default=None, description="Scryer metadata id (`tvdbId` in searchMetadata)")
    slug: Optional[str] = Field(default=None, description="Latin-script slug — matched against when the title is localised")
    library_id: Optional[str] = Field(default=None, description="Scryer library id")
    quality_tier: Optional[str] = Field(default=None, description="Quality profile name applied by Scryer")
    current_quality_tier: Optional[str] = Field(default=None, description="Quality actually on disk")
    monitored: bool = Field(default=False)
    is_available: bool = Field(default=False, description="Whether movie is available")
    has_file: bool = Field(default=False, description="Whether movie file exists")
    quality_profile_id: Optional[str] = Field(default=None)
    root_folder_path: Optional[str] = Field(default=None)


class SeriesInfo(BaseModel):
    """Series (or anime) information from Scryer.

    Anime rides this model too — Scryer models it as a separate *facet*, not a
    separate entity shape. `facet` carries "SERIES"/"ANIME" so callers can route
    add/search to the right library without a second lookup.
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

    # Scryer-specific
    scryer_id: Optional[str] = Field(default=None, description="Title id in Scryer if already in the catalog")
    metadata_id: Optional[str] = Field(default=None, description="Scryer metadata id (`tvdbId` in searchMetadata)")
    slug: Optional[str] = Field(default=None, description="Latin-script slug — matched against when the title is localised")
    facet: str = Field(default="SERIES", description="Scryer MediaFacetValue: SERIES or ANIME")
    library_id: Optional[str] = Field(default=None, description="Scryer library id")
    quality_tier: Optional[str] = Field(default=None, description="Quality profile name applied by Scryer")
    current_quality_tier: Optional[str] = Field(default=None, description="Quality actually on disk")
    monitored: bool = Field(default=False)
    has_file: bool = Field(default=False, description="Whether at least one episode file exists")
    episodes_owned: int = Field(default=0)
    episodes_total: int = Field(default=0)
    quality_profile_id: Optional[str] = Field(default=None)
    root_folder_path: Optional[str] = Field(default=None)
    seasons: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def content_type(self) -> ContentType:
        """The ContentType matching this title's Scryer facet."""
        return ContentType.from_scryer_facet(self.facet)


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
# Scryer models (migration 2026-07-28)
# ============================================================================


class IndexerStat(BaseModel):
    """Per-indexer 24h counters from Scryer's `systemHealth.indexerStats`."""

    name: str
    queries_24h: int = 0
    successful_24h: int = 0
    failed_24h: int = 0

    @property
    def failure_rate(self) -> float:
        """Share of failed queries in the last 24h (0.0 when idle)."""
        total = self.successful_24h + self.failed_24h
        return (self.failed_24h / total) if total else 0.0


class ScryerHealth(BaseModel):
    """Snapshot of Scryer's `systemHealth` query."""

    service_ready: bool = False
    total_titles: int = 0
    monitored_titles: int = 0
    titles_movie: int = 0
    titles_series: int = 0
    titles_anime: int = 0
    version: Optional[str] = None
    indexers: list[IndexerStat] = Field(default_factory=list)


class ScryerQueueItem(BaseModel):
    """One row of Scryer's `downloadQueue`."""

    id: str
    title_id: Optional[str] = None
    episode_id: Optional[str] = None
    title_name: str = "?"
    content_type: ContentType = ContentType.UNKNOWN
    state: str = "UNKNOWN"
    display_state: str = "UNKNOWN"
    progress_percent: int = 0
    size_bytes: Optional[int] = None
    remaining_seconds: Optional[int] = None
    queued_at: Optional[datetime] = None
    client_name: Optional[str] = None
    attention_required: bool = False
    attention_reason: Optional[str] = None
    import_status: Optional[str] = None
    download_id: Optional[str] = None

    @property
    def size_formatted(self) -> str:
        return format_bytes(self.size_bytes) if self.size_bytes else "N/A"

    @property
    def eta_formatted(self) -> str:
        """Human-readable ETA; "∞" when Scryer has no estimate."""
        if self.remaining_seconds is None or self.remaining_seconds < 0:
            return "∞"
        hours, remainder = divmod(self.remaining_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 24:
            return f"{hours // 24}d {hours % 24}h"
        if hours:
            return f"{hours}h {minutes}m"
        if minutes:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"


class ScryerCalendarItem(BaseModel):
    """One row of Scryer's `calendarEpisodes`."""

    id: str
    title_id: str
    title_name: str
    content_type: ContentType = ContentType.SERIES
    season_number: Optional[int] = None
    episode_number: Optional[int] = None
    episode_title: Optional[str] = None
    air_date: Optional[str] = None
    monitored: bool = True


class ScryerWantedItem(BaseModel):
    """One row of Scryer's `wantedItems`."""

    id: str
    title_id: str
    title_name: str = "?"
    content_type: ContentType = ContentType.UNKNOWN
    season_number: Optional[int] = None
    episode_number: Optional[int] = None
    status: Optional[str] = None
    media_type: Optional[str] = None


class QueueResult(BaseModel):
    """Outcome of a queue mutation (`QueueDownloadPayload`)."""

    status: str = "QUEUED"
    job_id: Optional[str] = None
    title_id: Optional[str] = None
    title_name: Optional[str] = None
    conflict: Optional[dict[str, Any]] = None

    @property
    def queued(self) -> bool:
        return self.status == "QUEUED"


class AddTitleOutcome(BaseModel):
    """Outcome of `addTitle` / `addTitleAndQueueDownload` (`AddTitleResult`)."""

    title: Union[MovieInfo, SeriesInfo] = Field(..., description="The added or reused title")
    reused_existing: bool = False
    download_job_id: Optional[str] = None
    queued_download: Optional[QueueResult] = None

    @property
    def queued(self) -> bool:
        """True when Scryer actually put a download in the queue."""
        if self.queued_download is not None:
            return self.queued_download.queued
        return self.download_job_id is not None


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
    # detection (search_service.detect_with_confidence), so handle_release_selection/
    # _execute_grab can reuse them instead of repeating the same lookup up to
    # 2 more times per grab. None when detection didn't run or produced no
    # usable lookup (music winner, low-confidence, timeout, etc).
    lookup_candidates: Optional[list[ContentInfo]] = None
    # Feature #2: user-chosen Sonarr season-monitoring preset (all/future/
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
