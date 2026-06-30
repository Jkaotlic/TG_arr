"""R4 C8 coverage tests — pin CURRENT (HEAD) behaviour for under-tested paths.

Covers:
  TEST-01  AuthMiddleware allow/deny + Settings.is_user_allowed / is_admin
  TEST-02  QBittorrentClient._request 403 -> re-auth -> retry state machine
  TEST-03  NotificationService._check_for_completions completion detection
  TEST-04  AddService.grab_movie_release happy paths (push / direct grab / qBit)
  TEST-07  Database.get_session auto-deletes a corrupt session row, returns None

These are NEW tests only — they do not touch any source file or any existing
test file. Assertions are robust to incidental added logging (we assert on
observable behaviour: call counts, return values, state), not on log lines.
"""

import json

import pytest
import pytest_asyncio
from aiogram.types import CallbackQuery, Message
from unittest.mock import AsyncMock, MagicMock, patch

from bot.config import get_settings
from bot.clients.qbittorrent import QBittorrentClient
from bot.db import Database
from bot.middleware.auth import AuthMiddleware
from bot.models import (
    MovieInfo,
    SearchResult,
    SearchSession,
    TorrentInfo,
    TorrentState,
    User,
    UserRole,
)
from bot.services.add_service import AddService
from bot.services.notification_service import NotificationService


# ---------------------------------------------------------------------------
# TEST-01: AuthMiddleware allow/deny + Settings.is_user_allowed / is_admin
# ---------------------------------------------------------------------------
# conftest sets ALLOWED_TG_IDS=123456789,987654321 and ADMIN_TG_IDS=123456789.
ALLOWED_ADMIN_ID = 123456789
ALLOWED_USER_ID = 987654321
UNKNOWN_ID = 555000111


def test_settings_is_user_allowed_and_is_admin():
    """Settings helpers reflect the configured allow/admin lists."""
    s = get_settings()
    assert s.is_user_allowed(ALLOWED_ADMIN_ID) is True
    assert s.is_user_allowed(ALLOWED_USER_ID) is True
    assert s.is_user_allowed(UNKNOWN_ID) is False

    assert s.is_admin(ALLOWED_ADMIN_ID) is True
    # a plain allowed (non-admin) user is not admin
    assert s.is_admin(ALLOWED_USER_ID) is False
    assert s.is_admin(UNKNOWN_ID) is False


def _make_message(user_id: int) -> Message:
    """A Message mock that passes isinstance(event, Message)."""
    event = MagicMock(spec=Message)
    tg_user = MagicMock()
    tg_user.id = user_id
    tg_user.username = "tester"
    tg_user.first_name = "Test"
    event.from_user = tg_user
    event.answer = AsyncMock()
    return event


@pytest.mark.asyncio
async def test_auth_middleware_allows_known_admin_and_flags_admin():
    """An allowed admin id passes through; handler runs and is_admin is True."""
    db = AsyncMock()
    # Already-existing user → no create_user call needed.
    db.get_user = AsyncMock(
        return_value=User(tg_id=ALLOWED_ADMIN_ID, role=UserRole.ADMIN)
    )
    db.create_user = AsyncMock()

    mw = AuthMiddleware(db)
    handler = AsyncMock(return_value="HANDLED")
    data: dict = {}
    event = _make_message(ALLOWED_ADMIN_ID)

    result = await mw(handler, event, data)

    assert result == "HANDLED"
    handler.assert_awaited_once()
    # middleware injected the admin flag + db into handler data
    assert data["is_admin"] is True
    assert data["db"] is db
    event.answer.assert_not_called()


@pytest.mark.asyncio
async def test_auth_middleware_allows_known_user_not_admin():
    """An allowed non-admin id passes through with is_admin False."""
    db = AsyncMock()
    db.get_user = AsyncMock(
        return_value=User(tg_id=ALLOWED_USER_ID, role=UserRole.USER)
    )
    db.create_user = AsyncMock()

    mw = AuthMiddleware(db)
    handler = AsyncMock(return_value="OK")
    data: dict = {}
    event = _make_message(ALLOWED_USER_ID)

    result = await mw(handler, event, data)

    assert result == "OK"
    assert data["is_admin"] is False
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_auth_middleware_rejects_unknown_user():
    """An unknown id is denied: handler NOT called, rejection message sent."""
    db = AsyncMock()
    db.get_user = AsyncMock()
    db.create_user = AsyncMock()

    mw = AuthMiddleware(db)
    handler = AsyncMock(return_value="SHOULD_NOT_RUN")
    data: dict = {}
    event = _make_message(UNKNOWN_ID)

    result = await mw(handler, event, data)

    assert result is None
    handler.assert_not_awaited()
    event.answer.assert_awaited_once()  # rejection message
    db.get_user.assert_not_called()  # never reaches DB for unknown user


