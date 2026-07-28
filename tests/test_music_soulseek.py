"""Tests for the Soulseek/Navidrome music additions (2026-07-28).

Shapes below are copied from real responses of the live slskd 0.24.5 instance
at 192.168.0.95:5030 (a "Metallica Master of Puppets" search).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from bot.clients.navidrome import NavidromeClient
from bot.clients.slskd import SlskdClient
from bot.models import SlskdFile, SlskdSearchResult, SlskdTransfer


def _slskd() -> SlskdClient:
    return SlskdClient("http://slskd.local:5030", "test-key", search_timeout=0.1)


SEARCH_RESPONSES = [
    {
        "username": "Asintok0",
        "hasFreeUploadSlot": False,
        "queueLength": 41,
        "uploadSpeed": 1121290,
        "files": [
            {
                "filename": "@@pamnw\\Albums\\Metallica - Master of Puppets (FLAC)\\01 - Battery.flac",
                "size": 37250263, "extension": "", "bitDepth": 16, "sampleRate": 48000, "length": 312,
            },
            {
                "filename": "@@pamnw\\Albums\\Metallica - Master of Puppets (FLAC)\\02 - Master.flac",
                "size": 41000000, "extension": "", "bitDepth": 16, "sampleRate": 48000, "length": 515,
            },
            {
                "filename": "@@pamnw\\Albums\\Metallica - Master of Puppets (FLAC)\\cover.jpg",
                "size": 120000, "extension": "", "length": None,
            },
        ],
    },
    {
        "username": "mp3guy",
        "hasFreeUploadSlot": True,
        "queueLength": 0,
        "uploadSpeed": 50000,
        "files": [
            {
                "filename": "Music\\Metallica\\Master of Puppets\\01 Battery.mp3",
                "size": 9000000, "extension": "mp3", "bitRate": 320, "length": 312,
            },
        ],
    },
]


# ------------------------------------------------------------------- slskd
def test_grouping_collapses_a_peers_folder_into_one_candidate():
    """Soulseek shares files, but a user downloads a *folder* — that grouping
    is what makes a result actionable."""
    client = _slskd()
    results = client._flatten_responses(SEARCH_RESPONSES)

    assert len(results) == 2
    flac = next(r for r in results if r.username == "Asintok0")
    assert flac.track_count == 2  # cover.jpg is not audio
    assert flac.dominant_format == "flac"
    assert flac.folder == "Metallica - Master of Puppets (FLAC)"
    assert flac.guessed_artist == "Metallica"


def test_non_audio_files_are_dropped():
    client = _slskd()
    results = client._flatten_responses(SEARCH_RESPONSES)
    assert all(f.extension in ("flac", "mp3") for r in results for f in r.files)


def test_lossless_ranks_above_lossy():
    client = _slskd()
    results = client._flatten_responses(SEARCH_RESPONSES)
    assert results[0].dominant_format == "flac"


def test_free_slot_wins_between_equal_formats():
    """A fast peer with 40 queued transfers is slower in practice."""
    client = _slskd()
    busy = dict(SEARCH_RESPONSES[1], username="busy", hasFreeUploadSlot=False, queueLength=30)
    results = client._flatten_responses([busy, SEARCH_RESPONSES[1]])
    assert results[0].username == "mp3guy"


def test_free_slot_filter_is_opt_in():
    client = _slskd()
    results = client._flatten_responses(SEARCH_RESPONSES, min_free_slot_only=True)
    assert [r.username for r in results] == ["mp3guy"]


@pytest.mark.asyncio
async def test_search_posts_then_polls_and_maps_results():
    client = _slskd()
    created = httpx.Response(200, json={"id": "s1", "isComplete": False},
                             request=httpx.Request("POST", "http://slskd.local/api/v0/searches"))
    completed = httpx.Response(
        200,
        json={"id": "s1", "isComplete": True, "state": "Completed", "responses": SEARCH_RESPONSES},
        request=httpx.Request("GET", "http://slskd.local/api/v0/searches/s1"),
    )
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(side_effect=[created, completed])) as req:
        results = await client.search("Metallica Master of Puppets")

    assert len(results) == 2
    assert req.await_args_list[0].kwargs["method"] == "POST"
    assert req.await_args_list[1].kwargs["params"] == {"includeResponses": "true"}


@pytest.mark.asyncio
async def test_search_uses_the_api_key_header():
    """slskd wants X-API-KEY, not the *arr X-Api-Key spelling."""
    client = _slskd()
    assert client._get_headers()["X-API-KEY"] == "test-key"
    assert "X-Api-Key" not in client._get_headers()


@pytest.mark.asyncio
async def test_enqueue_sends_filename_and_size_pairs():
    client = _slskd()
    files = [SlskdFile(filename="a\\b.flac", name="b.flac", size=123, extension="flac")]
    ok_response = httpx.Response(200, json={}, request=httpx.Request("POST", "http://slskd.local/x"))

    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(return_value=ok_response)) as req:
        ok = await client.enqueue("peer", files)

    assert ok is True
    assert req.await_args.kwargs["json"] == [{"filename": "a\\b.flac", "size": 123}]
    assert "/transfers/downloads/peer" in req.await_args.kwargs["url"]


@pytest.mark.asyncio
async def test_enqueue_reports_failure_instead_of_raising():
    client = _slskd()
    files = [SlskdFile(filename="a.flac", name="a.flac", size=1, extension="flac")]
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(side_effect=httpx.ConnectError("no"))):
        assert await client.enqueue("peer", files) is False


@pytest.mark.asyncio
async def test_enqueue_of_nothing_is_a_no_op():
    assert await _slskd().enqueue("peer", []) is False


@pytest.mark.asyncio
async def test_active_transfers_skips_completed_ones():
    client = _slskd()
    payload = [{
        "username": "peer",
        "directories": [{
            "files": [
                {"filename": "x\\a.flac", "state": "InProgress", "size": 100, "bytesTransferred": 40,
                 "averageSpeed": 1000},
                {"filename": "x\\b.flac", "state": "Completed, Succeeded", "size": 100,
                 "bytesTransferred": 100, "averageSpeed": 1000},
                {"filename": "x\\c.flac", "state": "Completed, Errored", "size": 100,
                 "bytesTransferred": 10, "averageSpeed": 0},
            ]
        }],
    }]
    response = httpx.Response(200, json=payload, request=httpx.Request("GET", "http://slskd.local/x"))
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(return_value=response)):
        transfers = await client.get_active_transfers()

    names = [t.filename for t in transfers]
    assert names == ["a.flac", "c.flac"]  # succeeded one is done, errored one still matters
    assert transfers[0].progress_percent == 40
    assert transfers[1].is_errored is True


@pytest.mark.asyncio
async def test_check_connection_requires_a_logged_in_soulseek_session():
    """A running slskd that is disconnected from Soulseek cannot download."""
    client = _slskd()
    offline = httpx.Response(
        200,
        json={"version": {"current": "0.24.5.0"}, "server": {"isLoggedIn": False}},
        request=httpx.Request("GET", "http://slskd.local/x"),
    )
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(return_value=offline)):
        ok, version, _ms = await client.check_connection()
    assert ok is False
    assert version == "0.24.5.0"


# --------------------------------------------------------------- navidrome
def _navidrome() -> NavidromeClient:
    return NavidromeClient("http://navidrome.local:4533", "user", "pw")


def test_navidrome_auth_never_puts_the_password_in_the_url():
    """Subsonic token auth: t = md5(password + salt), fresh salt per call."""
    client = _navidrome()
    first = client._auth_params()
    second = client._auth_params()

    assert "pw" not in first.values()
    assert first["u"] == "user"
    assert first["s"] != second["s"], "salt must be per-request"
    assert first["t"] != second["t"]


@pytest.mark.asyncio
async def test_navidrome_reports_a_matching_album():
    client = _navidrome()
    payload = {"subsonic-response": {"status": "ok", "searchResult3": {"album": [
        {"id": "1", "name": "Master of Puppets", "artist": "Metallica", "year": 1986, "songCount": 8},
    ]}}}
    response = httpx.Response(200, json=payload, request=httpx.Request("GET", "http://navidrome.local/x"))
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(return_value=response)):
        assert await client.has_album("Metallica", "Master of Puppets") is True


@pytest.mark.asyncio
async def test_navidrome_album_match_is_case_insensitive_and_partial():
    """Tagging differs between releases — an exact compare would report
    "not in library" for albums the user demonstrably has."""
    client = _navidrome()
    payload = {"subsonic-response": {"status": "ok", "searchResult3": {"album": [
        {"id": "1", "name": "Master Of Puppets (Remastered)", "artist": "METALLICA"},
    ]}}}
    response = httpx.Response(200, json=payload, request=httpx.Request("GET", "http://navidrome.local/x"))
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(return_value=response)):
        assert await client.has_album("metallica", "master of puppets") is True


@pytest.mark.asyncio
async def test_navidrome_error_status_is_not_treated_as_a_match():
    """Subsonic reports failures inside a 200 — same trap as Scryer's GraphQL."""
    client = _navidrome()
    payload = {"subsonic-response": {"status": "failed", "error": {"code": 40, "message": "Wrong username"}}}
    response = httpx.Response(200, json=payload, request=httpx.Request("GET", "http://navidrome.local/x"))
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(return_value=response)):
        assert await client.has_album("Metallica", "Master of Puppets") is False
        ok, _v, _ms = await client.check_connection()
    assert ok is False


