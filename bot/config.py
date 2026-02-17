"""Application configuration using pydantic-settings."""

from functools import lru_cache
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    allowed_tg_ids: list[int] = Field(default_factory=list, description="Allowed Telegram user IDs")
    admin_tg_ids: list[int] = Field(default_factory=list, description="Admin Telegram user IDs")

    # Prowlarr
    prowlarr_url: str = Field(..., min_length=1, description="Prowlarr base URL")
    prowlarr_api_key: str = Field(..., min_length=1, description="Prowlarr API key")

    # Radarr
    radarr_url: str = Field(..., min_length=1, description="Radarr base URL")
    radarr_api_key: str = Field(..., min_length=1, description="Radarr API key")

    # Sonarr
    sonarr_url: str = Field(..., min_length=1, description="Sonarr base URL")
    sonarr_api_key: str = Field(..., min_length=1, description="Sonarr API key")

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

    # Notifications
    notify_download_complete: bool = Field(default=True, description="Notify when download completes")
    notify_check_interval: int = Field(default=60, ge=10, description="Check interval for notifications (seconds)")

    # Optional
    timezone: str = Field(default="Europe/Moscow", description="Timezone for timestamps")
    log_level: str = Field(default="INFO", description="Logging level")
    database_path: str = Field(default="data/bot.db", description="SQLite database path")
    auto_grab_score_threshold: int = Field(
        default=80, ge=0, le=100, description="Score threshold for auto-grab suggestion"
    )

    # HTTP client settings
    http_timeout: float = Field(default=30.0, description="HTTP request timeout in seconds")
    http_max_retries: int = Field(default=3, description="Maximum HTTP retry attempts")

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
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return []

    @field_validator("prowlarr_url", "radarr_url", "sonarr_url", mode="after")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        """Remove trailing slash from URLs."""
        return v.rstrip("/")

    @field_validator("qbittorrent_url", "emby_url", mode="after")
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
