"""Audit 2026-09-23, Task C3: wording and admin-awareness.

UI-12 «Авто-граб» ничего не граббит — название врало; «≥ 80» в описании.
UI-15 пресеты сезонов сырые («latestSeason»), у аниме нет кнопки.
UI-16 админские кнопки видны всем; «▶️ Возобновить» = все; «🗑 Удалить» без
подтверждения; перезагрузка Emby спрашивала подтверждение у не-админа.
UI-20 разнобой «Назад/Закрыть/Отмена», стрелок и ⚡.
"""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.models import (
    ContentType,
    SearchResult,
    SearchSession,
    SeriesInfo,
    TorrentInfo,
    TorrentState,
    User,
    UserPreferences,
)


def _texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def _buttons(kb):
    return [b for row in kb.inline_keyboard for b in row]


def _cb(user_id: int = 42) -> MagicMock:
    cb = MagicMock()
    cb.from_user = MagicMock(id=user_id)
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    return cb


# ---------------------------------------------------------------- UI-12
def test_settings_menu_names_the_grab_best_button_with_state():
    from bot.ui.keyboards import Keyboards

    on = _texts(Keyboards.settings_menu(prefs=UserPreferences(auto_grab_enabled=True)))
    off = _texts(Keyboards.settings_menu(prefs=UserPreferences(auto_grab_enabled=False)))
    assert "⚡ Кнопка «Скачать лучший»: вкл" in on
    assert "⚡ Кнопка «Скачать лучший»: выкл" in off
    assert not any("Авто-граб" in t for t in on + off)


def test_toggle_button_uses_the_same_name():
    from bot.ui.keyboards import Keyboards

    assert _texts(Keyboards.auto_grab_toggle(True))[0] == "⚡ Кнопка «Скачать лучший»: вкл"
    assert _texts(Keyboards.auto_grab_toggle(False))[0] == "⚡ Кнопка «Скачать лучший»: выкл"


@pytest.mark.asyncio
async def test_grab_best_description_states_the_real_gate():
    from bot.handlers import settings as settings_mod

    cb = _cb()
    await settings_mod.handle_auto_grab_menu(cb, User(tg_id=42))
    text = cb.message.edit_text.await_args.args[0]
    assert "≥ 80" not in text and "80" not in text
    assert "принят профилем" in text and "CF ≥ 0" in text
    assert "Скачать лучший" in text


def test_settings_summary_uses_new_name():
    from bot.ui.formatters import Formatters

    text = Formatters.format_user_preferences(UserPreferences(auto_grab_enabled=True), [], [], [], [])
    assert "Авто-граб" not in text
    assert "Кнопка «Скачать лучший»: вкл" in text


def test_results_button_is_named_like_the_setting():
    from bot.ui.keyboards import Keyboards

    r = SearchResult(guid="g", title="T", indexer_id=1, size=1)
    texts = _texts(Keyboards.search_results([r], 0, 1, show_grab_best=True, best_score=250, list_token="t"))
    assert any(t.startswith("⚡ Скачать лучший") for t in texts)


def test_lightning_means_only_grab_best():
    from bot.ui.keyboards import Keyboards

    r = SearchResult(guid="g", title="T", indexer_id=1, size=1)
    card = _texts(Keyboards.release_details(r, ContentType.MOVIE, show_force_grab=True))
    failed = _texts(Keyboards.free_grab_failed("t", 0, offer_qbit=True))
    assert not any(t.startswith("⚡") for t in card + failed)


# ---------------------------------------------------------------- UI-15
def test_season_preset_names_are_human():
    from bot.ui.formatters import season_preset_name

    assert season_preset_name("latestSeason") == "последний сезон"
    assert season_preset_name("firstSeason") == "первый сезон"
    assert season_preset_name("future") == "только будущие"
    assert season_preset_name("all") == "все сезоны"
    assert season_preset_name("none") == "нет"
    assert season_preset_name(None) == "авто (по релизу)"


def test_anime_card_has_season_monitoring_button():
    from bot.ui.keyboards import Keyboards

    r = SearchResult(guid="g", title="T", indexer_id=1, size=1)
    assert "📺 Мониторинг сезонов" in _texts(Keyboards.release_details(r, ContentType.ANIME))
    assert "📺 Мониторинг сезонов" in _texts(Keyboards.release_details(r, ContentType.SERIES))
    assert "📺 Мониторинг сезонов" not in _texts(Keyboards.release_details(r, ContentType.MOVIE))


