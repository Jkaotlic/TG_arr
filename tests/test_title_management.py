"""Managing a title from the bot: stop monitoring / remove (2026-07-29).

Today the only way to stop an *arr hunting 102 unobtainable Paw Patrol
episodes was a hand-written GraphQL script (against Scryer, pre-rollback).
That belongs behind a button.

Rollback 2026-08-10 (Task 13): Scryer's single catalog is gone. Radarr and
Sonarr each own their own library — `_apply_monitor_toggle`/`_apply_delete`
dispatch to the client matching the title's content type, and
`handle_title_action` reads/writes a per-user in-memory cache (populated by
`/title`) instead of re-fetching a title by internal id, since neither
RadarrClient nor SonarrClient exposes a cheap "get one by id" among the
interfaces this rollback carries forward.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.models import MovieInfo, SeriesInfo
from bot.ui.callbacks import TitleActionCB


def _movie(**overrides) -> MovieInfo:
    data = dict(tmdb_id=1, title="Some Movie", year=2020, radarr_id=15, monitored=True, has_file=False)
    data.update(overrides)
    return MovieInfo(**data)


def _series(**overrides) -> SeriesInfo:
    data = dict(title="Paw Patrol", year=2013, sonarr_id=3, monitored=True,
                episodes_owned=104, episodes_total=552)
    data.update(overrides)
    return SeriesInfo(**data)


def _callback(data=None):
    cb = MagicMock()
    cb.data = data
    cb.from_user = MagicMock(id=42)
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    return cb


# --------------------------------------------------------- _apply_monitor_toggle
@pytest.mark.asyncio
async def test_title_command_toggles_monitoring_through_radarr():
    """Task 13 brief's mandated test."""
    from bot.handlers.titles import _apply_monitor_toggle

    radarr = AsyncMock()
    radarr.set_movie_monitored.return_value = True

    ok = await _apply_monitor_toggle(radarr, resource_id=15, monitored=False, is_movie=True)

    assert ok is True
    radarr.set_movie_monitored.assert_awaited_once_with(15, False)


@pytest.mark.asyncio
async def test_monitor_toggle_through_sonarr_for_a_series():
    from bot.handlers.titles import _apply_monitor_toggle

    sonarr = AsyncMock()
    sonarr.set_series_monitored.return_value = True

    ok = await _apply_monitor_toggle(sonarr, resource_id=3, monitored=True, is_movie=False)

    assert ok is True
    sonarr.set_series_monitored.assert_awaited_once_with(3, True)


# ------------------------------------------------------------------- _apply_delete
@pytest.mark.asyncio
async def test_delete_defaults_to_keeping_the_files_for_a_movie():
    """Removing a library entry must not remove the user's media by default."""
    from bot.handlers.titles import _apply_delete

    radarr = AsyncMock()
    radarr.delete_movie.return_value = True

    ok = await _apply_delete(radarr, resource_id=15, is_movie=True)

    assert ok is True
    radarr.delete_movie.assert_awaited_once_with(15, delete_files=False)


@pytest.mark.asyncio
async def test_delete_dispatches_to_sonarr_for_a_series():
    from bot.handlers.titles import _apply_delete

    sonarr = AsyncMock()
    sonarr.delete_series.return_value = True

    ok = await _apply_delete(sonarr, resource_id=3, is_movie=False)

    assert ok is True
    sonarr.delete_series.assert_awaited_once_with(3, delete_files=False)


# ------------------------------------------------------------------------ keyboards
def test_manage_menu_offers_monitoring_and_delete():
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.title_actions(_series())
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert any("монитор" in label.lower() for label in labels)
    assert any("удал" in label.lower() for label in labels)


def test_monitoring_toggle_label_follows_current_state():
    from bot.ui.keyboards import Keyboards

    on = [b.text for row in Keyboards.title_actions(_series(monitored=True)).inline_keyboard for b in row]
    off = [b.text for row in Keyboards.title_actions(_series(monitored=False)).inline_keyboard for b in row]
    assert on != off


def test_title_actions_packs_a_movie_scoped_id():
    """The callback must carry the content type as its OWN field — Radarr's
    and Sonarr's ids are independent sequences, and a colon-joined
    "movie:15" can't survive pack()/unpack() at all (aiogram uses ':' as its
    own field separator; see TitleActionCB's docstring)."""
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.title_actions(_movie(radarr_id=15))
    button = kb.inline_keyboard[0][0]
    cb = TitleActionCB.unpack(button.callback_data)
    assert cb.kind == "movie"
    assert cb.title_id == "15"


def test_title_actions_packs_a_series_scoped_id():
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.title_actions(_series(sonarr_id=3))
    button = kb.inline_keyboard[0][0]
    cb = TitleActionCB.unpack(button.callback_data)
    assert cb.kind == "series"
    assert cb.title_id == "3"


def test_title_choices_packs_both_kinds_without_collision():
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.title_choices([_movie(radarr_id=1), _series(sonarr_id=1)])
    refs = [
        (TitleActionCB.unpack(row[0].callback_data).kind, TitleActionCB.unpack(row[0].callback_data).title_id)
        for row in kb.inline_keyboard[:2]
    ]
    assert refs == [("movie", "1"), ("series", "1")]


