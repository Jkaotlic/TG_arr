"""Audit 2026-09-23, Task B3: an honest grab chain.

GRAB-02 (confirm): a release *arr rejected is never taken in one tap — the card
first shows the reasons and asks.
GRAB-08: a profile rejection is reported, not silently routed to qBittorrent; a
push that never reached a verdict no longer turns into a silent *arr
auto-search reported as "grabbed" — the user is offered it, and told the
release may differ.
GRAB-09: the free search pushed everything to Sonarr, films included.
GRAB-14: *arr's release cache (30 min) outlives nothing — a grab on an old list
got "Ошибка 404"; a timeout was reported as failure although the grab landed.
"""

import contextlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.clients.base import APIError, ServiceConnectionError
from bot.models import (
    ActionLog,
    ActionType,
    ContentType,
    MovieInfo,
    QualityInfo,
    SearchResult,
    SearchSession,
    User,
    UserPreferences,
)
from bot.services.add_service import (
    GRAB_OUTCOME_GONE,
    GRAB_OUTCOME_REJECTED,
    GRAB_OUTCOME_UNREACHABLE,
    AddService,
    grab_outcome,
)

TOK = "tok123"


def _arr_release(**kw) -> SearchResult:
    data = dict(
        guid="g1", title="Movie.2024.2160p.WEB-DL", indexer="RuTracker", origin="arr",
        indexer_id=3, download_url="http://localhost:9696/1/download?apikey=SECRET",
        size=8 * 1024**3, quality=QualityInfo(resolution="2160p"),
    )
    data.update(kw)
    return SearchResult(**data)


def _service(radarr=None, sonarr=None, qbt=None, lidarr=None) -> AddService:
    return AddService(radarr or AsyncMock(), sonarr or AsyncMock(), qbittorrent=qbt, lidarr=lidarr)


# =============================================================== AddService
@pytest.mark.asyncio
async def test_push_rejection_is_reported_and_never_falls_back_to_qbit():
    radarr = AsyncMock()
    radarr.push_release.return_value = {"approved": False, "rejections": ["Unknown Movie"]}
    qbt = AsyncMock()
    service = _service(radarr=radarr, qbt=qbt)

    ok, action = await service.grab_release(
        _arr_release(indexer_id=None), ContentType.MOVIE, arr_id=15,
    )

    assert ok is False
    qbt.add_torrent_url.assert_not_awaited()
    assert "Unknown Movie" in action.error_message
    assert grab_outcome(action) == GRAB_OUTCOME_REJECTED
    assert json.loads(action.details)["rejections"] == ["Unknown Movie"]


@pytest.mark.asyncio
async def test_push_transport_failure_offers_auto_search_instead_of_running_it():
    radarr = AsyncMock()
    radarr.push_release.side_effect = APIError("Radarr временно недоступен (503)")
    service = _service(radarr=radarr)

    ok, action = await service.grab_release(
        _arr_release(indexer_id=None), ContentType.MOVIE, arr_id=15,
    )

    assert ok is False
    radarr.search_movie.assert_not_awaited()
    assert grab_outcome(action) == GRAB_OUTCOME_UNREACHABLE


@pytest.mark.asyncio
async def test_release_without_a_download_url_is_unreachable_not_auto_searched():
    sonarr = AsyncMock()
    service = _service(sonarr=sonarr)

    ok, action = await service.grab_release(
        _arr_release(indexer_id=None, download_url=None), ContentType.SERIES, arr_id=7,
    )

    assert ok is False
    sonarr.search_series.assert_not_awaited()
    assert grab_outcome(action) == GRAB_OUTCOME_UNREACHABLE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type, release_kw, client, method, args",
    [
        (ContentType.MOVIE, {}, "radarr", "search_movie", (15,)),
        (ContentType.SERIES, {"is_season_pack": True, "detected_season": 2}, "sonarr", "search_season", (15, 2)),
        (ContentType.SERIES, {}, "sonarr", "search_series", (15,)),
        (ContentType.MUSIC, {}, "lidarr", "search_album", (15,)),
    ],
)
async def test_auto_search_is_an_explicit_step(content_type, release_kw, client, method, args):
    clients = {"radarr": AsyncMock(), "sonarr": AsyncMock(), "lidarr": AsyncMock()}
    service = AddService(clients["radarr"], clients["sonarr"], lidarr=clients["lidarr"])

    ok, action = await service.auto_search(_arr_release(**release_kw), content_type, arr_id=15)

    assert ok is True and action.success is True
    getattr(clients[client], method).assert_awaited_once_with(*args)


