"""Final branch review (2026-09-24): minor fixes."""

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMessage
from aiogram.types import Chat, Message
from aiogram.types import User as TgUser

from bot.models import QBittorrentStatus, User

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------ clip first, then escape
@pytest.mark.asyncio
async def test_soulseek_line_clips_before_escaping():
    """Escaping first and cutting at 45 could split "&amp;" into a bare "&"."""
    from bot.handlers import downloads

    name = "A" * 44 + "&B" + "C" * 20
    transfer = SimpleNamespace(filename=name, is_errored=False, progress_percent=10, speed_formatted="1 КБ/с")
    slskd = AsyncMock()
    slskd.get_active_transfers = AsyncMock(return_value=[transfer])
    with patch.object(downloads, "get_slskd", AsyncMock(return_value=slskd)):
        text = await downloads._soulseek_section()
    assert "A" * 44 + "&amp;" in text
    assert re.search(r"&(?!amp;|lt;|gt;|quot;|#x27;)", text) is None


def test_no_escape_then_slice_left_in_bot():
    pattern = re.compile(r"(?:html\.escape|_html\.escape|_e)\([^()]*(?:\([^()]*\))?[^()]*\)\[:\d+\]")
    offenders = [
        f"{p.relative_to(ROOT)}:{i}"
        for p in (ROOT / "bot").rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == []


# ------------------------------------------------ music prompt vs commands
def _reply_msg(text: str) -> Message:
    from bot.handlers.music import MUSIC_PROMPT

    bot_user = TgUser(id=999, is_bot=True, first_name="bot")
    prompt = Message(message_id=1, date=0, chat=Chat(id=42, type="private"), from_user=bot_user, text=MUSIC_PROMPT)
    return Message(
        message_id=2, date=0, chat=Chat(id=42, type="private"),
        from_user=TgUser(id=42, is_bot=False, first_name="u"), text=text, reply_to_message=prompt,
    )


@pytest.mark.asyncio
async def test_music_prompt_reply_ignores_commands():
    from bot.handlers.music import handle_music_prompt_reply, prompt_router

    handler = next(h for h in prompt_router.message.handlers if h.callback is handle_music_prompt_reply)
    ok_artist, _ = await handler.check(_reply_msg("Metallica"))
    ok_command, _ = await handler.check(_reply_msg("/downloads"))
    assert ok_artist is True
    assert ok_command is False


# ------------------------------------------------ empty list keeps qBit summary
@pytest.mark.asyncio
async def test_empty_downloads_list_still_shows_the_qbit_summary():
    from bot.handlers import downloads

    qbt = AsyncMock()
    qbt.get_torrents = AsyncMock(return_value=[])
    status = QBittorrentStatus(
        version="4.6.0", connection_status="connected", download_speed=0, upload_speed=0,
        active_downloads=0, free_space=10 * 1024**3,
    )
    status_msg = MagicMock(edit_text=AsyncMock())
    msg = MagicMock(answer=AsyncMock(return_value=status_msg), from_user=MagicMock(id=42))
    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)), \
         patch.object(downloads, "_list_status", AsyncMock(return_value=status)), \
         patch.object(downloads, "_soulseek_section", AsyncMock(return_value="")):
        await downloads.cmd_downloads(msg, User(tg_id=42))
    text = status_msg.edit_text.await_args.args[0]
    assert "Торренты не найдены" in text
    assert "активных: 0" in text
    assert "свободно:" in text


# ------------------------------------------------ MoreCB length
def test_more_cb_packs_within_64_bytes_for_every_panel_key():
    from bot.handlers.start import _MORE_TARGETS
    from bot.ui.callbacks import MoreCB
    from bot.ui.keyboards.menu import MORE_ROWS

    keys = set(_MORE_TARGETS) | {key for row in MORE_ROWS for _text, key in row}
    assert keys
    assert max(len(MoreCB(key=k).pack().encode()) for k in keys) <= 64


