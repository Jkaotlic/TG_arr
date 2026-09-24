"""SlskdClient.get_transfers keeps identity, directory and timestamps (audit 2026-09-23, NOTIF-01/02).

Fixture shape copied from the live slskd `GET /api/v0/transfers/downloads`
response captured 2026-09-24 (names anonymised).
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from bot.clients.slskd import SlskdClient


def _file(fid: str, directory: str, name: str, state: str, **extra):
    data = {
        "batchId": "934fac87-0000-0000-0000-000000000001",
        "id": fid,
        "username": "peer1",
        "direction": "Download",
        "filename": f"{directory}\\{name}",
        "size": 15343009,
        "state": state,
        "requestedAt": "2026-08-31T20:56:23.0620005",
        "startedAt": "2026-08-31T20:56:23.3947604Z",
        "endedAt": "2026-08-31T20:56:23.3947604Z",
        "bytesTransferred": 0,
        "exception": "Transfer rejected: Banned",
        "attempts": 1,
        "removed": False,
        "percentComplete": 0,
    }
    data.update(extra)
    return data


CD1 = "music_readonly\\Artist - Album (2023) [FLAC]\\CD1"
CD2 = "music_readonly\\Artist - Album (2023) [FLAC]\\CD2"

PAYLOAD = [{
    "username": "peer1",
    "directories": [
        {"directory": CD1, "fileCount": 1,
         "files": [_file("1f39d753-a", CD1, "06 - Track.flac", "Completed, Rejected")]},
        {"directory": CD2, "fileCount": 1,
         "files": [_file("1f39d753-b", CD2, "06 - Track.flac", "Completed, Succeeded",
                         exception=None, endedAt=None)]},
    ],
}]


async def _transfers():
    client = SlskdClient("http://slskd.example.com:5030", "test-key", search_timeout=0.1)
    response = httpx.Response(200, json=PAYLOAD, request=httpx.Request("GET", "http://slskd.example.com/x"))
    with patch.object(httpx.AsyncClient, "request", new=AsyncMock(return_value=response)):
        return await client.get_transfers(include_completed=True)


@pytest.mark.asyncio
async def test_transfers_carry_identity_directory_and_raw_filename():
    first, second = await _transfers()

    assert first.id == "1f39d753-a"
    assert first.batch_id == "934fac87-0000-0000-0000-000000000001"
    assert first.directory == CD1
    assert first.full_filename == f"{CD1}\\06 - Track.flac"
    assert first.filename == "06 - Track.flac"  # existing callers keep the basename
    assert first.exception == "Transfer rejected: Banned"

    # Same basename in two directories stays distinguishable.
    assert second.directory == CD2
    assert second.full_filename != first.full_filename
    assert second.exception == ""


@pytest.mark.asyncio
async def test_timestamps_parse_with_and_without_z_as_utc():
    first, second = await _transfers()

    assert first.ended_at == datetime(2026, 8, 31, 20, 56, 23, 394760, tzinfo=timezone.utc)
    # requestedAt comes without "Z" — naive is treated as UTC, not local time.
    assert first.requested_at is not None
    assert first.requested_at.tzinfo is not None
    assert first.requested_at.utcoffset() == timezone.utc.utcoffset(None)
    assert first.requested_at.replace(microsecond=0) == datetime(2026, 8, 31, 20, 56, 23, tzinfo=timezone.utc)
    assert second.ended_at is None
