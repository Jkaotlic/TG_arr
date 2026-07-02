"""LOGIC-09: Settings.model_validator warns (does not raise) on inconsistent
optional-integration configuration, plus the OBS-13/SEC-02 fields that ride
along with it (log_format, webhook_token, webhook_bind default)."""

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


def test_webhook_enabled_without_token_warns():
    with pytest.warns(UserWarning, match="WEBHOOK_TOKEN"):
        _settings(webhook_enabled=True)


def test_webhook_enabled_with_token_does_not_warn_about_token():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _settings(webhook_enabled=True, webhook_token="secret")
    assert not any("WEBHOOK_TOKEN" in str(w.message) for w in caught)


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


# --- SEC-02: webhook_bind default is loopback, not 0.0.0.0 -----------------


def test_webhook_bind_defaults_to_loopback():
    assert _settings().webhook_bind == "127.0.0.1"


def test_webhook_token_defaults_to_none():
    assert _settings().webhook_token is None