@pytest.mark.asyncio
async def test_native_404_refetches_and_retries_the_same_guid_once():
    radarr = AsyncMock()
    radarr.grab_release.side_effect = [APIError("Ошибка Radarr: 404", status_code=404), True]
    radarr.get_releases.return_value = [_arr_release(guid="other"), _arr_release(guid="g1", indexer_id=9)]
    service = _service(radarr=radarr)

    ok, action = await service.grab_release(_arr_release(), ContentType.MOVIE, arr_id=15)

    assert ok is True
    radarr.get_releases.assert_awaited_once_with(15)
    assert radarr.grab_release.await_args_list[-1].args == ("g1", 9)


@pytest.mark.asyncio
async def test_native_404_for_a_release_that_is_gone_says_so():
    radarr = AsyncMock()
    radarr.grab_release.side_effect = APIError("Ошибка Radarr: 404", status_code=404)
    radarr.get_releases.return_value = [_arr_release(guid="other")]
    service = _service(radarr=radarr)

    ok, action = await service.grab_release(_arr_release(), ContentType.MOVIE, arr_id=15)

    assert ok is False
    assert radarr.grab_release.await_count == 1
    assert action.error_message == "Релиз больше недоступен — обновите поиск"
    assert grab_outcome(action) == GRAB_OUTCOME_GONE


@pytest.mark.asyncio
async def test_native_404_refetch_for_a_series_keeps_the_season():
    sonarr = AsyncMock()
    sonarr.grab_release.side_effect = APIError("404", status_code=404)
    sonarr.get_releases.return_value = []
    service = _service(sonarr=sonarr)

    await service.grab_release(_arr_release(detected_season=3), ContentType.SERIES, arr_id=7)

    sonarr.get_releases.assert_awaited_once_with(7, season_number=3)


@pytest.mark.asyncio
async def test_native_timeout_checks_the_queue_before_reporting_failure():
    radarr = AsyncMock()
    radarr.grab_release.side_effect = ServiceConnectionError("Таймаут соединения с Radarr")
    radarr.find_in_queue.return_value = True
    service = _service(radarr=radarr)

    ok, action = await service.grab_release(_arr_release(), ContentType.MOVIE, arr_id=15)

    assert ok is True and action.success is True
    radarr.find_in_queue.assert_awaited_once_with("Movie.2024.2160p.WEB-DL")


@pytest.mark.asyncio
async def test_native_timeout_not_in_queue_is_unreachable():
    radarr = AsyncMock()
    radarr.grab_release.side_effect = ServiceConnectionError("Таймаут соединения с Radarr")
    radarr.find_in_queue.return_value = False
    service = _service(radarr=radarr)

    ok, action = await service.grab_release(_arr_release(), ContentType.MOVIE, arr_id=15)

    assert ok is False
    assert grab_outcome(action) == GRAB_OUTCOME_UNREACHABLE


@pytest.mark.asyncio
async def test_native_arr_error_offers_auto_search():
    """*arr answered with an error (e.g. the indexer would not hand over the
    torrent): nothing was queued, *arr may still find another release."""
    radarr = AsyncMock()
    radarr.grab_release.side_effect = APIError("Ошибка Radarr: 500", status_code=500)
    service = _service(radarr=radarr)

    ok, action = await service.grab_release(_arr_release(), ContentType.MOVIE, arr_id=15)

    assert ok is False
    assert grab_outcome(action) == GRAB_OUTCOME_UNREACHABLE


def test_grab_outcome_tolerates_missing_or_foreign_details():
    a = ActionLog(user_id=0, action_type=ActionType.GRAB, content_type=ContentType.MOVIE)
    assert grab_outcome(a) is None
    a.details = "not json"
    assert grab_outcome(a) is None