# ------------------------------------------------ "⋯ Ещё" double tap
def _more_cb(message) -> MagicMock:
    cb = MagicMock()
    cb.from_user = MagicMock(id=42)
    cb.message = message
    cb.answer = AsyncMock()
    return cb


def _bad_request() -> TelegramBadRequest:
    return TelegramBadRequest(method=DeleteMessage(chat_id=42, message_id=5), message="message not found")


@pytest.mark.asyncio
async def test_more_panel_second_tap_does_nothing():
    from bot.handlers import history, start
    from bot.ui.callbacks import MoreCB

    panel = MagicMock()
    panel.chat = MagicMock(id=42)
    panel.message_id = 5
    panel.delete = AsyncMock()
    with patch.object(history, "cmd_history", autospec=True) as target:
        first, second = _more_cb(panel), _more_cb(panel)
        await start.handle_more_item(first, MoreCB(key="history"), db_user=User(tg_id=42), db=MagicMock(), is_admin=True)
        await start.handle_more_item(second, MoreCB(key="history"), db_user=User(tg_id=42), db=MagicMock(), is_admin=True)
    target.assert_awaited_once()
    second.answer.assert_awaited_once()
    assert not second.answer.await_args.kwargs.get("show_alert")


@pytest.mark.asyncio
async def test_more_panel_already_gone_does_nothing():
    """Delete and edit both fail → the panel was handled elsewhere (e.g. after a restart)."""
    from bot.handlers import history, start
    from bot.ui.callbacks import MoreCB

    panel = MagicMock()
    panel.chat = MagicMock(id=42)
    panel.message_id = 6
    panel.delete = AsyncMock(side_effect=_bad_request())
    panel.edit_reply_markup = AsyncMock(side_effect=_bad_request())
    cb = _more_cb(panel)
    with patch.object(history, "cmd_history", autospec=True) as target:
        await start.handle_more_item(cb, MoreCB(key="history"), db_user=User(tg_id=42), db=MagicMock(), is_admin=True)
    target.assert_not_awaited()
    cb.answer.assert_awaited_once()
    assert not cb.answer.await_args.kwargs.get("show_alert")


# ------------------------------------------------ Soulseek enqueue error keeps the card keyboard
@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["raise", "reject"])
async def test_soulseek_enqueue_error_keeps_retry_and_back(failure):
    from bot.handlers import music
    from bot.models import SlskdFile, SlskdSearchResult
    from bot.ui.callbacks import SlskdCB

    candidate = SlskdSearchResult(
        username="peer1", folder="Artist - Album",
        files=[SlskdFile(filename="a.flac", name="a.flac", size=100, extension="flac")],
    )
    music._slskd_candidates[42] = [candidate]
    tok = music._slskd_tokens.issue(42)
    slskd = AsyncMock()
    if failure == "raise":
        slskd.enqueue = AsyncMock(side_effect=RuntimeError("boom"))
    else:
        slskd.enqueue = AsyncMock(return_value=False)
    cb = MagicMock()
    cb.from_user = MagicMock(id=42)
    cb.answer = AsyncMock()
    cb.message = MagicMock(edit_text=AsyncMock())
    try:
        with patch.object(music, "get_slskd", AsyncMock(return_value=slskd)):
            await music.handle_slskd_selection(cb, SlskdCB(tok=tok, idx=0), MagicMock(tg_id=42), AsyncMock())
    finally:
        music._slskd_candidates.clear()
    kb = cb.message.edit_text.await_args.kwargs.get("reply_markup")
    assert kb is not None
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert "✅ Скачать" in texts
    assert "◀️ Назад" in texts
    retry = kb.inline_keyboard[0][0].callback_data
    assert SlskdCB.unpack(retry) == SlskdCB(tok=tok, idx=0)


def test_trending_series_button_source_is_formatted():
    src = (ROOT / "bot" / "ui" / "keyboards" / "trending.py").read_text(encoding="utf-8")
    assert 'AddContentCB(kind="series",tmdb_id' not in src