def _db(session):
    db = MagicMock()
    db.get_session = AsyncMock(return_value=session)
    db.update_session = AsyncMock(return_value=True)

    @contextlib.asynccontextmanager
    async def _lock(_uid):
        yield

    db.session_lock = _lock
    return db


def _series_session(**kw) -> SearchSession:
    r = SearchResult(guid="g", title="Show.S01", indexer_id=1, size=1)
    series = SeriesInfo(tvdb_id=1, title="Show", sonarr_id=5, overview="About the show")
    return SearchSession(
        user_id=42, query="show", content_type=kw.pop("content_type", ContentType.SERIES),
        results=[r], selected_result=r, selected_content=series, title_created=True,
        list_token="tok", **kw,
    )


@pytest.mark.asyncio
async def test_season_menu_shows_human_current_preset():
    from bot.handlers.search import grab
    from bot.ui.callbacks import SeasonMenuCB

    cb = _cb()
    await grab.handle_season_menu(
        cb, SeasonMenuCB(tok="tok"), User(tg_id=42), _db(_series_session(monitor_type="latestSeason")),
    )
    text = cb.message.edit_text.await_args.args[0]
    assert "последний сезон" in text and "latestSeason" not in text


@pytest.mark.asyncio
async def test_season_preset_answer_and_card_are_human_and_full():
    from bot.handlers.search import grab
    from bot.ui.callbacks import SeasonPresetCB

    cb = _cb()
    services = (MagicMock(), MagicMock(qbittorrent=None))
    with patch.object(grab._search, "get_services", AsyncMock(return_value=services)):
        await grab.handle_season_preset(cb, SeasonPresetCB(tok="tok", preset="firstSeason"), User(tg_id=42),
                                        _db(_series_session()))
    assert "первый сезон" in cb.answer.await_args.args[0]
    text = cb.message.edit_text.await_args.args[0]
    assert "первый сезон" in text and "firstSeason" not in text
    assert "Show" in text and "---" in text, "back to the full card: release + title info"


@pytest.mark.asyncio
async def test_season_back_renders_full_card():
    from bot.handlers.search import grab
    from bot.ui.callbacks import SeasonBackCB

    cb = _cb()
    services = (MagicMock(), MagicMock(qbittorrent=None))
    with patch.object(grab._search, "get_services", AsyncMock(return_value=services)):
        await grab.handle_season_back(cb, SeasonBackCB(tok="tok"), User(tg_id=42), _db(_series_session()))
    text = cb.message.edit_text.await_args.args[0]
    assert "---" in text and "Show" in text


# ---------------------------------------------------------------- /title wording (controller ruling)
def test_title_series_monitor_button_says_what_it_does():
    from bot.ui.keyboards import Keyboards

    series = SeriesInfo(tvdb_id=1, title="S", sonarr_id=3, monitored=False)
    assert "🔔 Следить за новыми сериями" in _texts(Keyboards.title_actions(series))


# ---------------------------------------------------------------- UI-16
def _torrent(state=TorrentState.DOWNLOADING) -> TorrentInfo:
    return TorrentInfo(hash="a" * 40, name="T", size=1, progress=0.5, state=state)


def test_torrent_list_hides_blanket_actions_from_non_admin():
    from bot.ui.keyboards import Keyboards

    # Round 2 / D2: the blanket actions live in the "⋯ Ещё" panel.
    admin = _texts(Keyboards.torrent_more(is_admin=True))
    user = _texts(Keyboards.torrent_more(is_admin=False))
    assert "▶️ Возобновить все" in admin and "⏸ Пауза всех" in admin
    assert "▶️ Возобновить все" not in user and "⏸ Пауза всех" not in user


def test_torrent_card_hides_delete_from_non_admin():
    from bot.ui.keyboards import Keyboards

    admin = _texts(Keyboards.torrent_details(_torrent(), is_admin=True))
    user = _texts(Keyboards.torrent_details(_torrent(), is_admin=False))
    assert "🗑 Удалить" in admin
    assert not any(t.startswith("🗑") for t in user)