# ---------------------------------------------------------------------------- /title
@pytest.mark.asyncio
async def test_title_command_finds_a_single_library_match():
    from bot.handlers import titles as titles_handler

    search_service = AsyncMock()
    search_service.lookup_movies = AsyncMock(return_value=[_movie(radarr_id=15, title="Dune")])
    search_service.lookup_series = AsyncMock(return_value=[])

    message = MagicMock()
    message.text = "/title Dune"
    message.from_user = MagicMock(id=42)
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=status_msg)

    with patch.object(titles_handler, "get_radarr", AsyncMock()), \
         patch.object(titles_handler, "get_sonarr", AsyncMock()), \
         patch.object(titles_handler, "SearchService", return_value=search_service):
        await titles_handler.cmd_title(message, db_user=MagicMock(tg_id=42), db=AsyncMock())

    text = status_msg.edit_text.await_args.args[0]
    assert "Dune" in text
    assert "movie:15" in titles_handler._title_candidates[42]


@pytest.mark.asyncio
async def test_title_command_filters_out_candidates_not_yet_in_the_library():
    """A Radarr/Sonarr lookup returns TMDb/TVDB search hits too — only ones
    already added (radarr_id/sonarr_id set) belong in library management."""
    from bot.handlers import titles as titles_handler

    search_service = AsyncMock()
    search_service.lookup_movies = AsyncMock(return_value=[
        _movie(radarr_id=15, title="In Library"),
        _movie(radarr_id=None, title="Not Added"),
    ])
    search_service.lookup_series = AsyncMock(return_value=[])

    message = MagicMock()
    message.text = "/title x"
    message.from_user = MagicMock(id=43)
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=status_msg)

    with patch.object(titles_handler, "get_radarr", AsyncMock()), \
         patch.object(titles_handler, "get_sonarr", AsyncMock()), \
         patch.object(titles_handler, "SearchService", return_value=search_service):
        await titles_handler.cmd_title(message, db_user=MagicMock(tg_id=43), db=AsyncMock())

    text = status_msg.edit_text.await_args.args[0]
    assert "In Library" in text
    assert "Not Added" not in text


@pytest.mark.asyncio
async def test_title_command_asks_when_several_match():
    from bot.handlers import titles as titles_handler

    search_service = AsyncMock()
    search_service.lookup_movies = AsyncMock(return_value=[
        _movie(radarr_id=1, title="Frozen"), _movie(radarr_id=2, title="Frozen II"),
    ])
    search_service.lookup_series = AsyncMock(return_value=[])

    message = MagicMock()
    message.text = "/title Frozen"
    message.from_user = MagicMock(id=44)
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=status_msg)

    with patch.object(titles_handler, "get_radarr", AsyncMock()), \
         patch.object(titles_handler, "get_sonarr", AsyncMock()), \
         patch.object(titles_handler, "SearchService", return_value=search_service):
        await titles_handler.cmd_title(message, db_user=MagicMock(tg_id=44), db=AsyncMock())

    _, kwargs = status_msg.edit_text.call_args
    assert kwargs["reply_markup"] is not None


# --------------------------------------------------------------------- handlers
@pytest.mark.asyncio
async def test_delete_asks_for_confirmation_first():
    """A destructive action gets a confirm step, showing what's on disk."""
    from bot.handlers import titles as titles_handler

    radarr = AsyncMock()
    sonarr = AsyncMock()
    db = AsyncMock()
    cb = _callback()

    titles_handler._title_candidates[42] = {"movie:15": _movie(radarr_id=15, has_file=True)}

    with patch.object(titles_handler, "get_radarr", AsyncMock(return_value=radarr)), \
         patch.object(titles_handler, "get_sonarr", AsyncMock(return_value=sonarr)):
        await titles_handler.handle_title_action(
            cb, TitleActionCB(action="delete", kind="movie", title_id="15"), MagicMock(tg_id=42), db
        )

    radarr.delete_movie.assert_not_awaited()
    text = cb.message.edit_text.await_args.args[0]
    assert "останется" in text.lower() or "останутся" in text.lower()
    titles_handler._title_candidates.pop(42, None)


@pytest.mark.asyncio
async def test_confirmed_delete_goes_through_radarr():
    from bot.handlers import titles as titles_handler

    radarr = AsyncMock()
    radarr.delete_movie = AsyncMock(return_value=True)
    sonarr = AsyncMock()
    db = AsyncMock()
    cb = _callback()

    titles_handler._title_candidates[42] = {"movie:15": _movie(radarr_id=15)}

    with patch.object(titles_handler, "get_radarr", AsyncMock(return_value=radarr)), \
         patch.object(titles_handler, "get_sonarr", AsyncMock(return_value=sonarr)):
        await titles_handler.handle_title_action(
            cb, TitleActionCB(action="delconf", kind="movie", title_id="15"), MagicMock(tg_id=42), db
        )

    radarr.delete_movie.assert_awaited_once_with(15, delete_files=False)
    db.log_action.assert_awaited_once()
    titles_handler._title_candidates.pop(42, None)


