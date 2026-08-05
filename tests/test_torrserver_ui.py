"""Кнопки и тексты раздела TorrServer."""

from bot.models import (
    SyncHookResult,
    TorrServerFile,
    TorrServerRelease,
    TorrServerStats,
    TorrServerTorrent,
)
from bot.services.torrserver_service import AddResult
from bot.ui.formatters import Formatters
from bot.ui.keyboards import CallbackData, Keyboards
from bot.ui.menu import MENU_BUTTONS, MENU_TORRSERVER, TORRSERVER_PROMPT

RELEASE = TorrServerRelease(
    title="Dune 2021 BDRemux 1080p", size=2 * 1024 ** 3, seeders=42, peers=7,
    link="http://p:9696/2/download?link=a", tracker="RuTracker.org", year=2021,
)
TORRENT = TorrServerTorrent(
    hash="abc123", title="Dune 2021", category="movie", size=2 * 1024 ** 3,
    stat=3, stat_string="Torrent working",
    files=[TorrServerFile(id=1, path="Dune/Dune.2021.mkv", length=2 * 1024 ** 3)],
)


def test_menu_button_is_registered_in_the_button_set():
    """Иначе текст кнопки уйдёт в обычный поиск как поисковый запрос."""
    assert MENU_TORRSERVER in MENU_BUTTONS


def test_torrserver_prompt_is_a_non_empty_string():
    """Handler ForceReply matching relies on this exact constant existing."""
    assert TORRSERVER_PROMPT


def test_main_menu_contains_the_button():
    texts = [b.text for row in Keyboards.main_menu().keyboard for b in row]
    assert MENU_TORRSERVER in texts


def test_panel_has_search_and_list_buttons():
    data = [b.callback_data for row in Keyboards.torrserver_panel().inline_keyboard for b in row]
    assert CallbackData.TS_SEARCH in data
    assert CallbackData.TS_LIST in data
    assert CallbackData.TS_CLOSE in data


def test_results_keyboard_has_one_button_per_release_and_pagination():
    releases = [RELEASE] * 5
    markup = Keyboards.torrserver_results(releases, page=0, total_pages=3)
    flat = [b for row in markup.inline_keyboard for b in row]
    assert sum(1 for b in flat if b.callback_data.startswith("tsr:")) == 5
    assert any(b.callback_data.startswith("tsp:") for b in flat)


def test_list_keyboard_shows_delete_only_for_admins():
    admin_markup = Keyboards.torrserver_list([TORRENT], is_admin=True)
    user_markup = Keyboards.torrserver_list([TORRENT], is_admin=False)
    admin_data = [b.callback_data for row in admin_markup.inline_keyboard for b in row]
    user_data = [b.callback_data for row in user_markup.inline_keyboard for b in row]
    assert any(d.startswith("tst:del:") for d in admin_data)
    assert not any(d.startswith("tst:del:") for d in user_data)


def _torrents(n):
    """`n` distinct torrents — enough of them to blow past Telegram's message
    limit and the keyboard's per-message button budget."""
    return [
        TorrServerTorrent(
            hash=f"{i:040d}", title=f"Раздача номер {i} 2021 BDRemux 1080p WEB-DL x264",
            category="movie", size=2 * 1024 ** 3, stat=5, stat_string="Torrent in db",
            files=[TorrServerFile(id=1, path=f"Item{i}/movie.mkv", length=2 * 1024 ** 3)],
        )
        for i in range(n)
    ]


def test_torrents_text_is_truncated_well_under_the_telegram_limit():
    """At ~115 chars/torrent, 40 torrents would exceed Telegram's 4096-char
    message limit outright; the formatter must apply the same safety net
    every other list formatter in the codebase uses."""
    text = Formatters.format_torrserver_torrents(_torrents(40))
    assert len(text) <= 3800
    assert "truncated" in text


def test_list_keyboard_caps_delete_buttons():
    """35 torrents must not produce 35 delete buttons — Telegram rejects a
    keyboard that large, and the delete-confirmation callback adds ~70 more
    chars per button on top."""
    markup = Keyboards.torrserver_list(_torrents(35), is_admin=True)
    delete_buttons = [
        b for row in markup.inline_keyboard for b in row
        if b.callback_data.startswith("tst:del:")
    ]
    assert len(delete_buttons) <= 30


def test_torrents_text_notes_the_cut_for_admins():
    text = Formatters.format_torrserver_torrents(_torrents(35), is_admin=True)
    assert "первых 30" in text


def test_torrents_text_has_no_admin_cap_note_for_regular_users():
    text = Formatters.format_torrserver_torrents(_torrents(35), is_admin=False)
    assert "удален" not in text.lower()


def test_status_text_mentions_version_and_cache_mode():
    stats = TorrServerStats(version="MatriX.142.2", torrent_count=6,
                            total_size=10 * 1024 ** 3, cache_size=1610612736,
                            use_disk=False, source_count=6)
    text = Formatters.format_torrserver_status(stats)
    assert "MatriX.142.2" in text
    assert "6" in text
    assert "RAM" in text


def test_release_text_escapes_html():
    dangerous = TorrServerRelease(title="<b>Dune</b> & Co", link="http://x", seeders=1)
    text = Formatters.format_torrserver_release(dangerous)
    assert "&lt;b&gt;Dune&lt;/b&gt;" in text
    assert "&amp;" in text


def test_added_text_reports_stream_link_and_emby():
    result = AddResult(torrent=TORRENT, metadata_ready=True,
                       sync=SyncHookResult(status="ok", duration_s=3.0),
                       stream_url="http://ts:8090/stream/Dune.2021.mkv?link=abc123&index=1&play")
    text = Formatters.format_torrserver_added(result)
    assert "Dune" in text
    assert "stream" in text
    assert "Emby" in text


def test_added_text_explains_a_failed_sync():
    result = AddResult(torrent=TORRENT, metadata_ready=True,
                       sync=SyncHookResult(status="failed", error="хук недоступен"),
                       stream_url=None)
    text = Formatters.format_torrserver_added(result)
    assert "10 минут" in text


def test_added_text_explains_a_metadata_timeout():
    result = AddResult(torrent=TORRENT, metadata_ready=False)
    text = Formatters.format_torrserver_added(result)
    assert "10 минут" in text
