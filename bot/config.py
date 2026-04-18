"""Application configuration using pydantic-settings."""

from functools import lru_cache
from typing import Annotated, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Telegram
    telegram_bot_token: str = Field(..., min_length=1, description="Telegram bot token from @BotFather")
    # NoDecode: skip pydantic-settings' JSON parsing so "1,2" stays a string
    # for our comma-separated validator below (see BUG fix for ValidationError
    # on pydantic-settings >= 2.13).
    allowed_tg_ids: Annotated[list[int], NoDecode] = Field(
        default_factory=list, description="Allowed Telegram user IDs"
    )
    admin_tg_ids: Annotated[list[int], NoDecode] = Field(
        default_factory=list, description="Admin Telegram user IDs"
    )

    # Prowlarr
    prowlarr_url: str = Field(..., min_length=1, description="Prowlarr base URL")
    prowlarr_api_key: str = Field(..., min_length=1, description="Prowlarr API key")

    # Radarr
    radarr_url: str = Field(..., min_length=1, description="Radarr base URL")
    radarr_api_key: str = Field(..., min_length=1, description="Radarr API key")

    # Sonarr
    sonarr_url: str = Field(..., min_length=1, description="Sonarr base URL")
    sonarr_api_key: str = Field(..., min_length=1, description="Sonarr API key")

    # Lidarr (optional, music)
    lidarr_url: Optional[str] = Field(default=None, description="Lidarr base URL")
    lidarr_api_key: Optional[str] = Field(default=None, description="Lidarr API key")

    # Deezer (optional, public API for music trending/discovery — no key required)
    deezer_enabled: bool = Field(default=True, description="Enable Deezer public API for music trending")

    # qBittorrent (optional)
    qbittorrent_url: Optional[str] = Field(default=None, description="qBittorrent Web UI URL")
    qbittorrent_username: str = Field(default="admin", description="qBittorrent username")
    qbittorrent_password: Optional[str] = Field(default=None, description="qBittorrent password")
    qbittorrent_timeout: float = Field(default=30.0, ge=5.0, description="qBittorrent request timeout in seconds")

    # Emby (optional)
    emby_url: Optional[str] = Field(default=None, description="Emby server URL")
    emby_api_key: Optional[str] = Field(default=None, description="Emby API key")
    emby_timeout: float = Field(default=30.0, ge=5.0, description="Emby request timeout in seconds")

    # TMDb (optional, for trending/popular content)
    tmdb_api_key: Optional[str] = Field(default=None, description="TMDb API key (v3) for trending/popular content")
    tmdb_language: str = Field(default="ru-RU", description="TMDb language for content (ru-RU, en-US, etc.)")
    tmdb_proxy_url: Optional[str] = Field(default=None, description="HTTP proxy URL for TMDb requests (e.g. http://vps:8899)")

    # Notifications
    notify_download_complete: bool = Field(default=True, description="Notify when download completes")
    notify_check_interval: int = Field(default=60, ge=10, le=3600, description="Check interval for notifications (seconds)")

    # Optional
    timezone: str = Field(default="Europe/Moscow", description="Timezone for timestamps")
    log_level: str = Field(default="INFO", description="Logging level")
    database_path: str = Field(default="data/bot.db", description="SQLite database path")
    auto_grab_score_threshold: int = Field(
        default=80, ge=0, le=100, description="Score threshold for auto-grab suggestion"
    )

    # HTTP client settings
    http_timeout: float = Field(default=30.0, description="HTTP request timeout in seconds")

    # Pagination
    results_per_page: int = Field(default=5, ge=1, le=10, description="Search results per page")

    @field_validator("allowed_tg_ids", "admin_tg_ids", mode="before")
    @classmethod
    def parse_comma_separated_ids(cls, v: str | list[int] | None) -> list[int]:
        """Parse comma-separated string of IDs into list of integers."""
        if v is None or v == "":
            return []
        # pydantic-settings may JSON-decode env values; "123" becomes int(123).
        # Also avoid treating booleans as integers.
        if isinstance(v, bool):
            return []
        if isinstance(v, int):
            return [v]
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            ids = []
            for x in v.split(","):
                x = x.strip()
                if x:
                    try:
                        ids.append(int(x))
                    except ValueError:
                        raise ValueError(f"Некорректный Telegram ID: {x!r}. Должно быть целое число.")
            return ids
        return []

    @field_validator("prowlarr_url", "radarr_url", "sonarr_url", mode="after")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        """Remove trailing slash from URLs."""
        return v.rstrip("/")

    @field_validator("qbittorrent_url", "emby_url", "lidarr_url", mode="after")
    @classmethod
    def strip_trailing_slash_optional(cls, v: Optional[str]) -> Optional[str]:
        """Remove trailing slash from optional URLs."""
        if v is not None:
            return v.rstrip("/")
        return v

    @property
    def qbittorrent_enabled(self) -> bool:
        """Check if qBittorrent integration is configured."""
        return self.qbittorrent_url is not None and self.qbittorrent_password is not None

    @property
    def emby_enabled(self) -> bool:
        """Check if Emby integration is configured."""
        return self.emby_url is not None and self.emby_api_key is not None

    @property
    def tmdb_enabled(self) -> bool:
        """Check if TMDb integration is configured."""
        return self.tmdb_api_key is not None

    @property
    def lidarr_enabled(self) -> bool:
        """Check if Lidarr integration is configured."""
        return self.lidarr_url is not None and self.lidarr_api_key is not None

    def is_user_allowed(self, user_id: int) -> bool:
        """Check if user is in the allowlist."""
        return user_id in self.allowed_tg_ids or user_id in self.admin_tg_ids

    def is_admin(self, user_id: int) -> bool:
        """Check if user is an admin."""
        return user_id in self.admin_tg_ids


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
