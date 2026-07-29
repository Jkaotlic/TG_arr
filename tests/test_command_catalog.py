"""One catalog behind both the Telegram menu and /help.

They were separate lists, and /help had quietly fallen a release behind: no
/anime, /album, /track, /title, /wanted, /calendar, /health — and /music still
described as "артисты (Lidarr)" after music moved to slskd.
"""

import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from bot.main import publish_bot_commands
from bot.ui.commands import COMMAND_GROUPS, bot_commands, render_help


def _catalog() -> set[str]:
    return {name for _, entries in COMMAND_GROUPS for name, _ in entries}


def test_menu_is_built_from_the_catalog():
    assert {c.command for c in bot_commands()} == _catalog()


def test_help_mentions_every_command_in_the_catalog():
    text = render_help()

    missing = [name for name in sorted(_catalog()) if f"/{name}" not in text]
    assert not missing, f"missing from /help: {missing}"


def test_help_is_grouped_under_its_headings():
    text = render_help()

    for heading, _ in COMMAND_GROUPS:
        assert heading in text


def test_catalog_matches_the_implemented_handlers():
    """Advertising a command with no handler is worse than not advertising it:
    the user taps it and the bot stays silent.
    """
    handlers = Path(__file__).resolve().parent.parent / "bot" / "handlers"
    source = "\n".join(p.read_text(encoding="utf-8") for p in handlers.rglob("*.py"))
    implemented = set(re.findall(r'Command\("([a-z_]+)"', source))

    assert _catalog() <= implemented


def test_admin_commands_are_not_advertised():
    assert not ({"users", "adduser", "deluser"} & _catalog())


@pytest.mark.asyncio
async def test_publish_sends_the_catalog():
    bot = AsyncMock()

    await publish_bot_commands(bot)

    bot.set_my_commands.assert_awaited_once()
    sent = bot.set_my_commands.await_args.args[0]
    assert {c.command for c in sent} == _catalog()


@pytest.mark.asyncio
async def test_publish_failure_does_not_break_startup():
    """A cosmetic menu must never keep the bot from coming up."""
    bot = AsyncMock()
    bot.set_my_commands.side_effect = RuntimeError("Telegram unavailable")

    await publish_bot_commands(bot)  # must not raise
