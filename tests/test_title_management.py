"""Managing a title from the bot: stop monitoring / remove (2026-07-29).

Today the only way to stop Scryer hunting 102 unobtainable Paw Patrol episodes
was a hand-written GraphQL script. That belongs behind a button.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.models import SeriesInfo
from bot.services.add_service import AddService


def _series(**overrides) -> SeriesInfo:
    data = dict(title="Paw Patrol", year=2013, scryer_id="t1", monitored=True,
                episodes_owned=104, episodes_total=552)
    data.update(overrides)
    return SeriesInfo(**data)


# ------------------------------------------------------------ stop monitoring
@pytest.mark.asyncio
async def test_stop_monitoring_flips_the_flag():
    scryer = AsyncMock()
    scryer.set_title_monitored = AsyncMock(return_value=False)
    service = AddService(scryer)

    ok = await service.set_monitored(_series(), monitored=False)

    assert ok is True
    scryer.set_title_monitored.assert_awaited_once_with("t1", False)


@pytest.mark.asyncio
async def test_stop_monitoring_without_an_id_fails_cleanly():
    scryer = AsyncMock()
    service = AddService(scryer)

    ok = await service.set_monitored(SeriesInfo(title="X"), monitored=False)

    assert ok is False
    scryer.set_title_monitored.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_monitoring_reports_a_service_failure():
    scryer = AsyncMock()
    scryer.set_title_monitored = AsyncMock(side_effect=RuntimeError("boom"))
    service = AddService(scryer)

    assert await service.set_monitored(_series(), monitored=False) is False


# -------------------------------------------------------------------- delete
@pytest.mark.asyncio
async def test_delete_previews_before_removing():
    """Never delete blind: the preview says how many files are on disk, and
    the fingerprint ties the confirmation to what was actually shown."""
    scryer = AsyncMock()
    scryer.delete_title_preview = AsyncMock(
        return_value={"fingerprint": "fp1", "totalFileCount": 0, "targetLabel": "Paw Patrol"}
    )
    scryer.delete_title = AsyncMock(return_value=True)
    service = AddService(scryer)

    preview = await service.preview_delete("t1")
    assert preview["fingerprint"] == "fp1"

    ok = await service.delete_title("t1", fingerprint="fp1", delete_files=False)
    assert ok is True
    assert scryer.delete_title.await_args.kwargs["delete_files"] is False


@pytest.mark.asyncio
async def test_delete_defaults_to_keeping_the_files():
    """Removing a catalog entry must not remove the user's media by default."""
    scryer = AsyncMock()
    scryer.delete_title = AsyncMock(return_value=True)
    service = AddService(scryer)

    await service.delete_title("t1", fingerprint="fp1")

    assert scryer.delete_title.await_args.kwargs["delete_files"] is False


# ------------------------------------------------------------------ handlers
def _callback(data=None):
    cb = MagicMock()
    cb.data = data
    cb.from_user = MagicMock(id=42)
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    return cb


@pytest.mark.asyncio
async def test_manage_menu_offers_monitoring_and_delete():
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.title_actions(_series())
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert any("монитор" in label.lower() for label in labels)
    assert any("удал" in label.lower() for label in labels)


@pytest.mark.asyncio
async def test_monitoring_toggle_label_follows_current_state():
    from bot.ui.keyboards import Keyboards

    on = [b.text for row in Keyboards.title_actions(_series(monitored=True)).inline_keyboard for b in row]
    off = [b.text for row in Keyboards.title_actions(_series(monitored=False)).inline_keyboard for b in row]
    assert on != off


@pytest.mark.asyncio
async def test_delete_asks_for_confirmation_first():
    """A destructive action gets a confirm step, with the file count shown."""
    from bot.handlers import titles as titles_handler
    from bot.ui.callbacks import TitleActionCB

    scryer = AsyncMock()
    scryer.delete_title_preview = AsyncMock(
        return_value={"fingerprint": "fp1", "totalFileCount": 3, "targetLabel": "Paw Patrol"}
    )
    scryer.delete_title = AsyncMock()
    db = AsyncMock()
    cb = _callback()

    with patch.object(titles_handler, "get_scryer", AsyncMock(return_value=scryer)):
        await titles_handler.handle_title_action(
            cb, TitleActionCB(action="delete", title_id="t1"), MagicMock(tg_id=42), db
        )

    scryer.delete_title.assert_not_awaited()
    text = cb.message.edit_text.await_args.args[0]
    assert "3" in text  # the file count the user is about to affect


@pytest.mark.asyncio
async def test_confirmed_delete_goes_through():
    from bot.handlers import titles as titles_handler
    from bot.ui.callbacks import TitleActionCB

    scryer = AsyncMock()
    scryer.delete_title_preview = AsyncMock(
        return_value={"fingerprint": "fp1", "totalFileCount": 0, "targetLabel": "Paw Patrol"}
    )
    scryer.delete_title = AsyncMock(return_value=True)
    db = AsyncMock()
    cb = _callback()

    with patch.object(titles_handler, "get_scryer", AsyncMock(return_value=scryer)):
        await titles_handler.handle_title_action(
            cb, TitleActionCB(action="delconf", title_id="t1"), MagicMock(tg_id=42), db
        )

    scryer.delete_title.assert_awaited_once()
    db.log_action.assert_awaited_once()


@pytest.mark.asyncio
async def test_unmonitor_action_reports_back():
    from bot.handlers import titles as titles_handler
    from bot.ui.callbacks import TitleActionCB

    scryer = AsyncMock()
    scryer.set_title_monitored = AsyncMock(return_value=False)
    scryer.get_title = AsyncMock(return_value=_series(monitored=False))
    db = AsyncMock()
    cb = _callback()

    with patch.object(titles_handler, "get_scryer", AsyncMock(return_value=scryer)):
        await titles_handler.handle_title_action(
            cb, TitleActionCB(action="unmon", title_id="t1"), MagicMock(tg_id=42), db
        )

    scryer.set_title_monitored.assert_awaited_once_with("t1", False)
    assert cb.message.edit_text.await_count == 1