@pytest.mark.asyncio
async def test_auth_middleware_rejects_unknown_callback():
    """Unknown id on a CallbackQuery is denied via answer(show_alert=True)."""
    db = AsyncMock()
    mw = AuthMiddleware(db)
    handler = AsyncMock(return_value="SHOULD_NOT_RUN")
    data: dict = {}

    event = MagicMock(spec=CallbackQuery)
    tg_user = MagicMock()
    tg_user.id = UNKNOWN_ID
    tg_user.username = "intruder"
    event.from_user = tg_user
    event.answer = AsyncMock()

    result = await mw(handler, event, data)

    assert result is None
    handler.assert_not_awaited()
    event.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_auth_middleware_creates_missing_allowed_user():
    """An allowed user with no DB row gets created with the right role."""
    db = AsyncMock()
    db.get_user = AsyncMock(return_value=None)  # not in DB yet
    db.create_user = AsyncMock(side_effect=lambda u: u)

    mw = AuthMiddleware(db)
    handler = AsyncMock(return_value="HANDLED")
    data: dict = {}
    event = _make_message(ALLOWED_ADMIN_ID)

    result = await mw(handler, event, data)

    assert result == "HANDLED"
    db.create_user.assert_awaited_once()
    created_user = db.create_user.await_args.args[0]
    assert created_user.tg_id == ALLOWED_ADMIN_ID
    assert created_user.role == UserRole.ADMIN


# ---------------------------------------------------------------------------
# TEST-02: QBittorrentClient._request 403 -> re-auth -> retry state machine
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_qbittorrent_request_403_reauth_then_retry_succeeds():
    """First API call returns 403 -> client clears auth, re-logs in, retries,
    second call returns 200 with a JSON body which is returned to caller."""
    client = QBittorrentClient("http://localhost:8080", "admin", "pw")
    # Pretend we start authenticated so _ensure_authenticated() is a no-op
    # until the 403 forces a re-login.
    client._authenticated = True

    resp_403 = MagicMock()
    resp_403.status_code = 403
    resp_403.text = "Forbidden"

    resp_ok = MagicMock()
    resp_ok.status_code = 200
    resp_ok.text = '{"version": "5.0.0"}'
    resp_ok.json.return_value = {"version": "5.0.0"}

    mock_http = AsyncMock()
    mock_http.request = AsyncMock(side_effect=[resp_403, resp_ok])

    login_calls = {"n": 0}

    async def fake_login():
        login_calls["n"] += 1
        client._authenticated = True
        return True

    with patch.object(client, "_get_client", new_callable=AsyncMock, return_value=mock_http), \
            patch.object(client, "login", side_effect=fake_login):
        result = await client._request("GET", "/api/v2/app/version")

    # Re-auth happened exactly once and the request was re-issued.
    assert login_calls["n"] == 1
    assert mock_http.request.await_count == 2
    assert result == {"version": "5.0.0"}
    assert client._authenticated is True


@pytest.mark.asyncio
async def test_qbittorrent_request_200_no_reauth():
    """A first-try 200 must NOT trigger re-login and returns the parsed body."""
    client = QBittorrentClient("http://localhost:8080", "admin", "pw")
    client._authenticated = True

    resp_ok = MagicMock()
    resp_ok.status_code = 200
    resp_ok.text = '{"ok": 1}'
    resp_ok.json.return_value = {"ok": 1}

    mock_http = AsyncMock()
    mock_http.request = AsyncMock(return_value=resp_ok)

    with patch.object(client, "_get_client", new_callable=AsyncMock, return_value=mock_http), \
            patch.object(client, "login", new_callable=AsyncMock) as mock_login:
        result = await client._request("GET", "/api/v2/app/version")

    mock_login.assert_not_called()
    assert mock_http.request.await_count == 1
    assert result == {"ok": 1}


# ---------------------------------------------------------------------------
# TEST-03: NotificationService completion detection (_check_for_completions)
# ---------------------------------------------------------------------------
def _torrent(hash_: str, progress: float, state: TorrentState, name: str = "T") -> TorrentInfo:
    return TorrentInfo(hash=hash_, name=name, progress=progress, state=state)


