"""Application configuration using pydantic-settings."""

import warnings
from functools import lru_cache
from typing import Annotated, Literal, Optional

from pydantic import Field, field_validator, model_validator
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

    # Prowlarr — release search across every configured indexer.
    prowlarr_url: str = Field(..., min_length=1, description="Prowlarr base URL")
    prowlarr_api_key: str = Field(..., min_length=1, description="Prowlarr API key")
    # A search fans out to every indexer. RuTracker sits behind Cloudflare and
    # answers 521/522 under load, so 25s (the pre-migration default) timed out
    # routinely — raised to 45s in round 4 and kept here as the code default.
    prowlarr_search_timeout: float = Field(
        default=45.0, ge=5.0, le=120.0, description="Prowlarr search timeout in seconds"
    )
    prowlarr_search_retries: int = Field(
        default=1, ge=0, le=3,
        description="Extra attempts on a Prowlarr timeout (0=none, 1=one retry). "
                    "A flaky tracker often succeeds on the second attempt.",
    )

    # Radarr — movie catalog.
    radarr_url: str = Field(..., min_length=1, description="Radarr base URL")
    radarr_api_key: str = Field(..., min_length=1, description="Radarr API key")

    # Sonarr — series and anime catalog (anime is seriesType=anime, not a
    # separate service: the live Sonarr holds both in the same root folders).
    sonarr_url: str = Field(..., min_length=1, description="Sonarr base URL")
    sonarr_api_key: str = Field(..., min_length=1, description="Sonarr API key")

    # Lidarr (optional, music)
    lidarr_url: Optional[str] = Field(default=None, description="Lidarr base URL")
    lidarr_api_key: Optional[str] = Field(default=None, description="Lidarr API key")

    # slskd (optional, Soulseek) — the download client behind Lidarr, also
    # usable directly from the bot for album/track searches Lidarr can't express.
    slskd_url: Optional[str] = Field(default=None, description="slskd base URL")
    slskd_api_key: Optional[str] = Field(default=None, description="slskd API key (X-API-KEY)")
    slskd_timeout: float = Field(default=30.0, ge=5.0, description="slskd request timeout in seconds")
    slskd_search_timeout: float = Field(
        default=25.0, ge=5.0, le=120.0,
        description="How long to wait for a Soulseek search to gather responses (seconds)",
    )

    # Navidrome (optional) — read-only "is this already in the library?" probe
    # so the bot doesn't queue a duplicate album.
    navidrome_url: Optional[str] = Field(default=None, description="Navidrome base URL")
    navidrome_username: Optional[str] = Field(default=None, description="Navidrome username")
    navidrome_password: Optional[str] = Field(default=None, description="Navidrome password")
    navidrome_timeout: float = Field(default=15.0, ge=5.0, description="Navidrome request timeout in seconds")

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

    # TorrServer (optional) — streaming contour: "watch now" instead of
    # "have it in the library". Separate from Scryer on purpose.
    torrserver_url: Optional[str] = Field(default=None, description="TorrServer base URL")
    torrserver_username: Optional[str] = Field(default=None, description="TorrServer basic-auth user")
    torrserver_password: Optional[str] = Field(default=None, description="TorrServer basic-auth password")
    torrserver_timeout: float = Field(default=30.0, ge=5.0, description="TorrServer request timeout in seconds")
    # A torznab search fans out to every configured Prowlarr indexer, so it is
    # far slower than the plain API calls — same split as scryer_search_timeout.
    torrserver_search_timeout: float = Field(
        default=60.0, ge=10.0, le=300.0, description="TorrServer torznab search timeout (seconds)"
    )
    # How long to wait for a freshly added torrent to report its files. Without
    # them the Emby sync would publish an empty release.
    torrserver_metadata_timeout: float = Field(
        default=30.0, ge=5.0, le=180.0, description="How long to wait for torrent metadata (seconds)"
    )

    # Emby sync hook on Homeserver — the bot's container has neither ssh nor
    # curl, so the forced `.strm` sync is reached over HTTP.
    emby_sync_hook_url: Optional[str] = Field(default=None, description="Emby sync hook base URL")
    emby_sync_hook_token: Optional[str] = Field(default=None, description="Emby sync hook token")
    emby_sync_hook_timeout: float = Field(
        default=90.0, ge=5.0, le=600.0, description="Emby sync hook timeout (seconds)"
    )

    # TMDb (optional, for trending/popular content)
    tmdb_api_key: Optional[str] = Field(default=None, description="TMDb API key (v3) for trending/popular content")
    tmdb_language: str = Field(default="ru-RU", description="TMDb language for content (ru-RU, en-US, etc.)")
    tmdb_proxy_url: Optional[str] = Field(default=None, description="HTTP proxy URL for TMDb requests (e.g. http://vps:8899)")

    # Notifications
    notify_download_complete: bool = Field(default=True, description="Notify when download completes")
    notify_admins_on_start: bool = Field(
        default=True,
        description="Send admins a 'bot is up' card on startup (matches the other bots' behaviour)",
    )
    notify_check_interval: int = Field(default=60, ge=10, le=3600, description="Check interval for notifications (seconds)")

    # Optional
    timezone: str = Field(default="Europe/Moscow", description="Timezone for timestamps")
    log_level: str = Field(default="INFO", description="Logging level")
    # OBS-13: rendering format is independent of verbosity — LOG_LEVEL=DEBUG in
    # prod must not silently switch the log stream to non-JSON ConsoleRenderer.
    log_format: Literal["json", "console"] = Field(
        default="json", description="Log rendering format (json for prod/docker logs, console for local dev)"
    )
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

    @field_validator(
        "qbittorrent_url", "emby_url", "lidarr_url", "slskd_url", "navidrome_url",
        "torrserver_url", "emby_sync_hook_url", mode="after",
    )
    @classmethod
    def strip_trailing_slash_optional(cls, v: Optional[str]) -> Optional[str]:
        """Remove trailing slash from optional URLs."""
        if v is not None:
            return v.rstrip("/")
        return v

    @field_validator(
        "torrserver_url", "torrserver_username", "torrserver_password",
        "emby_sync_hook_url", "emby_sync_hook_token", mode="before",
    )
    @classmethod
    def _empty_ts_field_is_unset(cls, v: Optional[str]) -> Optional[str]:
        """Compose passes ``TORRSERVER_URL: ${TORRSERVER_URL:-}`` — with the
        var unset in the shell/host env, Compose injects an *empty string*,
        not "absence". Without this, ``env_ignore_empty`` being off (its
        repo-wide default, left alone here on purpose — flipping it changes
        behaviour for every other optional integration too) means the field
        becomes ``""`` rather than ``None``, so ``torrserver_enabled`` /
        ``emby_sync_hook_enabled`` read True with a blank URL/password.
        Scoped to only the fields this feature added — the other optional
        integrations (Emby, qBittorrent, Lidarr, slskd, Navidrome, TMDb) are
        deliberately left as-is; that's a separate, repo-wide change.
        """
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @model_validator(mode="after")
    def _warn_on_inconsistent_integration_config(self) -> "Settings":
        """LOGIC-09: half-configured integrations fail silently (`*_enabled`
        properties just go False) — an operator with a typo'd env var gets no
        signal beyond "why doesn't Lidarr search work". These are warnings,
        not ValueErrors: an inconsistent optional integration must not stop
        the bot from starting (structlog isn't configured yet at this stage,
        so we use the stdlib `warnings` module).
        """
        if (self.lidarr_url is None) != (self.lidarr_api_key is None):
            warnings.warn(
                "Lidarr partially configured: both LIDARR_URL and LIDARR_API_KEY "
                "are required — Lidarr integration will stay disabled.",
                stacklevel=2,
            )
        if (self.emby_url is None) != (self.emby_api_key is None):
            warnings.warn(
                "Emby partially configured: both EMBY_URL and EMBY_API_KEY are "
                "required — Emby integration will stay disabled.",
                stacklevel=2,
            )
        if (self.slskd_url is None) != (self.slskd_api_key is None):
            warnings.warn(
                "slskd partially configured: both SLSKD_URL and SLSKD_API_KEY "
                "are required — Soulseek search will stay disabled.",
                stacklevel=2,
            )
        if self.navidrome_url is not None and not self.navidrome_enabled:
            warnings.warn(
                "Navidrome partially configured: NAVIDROME_URL, NAVIDROME_USERNAME "
                "and NAVIDROME_PASSWORD are all required — the "
                "'already in library' check will stay disabled.",
                stacklevel=2,
            )
        if (self.qbittorrent_url is None) != (self.qbittorrent_password is None):
            warnings.warn(
                "qBittorrent partially configured: both QBITTORRENT_URL and "
                "QBITTORRENT_PASSWORD are required — qBittorrent integration "
                "will stay disabled.",
                stacklevel=2,
            )
        if self.notify_download_complete and not self.qbittorrent_enabled:
            warnings.warn(
                "NOTIFY_DOWNLOAD_COMPLETE=true but qBittorrent is not fully "
                "configured — download-completion notifications will never fire.",
                stacklevel=2,
            )
        ts_parts = (self.torrserver_url, self.torrserver_username, self.torrserver_password)
        if any(p is not None for p in ts_parts) and not self.torrserver_enabled:
            warnings.warn(
                "TorrServer partially configured: TORRSERVER_URL, TORRSERVER_USERNAME "
                "and TORRSERVER_PASSWORD are all required — the TorrServer section "
                "will stay disabled.",
                stacklevel=2,
            )
        if (self.emby_sync_hook_url is None) != (self.emby_sync_hook_token is None):
            warnings.warn(
                "Emby sync hook partially configured: both EMBY_SYNC_HOOK_URL and "
                "EMBY_SYNC_HOOK_TOKEN are required.",
                stacklevel=2,
            )
        if self.torrserver_enabled and not self.emby_sync_hook_enabled:
            warnings.warn(
                "TorrServer настроен без хука синхронизации: раздачи будут "
                "добавляться, но в Emby попадут только штатной задачей раз в "
                "10 минут.",
                stacklevel=2,
            )
        return self

    @property
    def qbittorrent_enabled(self) -> bool:
        """Check if qBittorrent integration is configured."""
        return self.qbittorrent_url is not None and self.qbittorrent_password is not None

    @property
    def emby_enabled(self) -> bool:
        """Check if Emby integration is configured."""
        return self.emby_url is not None and self.emby_api_key is not None

    @property
    def torrserver_enabled(self) -> bool:
        """Check if TorrServer integration is configured."""
        return (
            self.torrserver_url is not None
            and self.torrserver_username is not None
            and self.torrserver_password is not None
        )

    @property
    def emby_sync_hook_enabled(self) -> bool:
        """Check if the forced Emby sync hook is configured."""
        return self.emby_sync_hook_url is not None and self.emby_sync_hook_token is not None

    @property
    def tmdb_enabled(self) -> bool:
        """Check if TMDb integration is configured."""
        return self.tmdb_api_key is not None

    @property
    def lidarr_enabled(self) -> bool:
        """Check if Lidarr integration is configured."""
        return self.lidarr_url is not None and self.lidarr_api_key is not None

    @property
    def slskd_enabled(self) -> bool:
        """Check if slskd (Soulseek) integration is configured."""
        return self.slskd_url is not None and self.slskd_api_key is not None

    @property
    def navidrome_enabled(self) -> bool:
        """Check if Navidrome integration is configured."""
        return (
            self.navidrome_url is not None
            and self.navidrome_username is not None
            and self.navidrome_password is not None
        )

    @property
    def music_enabled(self) -> bool:
        """Whether any music backend is usable at all."""
        return self.lidarr_enabled or self.slskd_enabled

    def is_user_allowed(self, user_id: int) -> bool:
        """Check if user is in the allowlist."""
        return user_id in self.allowed_tg_ids or user_id in self.admin_tg_ids

    def is_admin(self, user_id: int) -> bool:
        """Check if user is an admin."""
        return user_id in self.admin_tg_ids


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    # Every field is populated from the environment/.env by pydantic-settings;
    # mypy only sees the model signature and thinks they are missing.
    return Settings()  # type: ignore[call-arg]