@pytest.mark.asyncio
async def test_confirmed_delete_goes_through_sonarr():
    from bot.handlers import titles as titles_handler

    radarr = AsyncMock()
    sonarr = AsyncMock()
    sonarr.delete_series = AsyncMock(return_value=True)
    db = AsyncMock()
    cb = _callback()

    titles_handler._title_candidates[42] = {"series:3": _series(sonarr_id=3)}

    with patch.object(titles_handler, "get_radarr", AsyncMock(return_value=radarr)), \
         patch.object(titles_handler, "get_sonarr", AsyncMock(return_value=sonarr)):
        await titles_handler.handle_title_action(
            cb, TitleActionCB(action="delconf", kind="series", title_id="3"), MagicMock(tg_id=42), db
        )

    sonarr.delete_series.assert_awaited_once_with(3, delete_files=False)
    db.log_action.assert_awaited_once()
    titles_handler._title_candidates.pop(42, None)


@pytest.mark.asyncio
async def test_unmonitor_action_reports_back():
    from bot.handlers import titles as titles_handler

    radarr = AsyncMock()
    radarr.set_movie_monitored = AsyncMock(return_value=True)
    sonarr = AsyncMock()
    db = AsyncMock()
    cb = _callback()

    titles_handler._title_candidates[42] = {"movie:15": _movie(radarr_id=15, monitored=True)}

    with patch.object(titles_handler, "get_radarr", AsyncMock(return_value=radarr)), \
         patch.object(titles_handler, "get_sonarr", AsyncMock(return_value=sonarr)):
        await titles_handler.handle_title_action(
            cb, TitleActionCB(action="unmon", kind="movie", title_id="15"), MagicMock(tg_id=42), db
        )

    radarr.set_movie_monitored.assert_awaited_once_with(15, False)
    assert cb.message.edit_text.await_count == 1
    # The card re-rendered from the cache must reflect the new state.
    text = cb.message.edit_text.await_args.args[0]
    assert "выключен" in text.lower()
    titles_handler._title_candidates.pop(42, None)


@pytest.mark.asyncio
async def test_monitor_action_dispatches_to_sonarr_for_a_series():
    from bot.handlers import titles as titles_handler

    radarr = AsyncMock()
    sonarr = AsyncMock()
    sonarr.set_series_monitored = AsyncMock(return_value=True)
    db = AsyncMock()
    cb = _callback()

    titles_handler._title_candidates[42] = {"series:3": _series(sonarr_id=3, monitored=False)}

    with patch.object(titles_handler, "get_radarr", AsyncMock(return_value=radarr)), \
         patch.object(titles_handler, "get_sonarr", AsyncMock(return_value=sonarr)):
        await titles_handler.handle_title_action(
            cb, TitleActionCB(action="mon", kind="series", title_id="3"), MagicMock(tg_id=42), db
        )

    sonarr.set_series_monitored.assert_awaited_once_with(3, True)
    radarr.set_movie_monitored.assert_not_awaited()
    titles_handler._title_candidates.pop(42, None)


@pytest.mark.asyncio
async def test_pick_action_renders_the_chosen_candidate():
    from bot.handlers import titles as titles_handler

    db = AsyncMock()
    cb = _callback()

    titles_handler._title_candidates[42] = {
        "movie:1": _movie(radarr_id=1, title="Frozen"),
        "movie:2": _movie(radarr_id=2, title="Frozen II"),
    }

    with patch.object(titles_handler, "get_radarr", AsyncMock()), \
         patch.object(titles_handler, "get_sonarr", AsyncMock()):
        await titles_handler.handle_title_action(
            cb, TitleActionCB(action="pick", kind="movie", title_id="2"), MagicMock(tg_id=42), db
        )

    text = cb.message.edit_text.await_args.args[0]
    assert "Frozen II" in text
    titles_handler._title_candidates.pop(42, None)


@pytest.mark.asyncio
async def test_action_on_an_expired_cache_reports_gone_not_a_crash():
    from bot.handlers import titles as titles_handler

    db = AsyncMock()
    cb = _callback()
    titles_handler._title_candidates.pop(42, None)

    with patch.object(titles_handler, "get_radarr", AsyncMock()), \
         patch.object(titles_handler, "get_sonarr", AsyncMock()):
        await titles_handler.handle_title_action(
            cb, TitleActionCB(action="pick", kind="movie", title_id="99"), MagicMock(tg_id=42), db
        )

    cb.message.edit_text.assert_awaited_once()


# ---------------------------------------------------------------------------
# Task 13, carried-forward: titles.py must not reference get_scryer.
# ---------------------------------------------------------------------------
def test_titles_module_does_not_call_get_scryer():
    from pathlib import Path
    from bot.handlers import titles as titles_handler

    source = Path(titles_handler.__file__).read_text(encoding="utf-8")
    assert "get_scryer" not in source