@pytest.mark.asyncio
async def test_notification_completion_fires_once_on_flip():
    """A tracked, not-yet-complete torrent that flips to complete triggers
    send_notification EXACTLY once; a re-check does not re-notify."""
    qbt = AsyncMock()
    sender = AsyncMock()
    svc = NotificationService(qbt, sender)
    svc.subscribe_user(42)

    # Tracked as still downloading and not yet notified.
    svc._tracked_torrents["abc"] = {
        "completed": False,
        "notified": False,
        "name": "Movie",
        "added_on": None,
    }

    completed = _torrent("abc", 1.0, TorrentState.COMPLETED, "Movie")
    qbt.get_torrents = AsyncMock(return_value=[completed])

    await svc._check_for_completions()
    assert sender.await_count == 1
    sent_user, sent_msg = sender.await_args.args
    assert sent_user == 42
    assert "Movie" in sent_msg
    assert svc._tracked_torrents["abc"]["completed"] is True
    assert svc._tracked_torrents["abc"]["notified"] is True

    # Second pass with the same complete torrent: no duplicate notification.
    await svc._check_for_completions()
    assert sender.await_count == 1


@pytest.mark.asyncio
async def test_notification_new_already_complete_does_not_notify():
    """A brand-new torrent that is ALREADY complete when first seen must not
    notify (it wasn't downloaded during this session)."""
    qbt = AsyncMock()
    sender = AsyncMock()
    svc = NotificationService(qbt, sender)
    svc.subscribe_user(42)

    already_done = _torrent("zzz", 1.0, TorrentState.COMPLETED, "OldThing")
    qbt.get_torrents = AsyncMock(return_value=[already_done])

    await svc._check_for_completions()

    sender.assert_not_called()
    # It is now tracked, recorded as completed + notified (suppressed).
    tracked = svc._tracked_torrents["zzz"]
    assert tracked["completed"] is True
    assert tracked["notified"] is True


# ---------------------------------------------------------------------------
# TEST-04: AddService.grab_movie_release happy paths with mocked clients
# ---------------------------------------------------------------------------
def _movie(radarr_id=None) -> MovieInfo:
    return MovieInfo(
        tmdb_id=123456,
        imdb_id="tt1234567",
        title="Test Movie",
        year=2024,
        radarr_id=radarr_id,
    )


def _public_release(**over) -> SearchResult:
    kwargs = dict(
        guid="rel-1",
        indexer="PublicIndexer",
        indexer_id=7,
        title="Test.Movie.2024.1080p.WEB-DL",
        size=5_000_000_000,
        protocol="torrent",
        # A public (resolvable, non-private) host so SSRF guard allows it.
        download_url="magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01",
    )
    kwargs.update(over)
    return SearchResult(**kwargs)


def _build_service(radarr=None, qbt=None) -> AddService:
    return AddService(
        prowlarr=AsyncMock(),
        radarr=radarr or AsyncMock(),
        sonarr=AsyncMock(),
        qbittorrent=qbt,
        lidarr=None,
    )


@pytest.mark.asyncio
async def test_grab_movie_push_approved_success():
    """push_release approved → success with the 'отправлен' message; no grab."""
    movie = _movie(radarr_id=42)
    radarr = AsyncMock()
    radarr.get_movie_by_tmdb = AsyncMock(return_value=movie)
    radarr.push_release = AsyncMock(return_value={"approved": True})
    radarr.grab_release = AsyncMock()

    svc = _build_service(radarr=radarr)
    ok, action, msg = await svc.grab_movie_release(
        movie=movie,
        release=_public_release(),
        quality_profile_id=1,
        root_folder_path="/movies",
    )

    assert ok is True
    assert action.success is True
    assert "отправлен" in msg.lower()
    radarr.push_release.assert_awaited_once()
    radarr.grab_release.assert_not_called()


