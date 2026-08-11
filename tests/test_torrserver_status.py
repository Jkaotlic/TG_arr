"""TorrServer должен попадать в общую карточку /status.

Task 12: TorrServer в /status и в списке команд. The status fan-out already
lives in `bot.handlers.status._collect_statuses` (LOGIC-17, shared by
cmd_status/cmd_health) — this only adds a TorrServer branch to it, guarded by
`get_torrserver()` returning None when the integration is not configured.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.handlers import status as status_handlers


def _quiet_arr_client() -> MagicMock:
    """Radarr/Sonarr/Prowlarr are always checked in `_collect_statuses`; give
    each a clean, successful `check_connection()` so their SystemStatus never
    interferes with the TorrServer-focused assertions below."""
    client = MagicMock()
    client.check_connection = AsyncMock(return_value=(True, "1.0.0", 5.0))
    return client


@pytest.mark.asyncio
async def test_status_includes_torrserver_when_configured():
    client = MagicMock()
    client.check_connection = AsyncMock(return_value=(True, "MatriX.142.2", 12.0))

    with patch.object(status_handlers, "get_radarr", AsyncMock(return_value=_quiet_arr_client())), \
         patch.object(status_handlers, "get_sonarr", AsyncMock(return_value=_quiet_arr_client())), \
         patch.object(status_handlers, "get_prowlarr", AsyncMock(return_value=_quiet_arr_client())), \
         patch.object(status_handlers, "get_lidarr", AsyncMock(return_value=None)), \
         patch.object(status_handlers, "get_qbittorrent", AsyncMock(return_value=None)), \
         patch.object(status_handlers, "get_emby", AsyncMock(return_value=None)), \
         patch.object(status_handlers, "get_torrserver", new_callable=AsyncMock,
                      return_value=client):
        statuses = await status_handlers._collect_statuses(include_deezer=False)

    names = [s.service for s in statuses]
    assert "TorrServer" in names
    ts_status = next(s for s in statuses if s.service == "TorrServer")
    assert ts_status.available is True
    assert ts_status.version == "MatriX.142.2"
    assert ts_status.response_time_ms == 12.0


@pytest.mark.asyncio
async def test_status_skips_torrserver_when_not_configured():
    with patch.object(status_handlers, "get_radarr", AsyncMock(return_value=_quiet_arr_client())), \
         patch.object(status_handlers, "get_sonarr", AsyncMock(return_value=_quiet_arr_client())), \
         patch.object(status_handlers, "get_prowlarr", AsyncMock(return_value=_quiet_arr_client())), \
         patch.object(status_handlers, "get_lidarr", AsyncMock(return_value=None)), \
         patch.object(status_handlers, "get_qbittorrent", AsyncMock(return_value=None)), \
         patch.object(status_handlers, "get_emby", AsyncMock(return_value=None)), \
         patch.object(status_handlers, "get_torrserver", new_callable=AsyncMock,
                      return_value=None) as get_ts_mock:
        statuses = await status_handlers._collect_statuses(include_deezer=False)

    assert "TorrServer" not in [s.service for s in statuses]
    # get_torrserver() itself must still be consulted (to know whether it's
    # configured) but nothing beyond that — no check_connection() call happens
    # when it returns None, i.e. no network attempt for an unconfigured service.
    get_ts_mock.assert_awaited_once()


def test_ts_command_is_advertised():
    from bot.ui.commands import bot_commands

    assert any(c.command == "ts" for c in bot_commands())
