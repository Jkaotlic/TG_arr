"""Data models for the application."""

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Discriminator, Field, Tag, field_validator


class ContentType(str, Enum):
    """Type of content being searched."""

    MOVIE = "movie"
    SERIES = "series"
    UNKNOWN = "unknown"


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
        size = float(self.size)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if abs(size) < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} PB"

    def get_size_gb(self) -> float:
        """Get size in gigabytes."""
        return self.size / (1024 ** 3)

    class Config:
        """Pydantic config."""

        from_attributes = True


class MovieInfo(BaseModel):
    """Movie information from Radarr lookup."""

    content_model_type: Literal["movie"] = "movie"
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

    # Radarr-specific
    radarr_id: Optional[int] = Field(default=None, description="ID in Radarr if already added")
    is_available: bool = Field(default=False, description="Whether movie is available")
    has_file: bool = Field(default=False, description="Whether movie file exists")
    quality_profile_id: Optional[int] = Field(default=None)
    root_folder_path: Optional[str] = Field(default=None)


class SeriesInfo(BaseModel):
    """Series information from Sonarr lookup."""

    content_model_type: Literal["series"] = "series"
    tvdb_id: int = Field(..., description="TVDB ID")
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

    # Sonarr-specific
    sonarr_id: Optional[int] = Field(default=None, description="ID in Sonarr if already added")
    quality_profile_id: Optional[int] = Field(default=None)
    root_folder_path: Optional[str] = Field(default=None)
    seasons: list[dict[str, Any]] = Field(default_factory=list)


class QualityProfile(BaseModel):
    """Quality profile from Radarr/Sonarr."""

    id: int
    name: str


class RootFolder(BaseModel):
    """Root folder from Radarr/Sonarr."""

    id: int
    path: str
    free_space: Optional[int] = Field(default=None)

    @property
    def free_space_formatted(self) -> str:
        """Return human-readable free space."""
        if self.free_space is None:
            return "N/A"
        size = self.free_space
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if abs(size) < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} PB"


class UserPreferences(BaseModel):
    """User preferences stored in database."""

    radarr_quality_profile_id: Optional[int] = None
    radarr_root_folder_id: Optional[int] = None
    sonarr_quality_profile_id: Optional[int] = None
    sonarr_root_folder_id: Optional[int] = None
    preferred_resolution: Optional[str] = None  # 1080p, 2160p, etc.
    auto_grab_enabled: bool = False
    language: str = "en"


class User(BaseModel):
    """User model."""

    tg_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    role: UserRole = UserRole.USER
    preferences: UserPreferences = Field(default_factory=UserPreferences)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# Union type with discriminator for content info
ContentInfo = Annotated[
    Union[
        Annotated[MovieInfo, Tag("movie")],
        Annotated[SeriesInfo, Tag("series")],
    ],
    Discriminator("content_model_type"),
]


class SearchSession(BaseModel):
    """Active search session for a user."""

    user_id: int
    query: str
    content_type: ContentType
    results: list[SearchResult] = Field(default_factory=list)
    current_page: int = 0
    selected_result: Optional[SearchResult] = None
    selected_content: Optional[ContentInfo] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # For series - season/episode selection
    selected_season: Optional[int] = None
    selected_episodes: list[int] = Field(default_factory=list)
    monitor_type: str = "all"  # all, future, missing, existing, pilot, firstSeason, latestSeason, none


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
    created_at: datetime = Field(default_factory=datetime.utcnow)


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
        if self.eta is None or self.eta < 0 or self.eta == 8640000:
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

    # Session stats
    total_downloaded: int = Field(default=0)
    total_uploaded: int = Field(default=0)

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


def format_bytes(size: int) -> str:
    """Format bytes to human-readable string."""
    if size == 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if abs(size) < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} EB"


def format_speed(bytes_per_sec: int) -> str:
    """Format speed to human-readable string."""
    if bytes_per_sec == 0:
        return "0 B/s"
    for unit in ["B/s", "KB/s", "MB/s", "GB/s"]:
        if abs(bytes_per_sec) < 1024.0:
            return f"{bytes_per_sec:.1f} {unit}"
        bytes_per_sec /= 1024.0
    return f"{bytes_per_sec:.1f} TB/s"