# ---------------------------------------------------------------- client queue
@pytest.mark.asyncio
async def test_find_in_queue_matches_the_release_title():
    from bot.clients.radarr import RadarrClient

    client = RadarrClient("http://radarr.example.com", "key")
    payload = {"records": [{"title": "Other"}, {"title": "movie.2024.2160p.web-dl"}]}
    with patch.object(client, "get", new=AsyncMock(return_value=payload)) as get:
        assert await client.find_in_queue("Movie.2024.2160p.WEB-DL") is True
    assert get.await_args.args[0] == "/api/v3/queue"


@pytest.mark.asyncio
async def test_find_in_queue_is_false_when_absent_or_unreachable():
    from bot.clients.radarr import RadarrClient

    client = RadarrClient("http://radarr.example.com", "key")
    with patch.object(client, "get", new=AsyncMock(return_value={"records": [{"title": "Other"}]})):
        assert await client.find_in_queue("X") is False
    with patch.object(client, "get", new=AsyncMock(side_effect=ServiceConnectionError("down"))):
        assert await client.find_in_queue("X") is False


# =============================================================== handlers
def _session(results, **kw) -> SearchSession:
    return SearchSession(
        user_id=42, query="q", content_type=ContentType.MOVIE, results=results,
        selected_content=MovieInfo(tmdb_id=1, title="Movie", year=2024, radarr_id=15),
        list_token=TOK, **kw,
    )


def _db(session=None) -> MagicMock:
    db = MagicMock()
    db.get_session = AsyncMock(return_value=session)
    db.save_session = AsyncMock()
    db.update_session = AsyncMock(return_value=True)
    db.delete_session = AsyncMock()
    db.log_action = AsyncMock()

    @contextlib.asynccontextmanager
    async def _lock(_uid):
        yield

    db.session_lock = _lock
    return db


def _cb() -> MagicMock:
    cb = MagicMock()
    cb.from_user = MagicMock(id=42)
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def _user() -> User:
    return User(tg_id=42, preferences=UserPreferences(auto_grab_enabled=True))


