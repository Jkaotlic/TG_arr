"""Startup notification to admins (2026-07-29).

Matches the shape the user's other bots use (BookBot posts a "🚀 … запущен!"
card with counts and config on start), adapted to what actually matters here:
which backends answered the warm-up probe.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.main import build_startup_message, notify_admins_on_start


def _warmup(**overrides):
    summary = {
        "lidarr": (True, 407.2, "3.1.2.4938"),
        "slskd": (True, 268.7, "0.24.5.0"),
    }
    summary.update(overrides)
    return summary


def test_message_lists_every_probed_backend():
    text = build_startup_message(
        warmup=_warmup(), admin_ids=[1], allowed_ids=[1, 2], version="3.1.2.4938"
    )
    assert "запущен" in text
    for name in ("Lidarr", "slskd"):
        assert name in text


def test_healthy_and_failed_backends_are_visually_distinct():
    text = build_startup_message(
        warmup=_warmup(lidarr=(False, 5000.0, None)), admin_ids=[1], allowed_ids=[1], version=None
    )
    slskd_line = next(line for line in text.splitlines() if "slskd" in line)
    lidarr_line = next(line for line in text.splitlines() if "Lidarr" in line)
    assert "✅" in slskd_line
    assert "❌" in lidarr_line


def test_unconfigured_backend_is_not_reported_as_broken():
    """A `None` outcome means "not configured" — that is not a failure."""
    text = build_startup_message(
        warmup=_warmup(slskd=None), admin_ids=[1], allowed_ids=[1], version=None
    )
    slskd_line = next((line for line in text.splitlines() if "slskd" in line), "")
    assert "❌" not in slskd_line


def test_probe_error_is_reported_without_leaking_a_stacktrace():
    text = build_startup_message(
        warmup=_warmup(lidarr=("error", "connection refused to http://user:pw@host")),
        admin_ids=[1], allowed_ids=[1], version=None,
    )
    lidarr_line = next(line for line in text.splitlines() if "Lidarr" in line)
    assert "❌" in lidarr_line
    assert "user:pw" not in text


def test_message_reports_user_counts_and_backend_versions():
    """Each backend reports its own version in the warm-up outcome."""
    text = build_startup_message(
        warmup=_warmup(), admin_ids=[1, 2], allowed_ids=[1, 2, 3]
    )
    assert "2" in text and "3" in text
    assert "3.1.2.4938" in text    # Lidarr
    assert "0.24.5.0" in text      # slskd


def test_message_escapes_html_from_a_probe_error():
    text = build_startup_message(
        warmup=_warmup(lidarr=("error", "<script>alert(1)</script>")),
        admin_ids=[1], allowed_ids=[1], version=None,
    )
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


@pytest.mark.asyncio
async def test_notification_goes_to_every_admin():
    bot = MagicMock()
    bot.send_message = AsyncMock()

    await notify_admins_on_start(bot, admin_ids=[10, 20], allowed_ids=[10, 20, 30], warmup={})

    assert bot.send_message.await_count == 2
    assert {c.args[0] for c in bot.send_message.await_args_list} == {10, 20}


@pytest.mark.asyncio
async def test_a_blocked_admin_does_not_break_startup():
    """One admin who blocked the bot must not stop the others being told."""
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=[Exception("bot was blocked"), None])

    await notify_admins_on_start(bot, admin_ids=[10, 20], allowed_ids=[10], warmup={})

    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_notification_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("NOTIFY_ADMINS_ON_START", "false")
    from bot.config import get_settings

    get_settings.cache_clear()
    try:
        bot = MagicMock()
        bot.send_message = AsyncMock()
        await notify_admins_on_start(bot, admin_ids=[10], allowed_ids=[10], warmup={})
        bot.send_message.assert_not_awaited()
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_startup_wires_the_notification_in():
    """Wiring check: on_startup must actually send it, with the warm-up data."""
    from bot import main as main_mod

    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=MagicMock(username="u", id=1))
    db = MagicMock()
    db.cleanup_old_sessions = AsyncMock(return_value=0)
    db.cleanup_old_searches = AsyncMock(return_value=0)
    db.list_allowed_users = AsyncMock(return_value=[])

    with patch.object(main_mod, "_warm_up_clients", AsyncMock(return_value={"lidarr": (True, 1.0)})), \
         patch.object(main_mod, "notify_admins_on_start", AsyncMock()) as notify:
        await main_mod.on_startup(bot, db, None)

    notify.assert_awaited_once()
    assert notify.await_args.kwargs["warmup"] == {"lidarr": (True, 1.0)}