# ---------------------------------------------------------------- handlers
@pytest.mark.asyncio
async def test_album_search_queues_the_selected_candidate():
    from bot.handlers import music
    from bot.ui.callbacks import SlskdCB

    candidate = SlskdSearchResult(
        username="peer",
        folder="Metallica - Master of Puppets",
        files=[SlskdFile(filename="a.flac", name="a.flac", size=100, extension="flac")],
    )
    music._slskd_candidates[42] = [candidate]

    slskd = AsyncMock()
    slskd.enqueue = AsyncMock(return_value=True)

    callback = MagicMock()
    callback.from_user = MagicMock(id=42)
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()
    db = AsyncMock()

    with patch.object(music, "get_slskd", AsyncMock(return_value=slskd)):
        await music.handle_slskd_selection(callback, SlskdCB(idx=0), MagicMock(tg_id=42), db)

    slskd.enqueue.assert_awaited_once_with("peer", candidate.files)
    db.log_action.assert_awaited_once()
    assert "очередь" in callback.message.edit_text.await_args.args[0].lower()
    music._slskd_candidates.clear()


@pytest.mark.asyncio
async def test_album_selection_with_a_stale_list_asks_to_search_again():
    from bot.handlers import music
    from bot.ui.callbacks import SlskdCB

    music._slskd_candidates.clear()
    callback = MagicMock()
    callback.from_user = MagicMock(id=42)
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    await music.handle_slskd_selection(callback, SlskdCB(idx=0), MagicMock(tg_id=42), AsyncMock())

    callback.message.edit_text.assert_not_awaited()
    assert callback.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_album_search_without_slskd_tells_the_user_how_to_enable_it():
    from bot.handlers import music

    message = MagicMock()
    message.answer = AsyncMock()

    with patch.object(music, "get_slskd", AsyncMock(return_value=None)):
        await music.process_soulseek_search(message, "Metallica", MagicMock(tg_id=42), AsyncMock())

    assert "SLSKD_URL" in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_navidrome_note_is_best_effort():
    """A Navidrome outage must never break the music flow."""
    from bot.handlers import music

    failing = AsyncMock()
    failing.has_album = AsyncMock(side_effect=RuntimeError("down"))
    with patch.object(music, "get_navidrome", AsyncMock(return_value=failing)):
        assert await music._navidrome_note("Metallica", "Master of Puppets") == ""

    with patch.object(music, "get_navidrome", AsyncMock(return_value=None)):
        assert await music._navidrome_note("Metallica") == ""


@pytest.mark.asyncio
async def test_downloads_appends_the_soulseek_section():
    from bot.handlers import downloads

    slskd = AsyncMock()
    slskd.get_active_transfers = AsyncMock(return_value=[
        SlskdTransfer(username="peer", filename="a.flac", state="InProgress",
                      size=100, transferred=25, average_speed=2048),
    ])

    with patch.object(downloads, "get_slskd", AsyncMock(return_value=slskd)):
        section = await downloads._soulseek_section()

    assert "Soulseek" in section
    assert "25%" in section


@pytest.mark.asyncio
async def test_downloads_survives_a_slskd_outage():
    from bot.handlers import downloads

    slskd = AsyncMock()
    slskd.get_active_transfers = AsyncMock(side_effect=RuntimeError("down"))

    with patch.object(downloads, "get_slskd", AsyncMock(return_value=slskd)):
        section = await downloads._soulseek_section()

    assert "недоступен" in section


@pytest.mark.asyncio
async def test_downloads_section_is_empty_without_slskd():
    from bot.handlers import downloads

    with patch.object(downloads, "get_slskd", AsyncMock(return_value=None)):
        assert await downloads._soulseek_section() == ""
