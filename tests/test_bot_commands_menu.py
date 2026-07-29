"""The command menu Telegram shows behind the "/" button.

Nothing registered it before, so every command the bot answers was invisible
unless you already knew it existed — including the ones added on 2026-07-29.
"""

from unittest.mock import AsyncMock

import pytest

from bot.main import BOT_COMMANDS, publish_bot_commands


def _registered() -> set[str]:
    return {c.command for c in BOT_COMMANDS}


def test_menu_covers_the_commands_users_reach_for():
    """The everyday entry points must be discoverable."""
    expected = {"menu", "help", "search", "movie", "series", "anime", "music",
                "downloads", "status", "settings", "history", "cancel"}

    assert expected <= _registered()


def test_menu_includes_the_2026_07_29_additions():
    """/wanted and /title were added with no way to find them."""
    assert {"wanted", "title"} <= _registered()


def test_admin_only_commands_stay_out_of_the_menu():
    """User management is admin-gated; advertising it to everyone invites
    "недостаточно прав" replies from people who never had a reason to try.
    """
    assert not ({"users", "adduser", "deluser"} & _registered())


def test_every_entry_has_a_description():
    """Telegram renders the description next to the command; a blank one just
    looks broken.
    """
    assert all(c.description.strip() for c in BOT_COMMANDS)


def test_every_advertised_command_has_a_handler():
    """A menu entry with nothing behind it is worse than no entry — the user
    taps it and the bot says nothing at all.
    """
    import re
    from pathlib import Path

    handlers = Path(__file__).resolve().parent.parent / "bot" / "handlers"
    source = "\n".join(
        p.read_text(encoding="utf-8") for p in handlers.rglob("*.py")
    )
    implemented = set(re.findall(r'Command\("([a-z_]+)"', source))

    missing = _registered() - implemented
    assert not missing, f"advertised but not implemented: {sorted(missing)}"


@pytest.mark.asyncio
async def test_publish_sends_the_menu():
    bot = AsyncMock()

    await publish_bot_commands(bot)

    bot.set_my_commands.assert_awaited_once()
    sent = bot.set_my_commands.await_args.args[0]
    assert {c.command for c in sent} == _registered()


@pytest.mark.asyncio
async def test_publish_failure_does_not_break_startup():
    """A cosmetic menu must never keep the bot from coming up."""
    bot = AsyncMock()
    bot.set_my_commands.side_effect = RuntimeError("Telegram unavailable")

    await publish_bot_commands(bot)  # must not raise
