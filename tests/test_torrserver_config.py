"""Конфигурация раздела TorrServer: свойства-переключатели и предупреждения
о полу-настроенной интеграции (LOGIC-09-стиль, как у Lidarr/Emby/qBittorrent)."""

import warnings

import pytest


def _settings(**overrides):
    from bot.config import Settings

    return Settings(**overrides)


def test_torrserver_disabled_by_default():
    s = _settings(notify_download_complete=False)
    assert s.torrserver_enabled is False


def test_torrserver_fully_configured_enables():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        s = _settings(
            torrserver_url="http://192.168.0.95:8090",
            torrserver_username="admin",
            torrserver_password="pw",
            emby_sync_hook_url="http://192.168.0.95:8099",
            emby_sync_hook_token="tok",
            notify_download_complete=False,
        )
    assert s.torrserver_enabled is True
    assert s.emby_sync_hook_enabled is True


def test_torrserver_without_password_warns():
    with pytest.warns(UserWarning, match="TorrServer"):
        s = _settings(torrserver_url="http://ts:8090", torrserver_username="admin",
                      notify_download_complete=False)
    assert s.torrserver_enabled is False


def test_torrserver_without_hook_warns_about_delayed_emby():
    with pytest.warns(UserWarning, match="синхронизац"):
        _settings(
            torrserver_url="http://ts:8090", torrserver_username="admin",
            torrserver_password="pw", notify_download_complete=False,
        )


def test_torrserver_url_trailing_slash_stripped():
    s = _settings(
        torrserver_url="http://ts:8090/", torrserver_username="admin",
        torrserver_password="pw", emby_sync_hook_url="http://ts:8099/",
        emby_sync_hook_token="tok", notify_download_complete=False,
    )
    assert s.torrserver_url == "http://ts:8090"
    assert s.emby_sync_hook_url == "http://ts:8099"
