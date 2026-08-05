"""Чистые функции и модели раздела TorrServer.

Размер в выдаче поиска приходит строкой вида "2.5 GCiB" (проверено на живом
сервере 2026-08-05), а слэш в названии раздачи ломает листинг WebDAV/DLNA —
обе особенности закрыты здесь.
"""

import pytest

from bot.models import (
    TorrServerFile,
    TorrServerTorrent,
    parse_torrserver_size,
    sanitize_torrent_title,
)


@pytest.mark.parametrize("raw,expected", [
    ("2.5 GCiB", int(2.5 * 1024 ** 3)),
    ("1.4 GCiB", int(1.4 * 1024 ** 3)),
    ("512 MCiB", 512 * 1024 ** 2),
    ("700 KCiB", 700 * 1024),
    ("123 B", 123),
    ("2,5 GCiB", int(2.5 * 1024 ** 3)),
])
def test_parse_size_from_torrserver_strings(raw, expected):
    assert parse_torrserver_size(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "nonsense", "GCiB"])
def test_parse_size_falls_back_to_zero(raw):
    assert parse_torrserver_size(raw) == 0


def test_parse_size_passes_through_numbers():
    assert parse_torrserver_size(276445467) == 276445467


def test_sanitize_replaces_slashes_that_break_webdav():
    raw = "Холодное сердце 2 / Frozen II [2019, WEB-DL 1080p]"
    assert sanitize_torrent_title(raw) == "Холодное сердце 2 - Frozen II [2019, WEB-DL 1080p]"


def test_sanitize_replaces_backslash_and_control_chars():
    assert sanitize_torrent_title("A\\B\tC") == "A - B C"


def test_sanitize_collapses_whitespace_and_trims():
    assert sanitize_torrent_title("  Dune    2021  ") == "Dune 2021"


def test_sanitize_truncates_long_titles():
    assert len(sanitize_torrent_title("x" * 400)) == 200


def test_video_files_are_filtered_and_sorted_by_size():
    torrent = TorrServerTorrent(
        hash="abc", title="T",
        files=[
            TorrServerFile(id=1, path="show/poster.jpg", length=10),
            TorrServerFile(id=2, path="show/ep1.mkv", length=100),
            TorrServerFile(id=3, path="show/ep2.mkv", length=300),
            TorrServerFile(id=4, path="show/sub.srt", length=5),
        ],
    )
    assert [f.id for f in torrent.video_files] == [3, 2]
