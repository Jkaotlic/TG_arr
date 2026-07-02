"""Tests for bot/handlers/status.py and bot/handlers/emby.py.

Covers (Task D):
- SEC-04: _format_health must html.escape version/service/path before
  interpolating into an HTML-parsed message.
- LOGIC-17: cmd_status/cmd_health share _collect_statuses.
- LOGIC-20: show_emby_status/_render_status_text — no isinstance-branching
  duplication; (text, keyboard) tuple contract.
- BUG-04c: emby scan/restart/update handlers call callback.answer() exactly
  once even though they also re-render the status card.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.models import SystemStatus


def _make_callback(data: str = "cb") -> MagicMock:
    cb = MagicMock()
    cb.data = data
    cb.answer = AsyncMock()
    message = MagicMock()
    message.edit_text = AsyncMock()
    cb.message = message
    return cb


# ---------------------------------------------------------------------------
# SEC-04 — status.py: _format_health HTML-escapes service/version/path
# ---------------------------------------------------------------------------


def test_format_health_escapes_malicious_version_string():
    """RED: a version string containing HTML must not break out of <code>."""
    from bot.handlers.status import _format_health

    statuses = [
        SystemStatus(service="Radarr", available=True, version="<img src=x onerror=alert(1)>"),
    ]
    text = _format_health(statuses, [], None)

    assert "<img src=x" not in text
    assert "&lt;img src=x" in text


def test_format_health_escapes_malicious_service_name():
    """RED: service name is normally a trusted literal, but must still be
    escaped defensively (SEC-04 explicitly calls out s.service)."""
    from bot.handlers.status import _format_health

    statuses = [
        SystemStatus(service="<b>Evil</b>", available=False),
    ]
    text = _format_health(statuses, [], None)

    assert "<b>Evil</b>" not in text
    assert "&lt;b&gt;Evil&lt;/b&gt;" in text


def test_format_health_escapes_malicious_disk_path():
    """RED: root-folder paths come from *arr config and are attacker-influenceable
    in a self-hosted setup; must be escaped."""
    from bot.handlers.status import _format_health

    disks = [("/movies/<script>alert(1)</script>", 1000)]
    text = _format_health([], disks, None)

    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_format_health_normal_values_render_unescaped_tags_intact():
    """Sanity: normal values still render with intended <b>/<code> markup
    (only the interpolated values are escaped, not our own tags)."""
    from bot.handlers.status import _format_health

    statuses = [SystemStatus(service="Radarr", available=True, version="5.1.0")]
    text = _format_health(statuses, [], None)

    assert "<b>Состояние системы</b>" in text
    assert "<code>5.1.0</code>" in text
    assert "<code>Radarr</code>" not in text  # service isn't wrapped in <code>, just escaped inline
    assert "Radarr" in text


# ---------------------------------------------------------------------------
# LOGIC-17 — status.py: _collect_statuses shared by cmd_status/cmd_health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collect_statuses_include_deezer_true_adds_deezer():
    from bot.handlers import status as status_handler

    deezer_client = AsyncMock()

    with patch.object(status_handler, "get_prowlarr", AsyncMock(return_value=AsyncMock())), \
         patch.object(status_handler, "get_radarr", AsyncMock(return_value=AsyncMock())), \
         patch.object(status_handler, "get_sonarr", AsyncMock(return_value=AsyncMock())), \
         patch.object(status_handler, "get_lidarr", AsyncMock(return_value=None)), \
         patch.object(status_handler, "get_qbittorrent", AsyncMock(return_value=None)), \
         patch.object(status_handler, "get_emby", AsyncMock(return_value=None)), \
         patch.object(status_handler, "get_deezer", AsyncMock(return_value=deezer_client)), \
         patch.object(status_handler, "check_service", AsyncMock(
             side_effect=lambda client, name: SystemStatus(service=name, available=True)
         )):
        statuses = await status_handler._collect_statuses(include_deezer=True)

    names = {s.service for s in statuses}
    assert "Deezer" in names
    assert "Prowlarr" in names and "Radarr" in names and "Sonarr" in names


@pytest.mark.asyncio
async def test_collect_statuses_include_deezer_false_omits_deezer():
    from bot.handlers import status as status_handler

    with patch.object(status_handler, "get_prowlarr", AsyncMock(return_value=AsyncMock())), \
         patch.object(status_handler, "get_radarr", AsyncMock(return_value=AsyncMock())), \
         patch.object(status_handler, "get_sonarr", AsyncMock(return_value=AsyncMock())), \
         patch.object(status_handler, "get_lidarr", AsyncMock(return_value=None)), \
         patch.object(status_handler, "get_qbittorrent", AsyncMock(return_value=None)), \
         patch.object(status_handler, "get_emby", AsyncMock(return_value=None)), \
         patch.object(status_handler, "get_deezer", AsyncMock()) as get_deezer_mock, \
         patch.object(status_handler, "check_service", AsyncMock(
             side_effect=lambda client, name: SystemStatus(service=name, available=True)
         )):
        statuses = await status_handler._collect_statuses(include_deezer=False)

    names = {s.service for s in statuses}
    assert "Deezer" not in names
    get_deezer_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_collect_statuses_exception_becomes_unknown_status():
    """A failed check_service() (unexpected exception, not caught internally)
    must degrade to an 'Unknown'/unavailable SystemStatus, not crash the gather."""
    from bot.handlers import status as status_handler

    async def flaky_check(client, name):
        if name == "Sonarr":
            raise RuntimeError("boom")
        return SystemStatus(service=name, available=True)

    with patch.object(status_handler, "get_prowlarr", AsyncMock(return_value=AsyncMock())), \
         patch.object(status_handler, "get_radarr", AsyncMock(return_value=AsyncMock())), \
         patch.object(status_handler, "get_sonarr", AsyncMock(return_value=AsyncMock())), \
         patch.object(status_handler, "get_lidarr", AsyncMock(return_value=None)), \
         patch.object(status_handler, "get_qbittorrent", AsyncMock(return_value=None)), \
         patch.object(status_handler, "get_emby", AsyncMock(return_value=None)), \
         patch.object(status_handler, "get_deezer", AsyncMock(return_value=None)), \
         patch.object(status_handler, "check_service", AsyncMock(side_effect=flaky_check)):
        statuses = await status_handler._collect_statuses(include_deezer=False)

    assert len(statuses) == 3  # Prowlarr, Radarr, Sonarr all yield a status
    unknown = [s for s in statuses if not s.available and s.service == "Unknown"]
    assert len(unknown) == 1


@pytest.mark.asyncio
async def test_cmd_status_and_cmd_health_both_use_collect_statuses():
    """Wiring check: both handlers delegate to the shared helper (LOGIC-17)
    rather than re-implementing the fan-out."""
    from bot.handlers import status as status_handler

    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    message = MagicMock()
    message.answer = AsyncMock(return_value=status_msg)

    with patch.object(status_handler, "_collect_statuses", AsyncMock(return_value=[])) as collect_mock, \
         patch.object(status_handler.Formatters, "format_system_status", return_value="TXT"):
        await status_handler.cmd_status(message)

    collect_mock.assert_awaited_once_with(include_deezer=True)

    with patch.object(status_handler, "_collect_statuses", AsyncMock(return_value=[])) as collect_mock2, \
         patch.object(status_handler, "get_radarr", AsyncMock(return_value=None)), \
         patch.object(status_handler, "get_sonarr", AsyncMock(return_value=None)), \
         patch.object(status_handler, "get_lidarr", AsyncMock(return_value=None)), \
         patch.object(status_handler, "get_qbittorrent", AsyncMock(return_value=None)):
        await status_handler.cmd_health(message)

    collect_mock2.assert_awaited_once_with(include_deezer=False)


# ---------------------------------------------------------------------------
# LOGIC-20 — emby.py: _render_status_text() + thin callers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_status_text_not_configured():
    from bot.handlers import emby as emby_handler

    with patch.object(emby_handler, "get_emby", AsyncMock(return_value=None)):
        text, keyboard = await emby_handler._render_status_text()

    assert "не настроен" in text
    assert keyboard is None


@pytest.mark.asyncio
async def test_render_status_text_success_returns_tuple():
    from bot.handlers import emby as emby_handler

    emby_client = AsyncMock()
    emby_client.get_server_info = AsyncMock(return_value=MagicMock(
        server_name="MyEmby", version="4.8", operating_system="Linux",
        has_pending_restart=False, has_update_available=False,
        can_self_restart=True, can_self_update=True,
    ))
    emby_client.get_libraries = AsyncMock(return_value=[])
    emby_client.get_sessions = AsyncMock(return_value=[])

    with patch.object(emby_handler, "get_emby", AsyncMock(return_value=emby_client)), \
         patch.object(emby_handler.Formatters, "format_emby_status", return_value="STATUS_TEXT"), \
         patch.object(emby_handler.Keyboards, "emby_main", return_value="KB"):
        text, keyboard = await emby_handler._render_status_text()

    assert text == "STATUS_TEXT"
    assert keyboard == "KB"


@pytest.mark.asyncio
async def test_cmd_emby_and_handle_refresh_both_use_render_status_text():
    """Wiring check: both entry points go through the shared renderer (LOGIC-20)."""
    from bot.handlers import emby as emby_handler

    message = MagicMock()
    message.answer = AsyncMock()

    with patch.object(emby_handler, "_render_status_text", AsyncMock(return_value=("T", "KB"))) as render_mock:
        await emby_handler.cmd_emby(message)
    render_mock.assert_awaited_once()
    message.answer.assert_awaited_once_with("T", reply_markup="KB", parse_mode="HTML")

    cb = _make_callback()
    with patch.object(emby_handler, "_render_status_text", AsyncMock(return_value=("T2", "KB2"))) as render_mock2:
        await emby_handler.handle_refresh(cb)
    render_mock2.assert_awaited_once()
    cb.message.edit_text.assert_awaited_once_with("T2", reply_markup="KB2", parse_mode="HTML")
    assert cb.answer.call_count == 1


# ---------------------------------------------------------------------------
# BUG-04c — emby.py: exactly one callback.answer() per callback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_scan_all_calls_answer_once():
    from bot.handlers import emby as emby_handler

    emby_client = AsyncMock()
    emby_client.scan_library = AsyncMock()
    emby_client.get_server_info = AsyncMock(return_value=MagicMock(
        server_name="E", version="1", operating_system="Linux",
        has_pending_restart=False, has_update_available=False,
        can_self_restart=True, can_self_update=True,
    ))
    emby_client.get_libraries = AsyncMock(return_value=[])
    emby_client.get_sessions = AsyncMock(return_value=[])

    cb = _make_callback()

    with patch.object(emby_handler, "get_emby", AsyncMock(return_value=emby_client)), \
         patch.object(emby_handler.Formatters, "format_emby_status", return_value="TXT"), \
         patch.object(emby_handler.Keyboards, "emby_main", return_value="KB"):
        await emby_handler.handle_scan_all(cb)

    assert cb.answer.call_count == 1, (
        f"callback.answer called {cb.answer.call_count} times (expected 1) — BUG-04c regression"
    )
    emby_client.scan_library.assert_awaited_once()
    cb.message.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_scan_movies_calls_answer_once():
    from bot.handlers import emby as emby_handler

    emby_client = AsyncMock()
    lib = MagicMock(collection_type="movies", id="lib1")
    emby_client.get_libraries = AsyncMock(return_value=[lib])
    emby_client.refresh_library = AsyncMock()
    emby_client.get_server_info = AsyncMock(return_value=MagicMock(
        server_name="E", version="1", operating_system="Linux",
        has_pending_restart=False, has_update_available=False,
        can_self_restart=True, can_self_update=True,
    ))
    emby_client.get_sessions = AsyncMock(return_value=[])

    cb = _make_callback()

    with patch.object(emby_handler, "get_emby", AsyncMock(return_value=emby_client)), \
         patch.object(emby_handler.Formatters, "format_emby_status", return_value="TXT"), \
         patch.object(emby_handler.Keyboards, "emby_main", return_value="KB"):
        await emby_handler.handle_scan_movies(cb)

    assert cb.answer.call_count == 1
    cb.message.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_restart_confirm_error_path_calls_answer_once():
    """Even the exception branch (which also re-renders status) must answer exactly once."""
    from bot.clients.emby import EmbyError
    from bot.handlers import emby as emby_handler

    emby_client = AsyncMock()
    emby_client.restart_server = AsyncMock(side_effect=EmbyError("nope"))
    emby_client.get_server_info = AsyncMock(return_value=MagicMock(
        server_name="E", version="1", operating_system="Linux",
        has_pending_restart=False, has_update_available=False,
        can_self_restart=True, can_self_update=True,
    ))
    emby_client.get_libraries = AsyncMock(return_value=[])
    emby_client.get_sessions = AsyncMock(return_value=[])

    cb = _make_callback()

    with patch.object(emby_handler, "get_emby", AsyncMock(return_value=emby_client)), \
         patch.object(emby_handler.Formatters, "format_emby_status", return_value="TXT"), \
         patch.object(emby_handler.Keyboards, "emby_main", return_value="KB"):
        await emby_handler.handle_restart_confirm(cb, is_admin=True)

    assert cb.answer.call_count == 1
    # _edit_status re-renders the card even on failure.
    cb.message.edit_text.assert_awaited_once()