@pytest.mark.asyncio
async def test_grab_movie_push_fails_then_direct_grab_success():
    """push_release raises APIError (not a rejection) → release_rejected stays
    False, so the direct grab_release branch runs and succeeds."""
    from bot.clients.base import APIError

    movie = _movie(radarr_id=42)
    radarr = AsyncMock()
    radarr.get_movie_by_tmdb = AsyncMock(return_value=movie)
    # An APIError from push (transient) must NOT mark the release rejected;
    # the code falls through to the direct-grab branch (indexer_id > 0).
    radarr.push_release = AsyncMock(side_effect=APIError("boom"))
    radarr.grab_release = AsyncMock(return_value=None)

    svc = _build_service(radarr=radarr)
    ok, action, msg = await svc.grab_movie_release(
        movie=movie,
        release=_public_release(),
        quality_profile_id=1,
        root_folder_path="/movies",
    )

    assert ok is True
    assert action.success is True
    assert "захвач" in msg.lower()
    radarr.push_release.assert_awaited_once()
    radarr.grab_release.assert_awaited_once()


@pytest.mark.asyncio
async def test_grab_movie_rejected_force_download_qbit_fallback():
    """Rejected by Radarr + force_download → qBittorrent fallback succeeds."""
    movie = _movie(radarr_id=42)
    radarr = AsyncMock()
    radarr.get_movie_by_tmdb = AsyncMock(return_value=movie)
    radarr.push_release = AsyncMock(
        return_value={"approved": False, "rejections": ["quality not allowed"]}
    )
    radarr.grab_release = AsyncMock()

    qbt = AsyncMock()
    qbt.add_torrent_url = AsyncMock(return_value=True)

    svc = _build_service(radarr=radarr, qbt=qbt)
    ok, action, msg = await svc.grab_movie_release(
        movie=movie,
        release=_public_release(),
        quality_profile_id=1,
        root_folder_path="/movies",
        force_download=True,
    )

    assert ok is True
    assert action.success is True
    assert "qbittorrent" in msg.lower()
    qbt.add_torrent_url.assert_awaited_once()
    # rejected → the direct-grab branch must be skipped
    radarr.grab_release.assert_not_called()
    # qBit was called with the radarr category
    assert qbt.add_torrent_url.await_args.kwargs.get("category") == "radarr"


# ---------------------------------------------------------------------------
# TEST-07: Database.get_session auto-deletes a corrupt session row -> None
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def db():
    database = Database(":memory:")
    await database.connect()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_get_session_corrupt_row_autodeletes_and_returns_none(db):
    """A session row whose session_data is not valid JSON is auto-deleted and
    get_session returns None."""
    user = User(tg_id=ALLOWED_ADMIN_ID)
    await db.create_user(user)

    # First store a valid session so the row exists, then corrupt it in place.
    valid = SearchSession(user_id=ALLOWED_ADMIN_ID, query="q", content_type="movie")
    await db.save_session(ALLOWED_ADMIN_ID, valid)

    # Corrupt the stored JSON directly via the connection.
    async with db._write_lock:
        await db.conn.execute(
            "UPDATE sessions SET session_data = ? WHERE user_id = ?",
            ("this-is-not-json{{{", ALLOWED_ADMIN_ID),
        )
        await db.conn.commit()

    # Sanity: the corrupt row is actually present before the read.
    async with db.conn.execute(
        "SELECT session_data FROM sessions WHERE user_id = ?", (ALLOWED_ADMIN_ID,)
    ) as cur:
        pre = await cur.fetchone()
    assert pre is not None

    result = await db.get_session(ALLOWED_ADMIN_ID)
    assert result is None

    # The corrupt row was deleted as a side effect.
    async with db.conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE user_id = ?", (ALLOWED_ADMIN_ID,)
    ) as cur:
        post = await cur.fetchone()
    assert post["n"] == 0


@pytest.mark.asyncio
async def test_get_session_valid_json_but_wrong_schema_autodeletes(db):
    """A row that is valid JSON but does not validate as a SearchSession is
    also treated as corrupt: deleted, returns None."""
    user = User(tg_id=ALLOWED_USER_ID)
    await db.create_user(user)

    valid = SearchSession(user_id=ALLOWED_USER_ID, query="q", content_type="movie")
    await db.save_session(ALLOWED_USER_ID, valid)

    bad_payload = json.dumps({"not": "a session", "totally": "wrong"})
    async with db._write_lock:
        await db.conn.execute(
            "UPDATE sessions SET session_data = ? WHERE user_id = ?",
            (bad_payload, ALLOWED_USER_ID),
        )
        await db.conn.commit()

    result = await db.get_session(ALLOWED_USER_ID)
    assert result is None

    async with db.conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE user_id = ?", (ALLOWED_USER_ID,)
    ) as cur:
        post = await cur.fetchone()
    assert post["n"] == 0
