"""Final branch review (2026-09-24): settings added on this branch must reach
the container (docker-compose passes env one by one, no env_file), and the
empty string Compose injects for an unset ``${X:-}`` must mean "unset".
"""

import warnings
from pathlib import Path

import pytest
import structlog

from bot.models import QualityProfile, UserPreferences

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
NEW_VARS = (
    "RADARR_DEFAULT_PROFILE_ID",
    "SONARR_DEFAULT_PROFILE_ID",
    "MUSIC_NOTIFY_SETTLE_SECONDS",
    "MUSIC_NOTIFY_STALL_SECONDS",
    "ARR_TORRENT_CATEGORIES",
)


def _settings(**overrides):
    from bot.config import Settings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return Settings(notify_download_complete=False, **overrides)


def test_compose_passes_the_new_settings_through():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    env = compose["services"]["tg-arr-bot"]["environment"]
    for name in NEW_VARS:
        assert name in env, f"{name} missing from docker-compose.yml environment"


@pytest.mark.parametrize("field", ["radarr_default_profile_id", "sonarr_default_profile_id"])
def test_empty_profile_id_is_unset(field):
    assert getattr(_settings(**{field: ""}), field) is None
    assert getattr(_settings(**{field: "  "}), field) is None
    assert getattr(_settings(**{field: "7"}), field) == 7


def test_empty_profile_id_from_env_is_unset(monkeypatch):
    monkeypatch.setenv("RADARR_DEFAULT_PROFILE_ID", "")
    monkeypatch.setenv("SONARR_DEFAULT_PROFILE_ID", "7")
    s = _settings()
    assert s.radarr_default_profile_id is None
    assert s.sonarr_default_profile_id == 7


def test_empty_tuning_values_fall_back_to_defaults(monkeypatch):
    for name in ("MUSIC_NOTIFY_SETTLE_SECONDS", "MUSIC_NOTIFY_STALL_SECONDS", "ARR_TORRENT_CATEGORIES"):
        monkeypatch.setenv(name, "")
    s = _settings()
    assert s.music_notify_settle_seconds == 300
    assert s.music_notify_stall_seconds == 3600
    assert s.arr_torrent_category_set == {"radarr", "tv-sonarr", "sonarr"}


def test_set_tuning_values_are_used(monkeypatch):
    monkeypatch.setenv("MUSIC_NOTIFY_SETTLE_SECONDS", "60")
    monkeypatch.setenv("MUSIC_NOTIFY_STALL_SECONDS", "7200")
    s = _settings()
    assert (s.music_notify_settle_seconds, s.music_notify_stall_seconds) == (60, 7200)


# ------------------------------------------------ unknown configured profile
PROFILES = [QualityProfile(id=1, name="Any"), QualityProfile(id=4, name="HD-1080p")]


def test_settings_shows_unknown_configured_profile_id():
    from bot.ui.formatters import Formatters

    text = Formatters.format_user_preferences(
        UserPreferences(), PROFILES, [], PROFILES, [],
        radarr_default=(PROFILES[0], "first"), sonarr_default=(PROFILES[1], "name"),
        radarr_config_id=7, sonarr_config_id=4,
    )
    assert "RADARR_DEFAULT_PROFILE_ID=7" in text
    assert "нет среди профилей Radarr" in text
    assert "нет среди профилей Sonarr" not in text


def test_known_configured_profile_id_has_no_warning():
    from bot.ui.formatters import Formatters

    text = Formatters.format_user_preferences(
        UserPreferences(), PROFILES, [], PROFILES, [],
        radarr_default=(PROFILES[1], "config"), sonarr_default=(PROFILES[1], "config"),
        radarr_config_id=4, sonarr_config_id=4,
    )
    assert "нет среди профилей" not in text


def test_unknown_configured_profile_id_is_logged():
    from bot.handlers.settings import warn_unknown_default_profile

    with structlog.testing.capture_logs() as logs:
        assert warn_unknown_default_profile(PROFILES, 7, "RADARR_DEFAULT_PROFILE_ID") is True
        assert warn_unknown_default_profile(PROFILES, 4, "RADARR_DEFAULT_PROFILE_ID") is False
        assert warn_unknown_default_profile(PROFILES, None, "RADARR_DEFAULT_PROFILE_ID") is False
        assert warn_unknown_default_profile([], 7, "RADARR_DEFAULT_PROFILE_ID") is False
    warned = [e for e in logs if e["event"] == "default_profile_id_unknown"]
    assert len(warned) == 1
    assert warned[0]["log_level"] == "warning"
    assert warned[0]["setting"] == "RADARR_DEFAULT_PROFILE_ID"
    assert warned[0]["profile_id"] == 7