def _datas(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def _texts(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


@pytest.fixture(autouse=True)
def _clear_guard():
    from bot.handlers import search

    search._grab_in_progress.clear()
    yield
    search._grab_in_progress.clear()


@pytest.mark.asyncio
async def test_grabbing_a_rejected_release_asks_first_with_the_reasons():
    from bot.handlers import search
    from bot.ui.callbacks import GrabCB, ReleaseCB

    rejected = _arr_release(rejected=True, rejections=["Russian Dub without English", "Not wanted"])
    db = _db(_session([rejected]))
    cb = _cb()

    with patch.object(search, "get_services", AsyncMock(return_value=(MagicMock(), MagicMock()))), \
         patch.object(search, "grab_release", AsyncMock()) as gr:
        await search.handle_confirm_grab(cb, GrabCB(tok=TOK, idx=0), _user(), db)

    gr.assert_not_awaited()
    text = cb.message.edit_text.await_args.args[0]
    assert "Russian Dub without English" in text and "Not wanted" in text
    markup = cb.message.edit_text.await_args.kwargs["reply_markup"]
    assert "⚠️ Всё равно скачать" in _texts(markup)
    assert "◀️ Назад" in _texts(markup)
    assert GrabCB(tok=TOK, idx=0, ok=1).pack() in _datas(markup)
    assert ReleaseCB(tok=TOK, idx=0).pack() in _datas(markup)


@pytest.mark.asyncio
async def test_confirmed_rejected_release_is_grabbed():
    from bot.handlers import search
    from bot.ui.callbacks import GrabCB

    rejected = _arr_release(rejected=True, rejections=["x"])
    db = _db(_session([rejected]))
    cb = _cb()

    with patch.object(search, "get_services", AsyncMock(return_value=(MagicMock(), MagicMock()))), \
         patch.object(search, "grab_release", AsyncMock()) as gr:
        await search.handle_confirm_grab(cb, GrabCB(tok=TOK, idx=0, ok=1), _user(), db)

    gr.assert_awaited_once()


@pytest.mark.asyncio
async def test_grab_best_on_a_rejected_first_release_shows_the_rejection_card():
    from bot.handlers import search
    from bot.ui.callbacks import GrabBestCB, GrabCB

    rejected = _arr_release(rejected=True, rejections=["Russian Dub without English"])
    db = _db(_session([rejected]))
    cb = _cb()

    with patch.object(search, "get_services", AsyncMock(return_value=(MagicMock(), MagicMock()))), \
         patch.object(search, "grab_release", AsyncMock()) as gr:
        await search.handle_grab_best(cb, GrabBestCB(tok=TOK), _user(), db)

    gr.assert_not_awaited()
    text = cb.message.edit_text.await_args.args[0]
    assert "Russian Dub without English" in text
    assert GrabCB(tok=TOK, idx=0, ok=1).pack() in _datas(cb.message.edit_text.await_args.kwargs["reply_markup"])


def _failed(outcome: str, msg: str = "boom") -> ActionLog:
    a = ActionLog(user_id=0, action_type=ActionType.GRAB, content_type=ContentType.MOVIE,
                  success=False, error_message=msg)
    a.details = json.dumps({"outcome": outcome})
    return a


@pytest.mark.asyncio
async def test_unreachable_grab_offers_arr_auto_search_and_keeps_the_session():
    from bot.handlers import search
    from bot.ui.callbacks import AutoSearchCB

    session = _session([_arr_release()], selected_result=_arr_release())
    db = _db(session)
    message = MagicMock()
    message.edit_text = AsyncMock()
    add_service = MagicMock()
    add_service.grab_release = AsyncMock(return_value=(False, _failed(GRAB_OUTCOME_UNREACHABLE)))

    await search._execute_grab(message, session, _user(), db, MagicMock(), add_service)

    markup = message.edit_text.await_args.kwargs["reply_markup"]
    assert "🔍 Пусть Radarr ищет сам" in _texts(markup)
    assert AutoSearchCB(tok=TOK, idx=0).pack() in _datas(markup)
    db.delete_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejected_push_reports_the_reasons():
    from bot.handlers import search

    session = _session([_arr_release()], selected_result=_arr_release())
    db = _db(session)
    message = MagicMock()
    message.edit_text = AsyncMock()
    add_service = MagicMock()
    add_service.grab_release = AsyncMock(
        return_value=(False, _failed(GRAB_OUTCOME_REJECTED, "Unknown Movie")),
    )

    await search._execute_grab(message, session, _user(), db, MagicMock(), add_service)

    assert "Unknown Movie" in message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_auto_search_button_says_the_release_may_differ():
    from bot.handlers import search
    from bot.ui.callbacks import AutoSearchCB

    session = _session([_arr_release()], selected_result=_arr_release())
    db = _db(session)
    cb = _cb()
    add_service = MagicMock()
    add_service.auto_search = AsyncMock(return_value=(True, ActionLog(
        user_id=0, action_type=ActionType.GRAB, content_type=ContentType.MOVIE, success=True,
    )))
    add_service.radarr = AsyncMock()

    with patch.object(search, "get_services", AsyncMock(return_value=(MagicMock(), add_service))):
        await search.handle_auto_search(cb, AutoSearchCB(tok=TOK, idx=0), _user(), db)

    add_service.auto_search.assert_awaited_once()
    assert add_service.auto_search.await_args.kwargs["arr_id"] == 15
    text = cb.message.edit_text.await_args.args[0]
    assert "Radarr ищет сам — релиз может быть другим" in text


@pytest.mark.asyncio
async def test_old_auto_search_button_is_stale():
    from bot.handlers import search
    from bot.ui.callbacks import AutoSearchCB
    from bot.ui.list_token import STALE_LIST_TEXT

    db = _db(_session([_arr_release()]))
    cb = _cb()
    add_service = MagicMock()
    add_service.auto_search = AsyncMock()
    with patch.object(search, "get_services", AsyncMock(return_value=(MagicMock(), add_service))):
        await search.handle_auto_search(cb, AutoSearchCB(tok="old000", idx=0), _user(), db)

    add_service.auto_search.assert_not_awaited()
    assert cb.answer.await_args.args[0] == STALE_LIST_TEXT


# =============================================================== free search
def _free_session(release) -> SearchSession:
    return SearchSession(user_id=42, query="q", content_type=ContentType.UNKNOWN,
                         results=[release], free_search=True, list_token=TOK)


def _prowlarr_release(detected: ContentType) -> SearchResult:
    return SearchResult(guid="p1", title="Some.Release.1080p", origin="prowlarr", indexer_id=2,
                        download_url="http://localhost:9696/2/download", detected_type=detected)


def _ok_action() -> ActionLog:
    return ActionLog(user_id=0, action_type=ActionType.GRAB, content_type=ContentType.MOVIE, success=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "detected, expected",
    [
        (ContentType.MOVIE, ContentType.MOVIE),
        (ContentType.SERIES, ContentType.SERIES),
        (ContentType.ANIME, ContentType.ANIME),
    ],
)
async def test_free_grab_picks_the_arr_by_detected_type(detected, expected):
    from bot.handlers.search import free
    from bot.ui.callbacks import FindGrabCB

    add_service = MagicMock()
    add_service.qbittorrent = MagicMock()
    add_service.grab_release = AsyncMock(return_value=(True, _ok_action()))
    with patch.object(free._search, "get_services", AsyncMock(return_value=(MagicMock(), add_service))):
        await free.handle_find_grab(_cb(), FindGrabCB(tok=TOK, idx=0), _user(),
                                    _db(_free_session(_prowlarr_release(detected))))

    assert add_service.grab_release.await_args.args[1] is expected
    assert not add_service.grab_release.await_args.kwargs.get("force_download")


@pytest.mark.asyncio
async def test_free_grab_of_an_unknown_type_goes_to_qbit_and_says_so():
    from bot.handlers.search import free
    from bot.ui.callbacks import FindGrabCB

    add_service = MagicMock()
    add_service.qbittorrent = MagicMock()
    add_service.grab_release = AsyncMock(return_value=(True, _ok_action()))
    cb = _cb()
    with patch.object(free._search, "get_services", AsyncMock(return_value=(MagicMock(), add_service))):
        await free.handle_find_grab(cb, FindGrabCB(tok=TOK, idx=0), _user(),
                                    _db(_free_session(_prowlarr_release(ContentType.UNKNOWN))))

    assert add_service.grab_release.await_args.kwargs.get("force_download") is True
    assert "qBittorrent" in cb.message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_free_grab_rejected_offers_an_explicit_qbit_button():
    from bot.handlers.search import free
    from bot.ui.callbacks import FindGrabCB

    add_service = MagicMock()
    add_service.qbittorrent = MagicMock()
    add_service.grab_release = AsyncMock(return_value=(False, _failed(GRAB_OUTCOME_REJECTED, "Unknown Movie")))
    cb = _cb()
    db = _db(_free_session(_prowlarr_release(ContentType.MOVIE)))
    with patch.object(free._search, "get_services", AsyncMock(return_value=(MagicMock(), add_service))):
        await free.handle_find_grab(cb, FindGrabCB(tok=TOK, idx=0), _user(), db)

    last = cb.message.edit_text.await_args
    assert "Unknown Movie" in last.args[0]
    assert FindGrabCB(tok=TOK, idx=0, q=1).pack() in _datas(last.kwargs["reply_markup"])
    db.delete_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_free_grab_explicit_qbit_button_forces_the_download():
    from bot.handlers.search import free
    from bot.ui.callbacks import FindGrabCB

    add_service = MagicMock()
    add_service.qbittorrent = MagicMock()
    add_service.grab_release = AsyncMock(return_value=(True, _ok_action()))
    with patch.object(free._search, "get_services", AsyncMock(return_value=(MagicMock(), add_service))):
        await free.handle_find_grab(_cb(), FindGrabCB(tok=TOK, idx=0, q=1), _user(),
                                    _db(_free_session(_prowlarr_release(ContentType.MOVIE))))

    assert add_service.grab_release.await_args.kwargs.get("force_download") is True


def test_new_callbacks_fit_the_limit():
    from bot.ui.callbacks import AutoSearchCB, FindGrabCB

    assert len(AutoSearchCB(tok="zzzzzz", idx=499).pack().encode()) <= 64
    assert len(FindGrabCB(tok="zzzzzz", idx=499, q=1).pack().encode()) <= 64
