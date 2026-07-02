"""Tests for bot/handlers/common.py — LOGIC-13 (strip_command) and
LOGIC-15 (safe_edit / swallow_not_modified).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest

from bot.handlers.common import safe_edit, strip_command, swallow_not_modified


# ============================================================================
# LOGIC-13: strip_command
# ============================================================================


def test_strip_command_plain_args():
    assert strip_command("/pause all", "/pause") == "all"


def test_strip_command_no_args():
    assert strip_command("/pause", "/pause") == ""


def test_strip_command_extra_whitespace():
    assert strip_command("/pause   all  ", "/pause") == "all"


def test_strip_command_non_matching_text_returned_unchanged():
    assert strip_command("hello world", "/pause") == "hello world"


def test_strip_command_botname_suffix_is_removed():
    """RED test (LOGIC-13): a naive text.replace("/pause", "") would leave
    "@botname all" as the args, which downloads.py then tries to resolve as
    a torrent hash. strip_command must strip the @botname suffix too.
    """
    assert strip_command("/pause@botname all", "/pause") == "all"


def test_strip_command_botname_suffix_no_args():
    assert strip_command("/pause@botname", "/pause") == ""


def test_strip_command_music_botname_suffix():
    """Same bug reproduced for music.py's /music command."""
    assert strip_command("/music@botname Metallica", "/music") == "Metallica"


# ============================================================================
# LOGIC-15: safe_edit / swallow_not_modified
# ============================================================================


@pytest.mark.asyncio
async def test_safe_edit_applies_edit_and_returns_true():
    message = MagicMock()
    message.edit_text = AsyncMock()

    result = await safe_edit(message, "hello", parse_mode="HTML")

    message.edit_text.assert_awaited_once_with("hello", parse_mode="HTML")
    assert result is True


@pytest.mark.asyncio
async def test_safe_edit_swallows_message_not_modified():
    message = MagicMock()
    message.edit_text = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: message is not modified")
    )

    result = await safe_edit(message, "hello")

    assert result is False


@pytest.mark.asyncio
async def test_safe_edit_reraises_other_bad_request():
    message = MagicMock()
    message.edit_text = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: message to edit not found")
    )

    with pytest.raises(TelegramBadRequest):
        await safe_edit(message, "hello")


@pytest.mark.asyncio
async def test_swallow_not_modified_passthrough_success():
    called = AsyncMock()
    result = await swallow_not_modified(called())
    called.assert_awaited_once()
    assert result is True


@pytest.mark.asyncio
async def test_swallow_not_modified_swallows_not_modified():
    async def _raise():
        raise TelegramBadRequest(method=MagicMock(), message="message is not modified")

    result = await swallow_not_modified(_raise())
    assert result is False


@pytest.mark.asyncio
async def test_swallow_not_modified_reraises_other_errors():
    async def _raise():
        raise TelegramBadRequest(method=MagicMock(), message="message to edit not found")

    with pytest.raises(TelegramBadRequest):
        await swallow_not_modified(_raise())