@pytest.mark.asyncio
async def test_plain_delete_asks_confirmation_first():
    from bot.handlers import downloads
    from bot.ui.callbacks import TorrentActionCB

    t = _torrent()
    qbt = MagicMock()
    qbt.get_torrent = AsyncMock(return_value=t)
    qbt.delete = AsyncMock()
    cb = _cb()
    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_torrent_action(cb, TorrentActionCB(action="delete", h=t.hash), is_admin=True)
    qbt.delete.assert_not_called()
    kb = cb.message.edit_text.await_args.kwargs["reply_markup"]
    confirm = TorrentActionCB.unpack(_buttons(kb)[0].callback_data)
    assert confirm.action == "delc"

    cb2 = _cb()
    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        qbt.get_torrents = AsyncMock(return_value=[])
        await downloads.handle_torrent_action(cb2, confirm, is_admin=True)
    qbt.delete.assert_awaited_once_with([t.hash], delete_files=False)


@pytest.mark.asyncio
async def test_confirmed_delete_is_admin_only():
    from bot.handlers import downloads
    from bot.ui.callbacks import TorrentActionCB

    cb = _cb()
    await downloads.handle_torrent_action(cb, TorrentActionCB(action="delc", h="a" * 40), is_admin=False)
    assert cb.answer.await_args.kwargs.get("show_alert")


def test_emby_panel_admin_buttons_hidden_for_users():
    from bot.ui.keyboards import Keyboards

    admin = _texts(Keyboards.emby_main(has_update=True, is_admin=True))
    user = _texts(Keyboards.emby_main(has_update=True, is_admin=False))
    assert "🔁 Перезагрузить" in admin and "📚 Сканировать всё" in admin
    for t in ("🔁 Перезагрузить", "📚 Сканировать всё", "⬆️ Установить обновление", "🎬 Фильмы"):
        assert t not in user


@pytest.mark.asyncio
async def test_emby_restart_prompt_checks_rights_first():
    from bot.handlers import emby

    cb = _cb()
    await emby.handle_restart_prompt(cb, is_admin=False)
    cb.message.edit_text.assert_not_called()
    assert cb.answer.await_args.kwargs.get("show_alert")

    cb2 = _cb()
    await emby.handle_update_prompt(cb2, is_admin=False)
    cb2.message.edit_text.assert_not_called()


# ---------------------------------------------------------------- UI-20
def test_arrows_and_close_are_unified():
    from bot.models import TorrServerRelease
    from bot.ui.keyboards import CallbackData, Keyboards
    from bot.ui.keyboards._constants import BTN_BACK, BTN_CLOSE

    rel = TorrServerRelease(title="R", link="magnet:?xt=urn:btih:" + "a" * 40)
    ts = _texts(Keyboards.torrserver_results([rel] * 5, page=1, total_pages=3))
    assert "◀️" in ts and "▶️" in ts and BTN_BACK in ts
    assert not any("⬅️" in t or "➡️" in t for t in ts)
    assert BTN_BACK in _texts(Keyboards.torrserver_list([], is_admin=False))
    assert not any("⬅️" in t for t in _texts(Keyboards.torrserver_confirm_delete("a" * 40)))

    for kb in (Keyboards.torrent_more(is_admin=True), Keyboards.emby_main(is_admin=True)):
        close = [b for b in _buttons(kb) if b.text == BTN_CLOSE]
        assert len(close) == 1 and close[0].callback_data == CallbackData.CLOSE


def test_emby_command_description():
    from bot.ui.commands import COMMAND_GROUPS

    desc = dict(e for _, entries in COMMAND_GROUPS for e in entries)
    assert desc["emby"] == "Панель Emby"


@pytest.mark.asyncio
async def test_list_staleness_uses_msg_stale():
    from bot.handlers import torrserver
    from bot.handlers.search import free
    from bot.ui.callbacks import FindPageCB, TsPageCB
    from bot.ui.keyboards._constants import MSG_STALE

    cb = _cb()
    await torrserver.handle_page(cb, TsPageCB(page=1))
    assert cb.answer.await_args.args[0] == MSG_STALE

    cb2 = _cb()
    db = MagicMock(get_session=AsyncMock(return_value=None))
    await free.handle_find_page(cb2, FindPageCB(page=1), User(tg_id=42), db)
    assert cb2.answer.await_args.args[0] == MSG_STALE
