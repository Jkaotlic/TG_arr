"""LOGIC-09: Settings.model_validator warns (does not raise) on inconsistent
optional-integration configuration, plus the OBS-13/SEC-02 fields that ride
along with it (log_format, optional-integration consistency)."""

import warnings

import pytest


def _settings(**overrides):
    from bot.config import Settings

    return Settings(**overrides)


# --- LOGIC-09: partially configured integrations warn, don't raise --------


def test_lidarr_url_without_key_warns_but_does_not_raise():
    with pytest.warns(UserWarning, match="Lidarr"):
        s = _settings(lidarr_url="http://lidarr:8686")
    assert s.lidarr_enabled is False


def test_lidarr_fully_configured_does_not_warn():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        # notify_download_complete=False: isolate this test to the
        # Lidarr-specific warning branch, independent of the (unrelated)
        # qBittorrent-notification warning covered separately below.
        s = _settings(
            lidarr_url="http://lidarr:8686", lidarr_api_key="key", notify_download_complete=False
        )
    assert s.lidarr_enabled is True


def test_emby_api_key_without_url_warns():
    with pytest.warns(UserWarning, match="Emby"):
        _settings(emby_api_key="key")


def test_qbittorrent_password_without_url_warns():
    with pytest.warns(UserWarning, match="qBittorrent"):
        _settings(qbittorrent_password="pw")


def test_notify_enabled_without_qbittorrent_warns():
    with pytest.warns(UserWarning, match="NOTIFY_DOWNLOAD_COMPLETE"):
        s = _settings(notify_download_complete=True)
    assert s.qbittorrent_enabled is False


def test_fully_unconfigured_optional_integrations_do_not_warn():
    """The common case (nothing optional configured at all) must be silent —
    warnings are for *inconsistent* half-configuration, not "not configured"."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _settings(notify_download_complete=False)


# --- OBS-13: log_format independent of log_level ---------------------------


def test_log_format_defaults_to_json():
    assert _settings().log_format == "json"


def test_log_format_rejects_invalid_value():
    with pytest.raises(Exception):
        _settings(log_format="xml")


# --- the *arr webhook is gone (2026-07-29) ---------------------------------


def test_webhook_settings_are_gone():
    """The inbound *arr webhook server was removed with its services; library
    notifications now come from polling (bot/services/library_watcher.py), so
    the settings — and the network-facing port they configured — went too."""
    settings = _settings()
    for field in ("webhook_enabled", "webhook_port", "webhook_bind", "webhook_token"):
        assert not hasattr(settings, field)


# --- Rollback 2026-08-10: restore *arr, remove Scryer -----------------------


def test_arr_settings_are_required_and_scryer_is_gone(monkeypatch):
    """*arr credentials are mandatory; the Scryer fields no longer exist."""
    for name, value in (
        ("PROWLARR_URL", "http://prowlarr:9696/"),
        ("PROWLARR_API_KEY", "pk"),
        ("RADARR_URL", "http://radarr:7878"),
        ("RADARR_API_KEY", "rk"),
        ("SONARR_URL", "http://sonarr:8989"),
        ("SONARR_API_KEY", "sk"),
    ):
        monkeypatch.setenv(name, value)
    for stale in ("SCRYER_URL", "SCRYER_USERNAME", "SCRYER_PASSWORD"):
        monkeypatch.delenv(stale, raising=False)

    from bot.config import Settings

    settings = Settings()

    # Trailing slash is stripped so endpoint joins never double up.
    assert settings.prowlarr_url == "http://prowlarr:9696"
    assert settings.radarr_api_key == "rk"
    assert settings.sonarr_url == "http://sonarr:8989"
    # RuTracker behind Cloudflare answers 521/522; 25s was not enough (round 4).
    assert settings.prowlarr_search_timeout == 45.0
    assert not hasattr(settings, "scryer_url")


def test_missing_radarr_key_is_a_validation_error(monkeypatch):
    """A half-configured *arr must fail loudly at startup, not at first search."""
    import pydantic

    for name, value in (
        ("PROWLARR_URL", "http://p"), ("PROWLARR_API_KEY", "pk"),
        ("RADARR_URL", "http://r"),
        ("SONARR_URL", "http://s"), ("SONARR_API_KEY", "sk"),
    ):
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("RADARR_API_KEY", raising=False)

    from bot.config import Settings

    with pytest.raises(pydantic.ValidationError):
        Settings()
