"""Подделка, которая молча пропускает нарушения, хуже её отсутствия."""

import asyncio
from pathlib import Path

import httpx
import pytest
from aiogram import F, methods
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    ReplyParameters,
)

from bot.middleware.auth import AuthMiddleware, LoggingMiddleware, RateLimitMiddleware, StaleMessageMiddleware
from tests.e2e.conftest import reset_process_state
from tests.e2e.fakes import (
    BotEnv,
    FakeBackend,
    TelegramContractViolation,
    buttons,
    find_button,
    parse_telegram_html,
    validate_keyboard,
)

ADMIN = BotEnv.ADMIN


@pytest.mark.parametrize(
    ("markup", "plain"),
    [
        ("<b>жирный</b> и <i>наклонный</i>", "жирный и наклонный"),
        ('<a href="https://example.com/?a=1&b=2">ссылка</a>', "ссылка"),
        ('<span class="tg-spoiler">x</span><tg-spoiler>y</tg-spoiler>', "xy"),
        ('<pre><code class="language-python">x</code></pre>', "x"),
        ("<blockquote expandable>x</blockquote>", "x"),
        ("&lt;a&gt; &amp; &quot;b&quot; &#128512; &#x41;", '<a> & "b" \U0001F600 A'),
        ("без разметки", "без разметки"),
    ],
)
def test_valid_html_is_accepted(markup, plain):
    assert parse_telegram_html(markup) == plain


@pytest.mark.parametrize(
    "markup",
    [
        "<b>не закрыт",
        "закрыт</b>",
        "<b><i>вперехлёст</b></i>",
        "<div>чужой тег</div>",
        "<br/>",
        "a < b",
        "a > b",
        "R&D",
        "&nbsp;",
        "<b",
        "<a>без href</a>",
        '<b class="x">атрибут</b>',
        '<span class="x">не спойлер</span>',
        '<code class="x">x</code>',
        "<!-- комментарий -->",
    ],
)
def test_invalid_html_is_rejected(markup):
    with pytest.raises(TelegramContractViolation):
        parse_telegram_html(markup)


@pytest.mark.parametrize(
    "button",
    [
        InlineKeyboardButton(text="оба", url="https://example.com", callback_data="x"),
        InlineKeyboardButton(text="ни одного"),
        InlineKeyboardButton(text="длинный", callback_data="x" * 65),
        InlineKeyboardButton(text="байты", callback_data="ю" * 33),
    ],
)
def test_bad_keyboards_are_rejected(button):
    with pytest.raises(TelegramContractViolation):
        validate_keyboard(InlineKeyboardMarkup(inline_keyboard=[[button]]))


@pytest.mark.parametrize("url", ["example.com", "ftp://example.com", "https://", "javascript:alert(1)", "tg:"])
def test_invalid_button_urls_are_rejected(url):
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="x", url=url)]])
    with pytest.raises(TelegramContractViolation, match="url"):
        validate_keyboard(markup)


@pytest.mark.parametrize("url", ["https://example.com/a?b=1", "http://example.com", "tg://resolve?domain=example"])
def test_valid_button_urls_are_accepted(url):
    validate_keyboard(InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="x", url=url)]]))


def test_keyboard_of_more_than_100_buttons_is_rejected():
    row = [InlineKeyboardButton(text="x", callback_data="x")]
    validate_keyboard(InlineKeyboardMarkup(inline_keyboard=[row] * 100))
    with pytest.raises(TelegramContractViolation, match="101"):
        validate_keyboard(InlineKeyboardMarkup(inline_keyboard=[row] * 101))


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [[]],
        [[KeyboardButton(text="")]],
    ],
)
def test_bad_reply_keyboards_are_rejected(rows):
    with pytest.raises(TelegramContractViolation):
        validate_keyboard(ReplyKeyboardMarkup(keyboard=rows))


def test_good_reply_keyboard_and_service_markups_are_accepted():
    validate_keyboard(ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="ok")]]))
    validate_keyboard(ForceReply())
    validate_keyboard(ReplyKeyboardRemove())


def test_longest_valid_callback_data_is_accepted():
    button = InlineKeyboardButton(text="ok", callback_data="x" * 64)
    validate_keyboard(InlineKeyboardMarkup(inline_keyboard=[[button]]))


async def _send(env: BotEnv, text: str, **kwargs):
    return await env.bot.send_message(ADMIN, text, **kwargs)


async def test_text_limit_counts_parsed_text(bot_env):
    # 4096 символов после разбора: разметка в лимит не входит.
    await _send(bot_env, "<b>" + "x" * 4096 + "</b>")
    assert bot_env.tg.violations == []

    with pytest.raises(TelegramContractViolation):
        await _send(bot_env, "x" * 4097)
    assert len(bot_env.tg.violations) == 1
    bot_env.tg.violations.clear()


async def test_plain_parse_mode_skips_html_check(bot_env):
    message = await _send(bot_env, "a < b & c", parse_mode=None)
    assert message.text == "a < b & c"


async def test_default_parse_mode_is_html(bot_env):
    with pytest.raises(TelegramContractViolation):
        await _send(bot_env, "<div>")
    bot_env.tg.violations.clear()


async def test_swallowed_violation_still_fails_the_test(bot_env):
    """Хендлеры ловят `except Exception`, поэтому нарушение обязано всплыть само."""
    async def careless(message):
        try:
            await message.answer("<div>")
        except Exception:
            pass

    bot_env.dp.message.register(careless, F.text == "careless")

    with pytest.raises(pytest.fail.Exception, match="div"):
        await bot_env.say(ADMIN, "careless")
    bot_env.tg.violations.clear()


async def test_handler_crash_fails_the_test(bot_env):
    async def crash(message):
        raise ValueError("бум")

    bot_env.dp.message.register(crash, F.text == "crash")

    with pytest.raises(pytest.fail.Exception, match="бум"):
        await bot_env.say(ADMIN, "crash")
    bot_env.handler_errors.clear()


async def test_expected_handler_crash_shows_the_error_handler_reply(bot_env):
    async def crash(message):
        raise ValueError("бум")

    bot_env.dp.message.register(crash, F.text == "crash")

    with bot_env.expect_handler_error():
        calls = await bot_env.say(ADMIN, "crash")
    assert len(calls) == 1 and calls[0].name == "SendMessage"


async def test_unknown_method_fails_loudly(bot_env):
    with pytest.raises(TelegramContractViolation, match="GetChat"):
        await bot_env.bot.get_chat(ADMIN)
    assert bot_env.tg.violations
    bot_env.tg.violations.clear()


async def test_identical_edit_is_not_modified(bot_env):
    sent = await _send(bot_env, "текст")
    edit = methods.EditMessageText(chat_id=ADMIN, message_id=sent.message_id, text="текст")

    with pytest.raises(TelegramBadRequest, match="not modified"):
        await bot_env.bot(edit)

    changed = await bot_env.bot.edit_message_text("новый", chat_id=ADMIN, message_id=sent.message_id)
    assert changed.message_id == sent.message_id
    assert changed.text == "новый"


async def test_edit_of_a_keyboard_counts_as_a_change_and_repeat_does_not(bot_env):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="a", callback_data="a")]])
    sent = await _send(bot_env, "текст")

    await bot_env.bot.edit_message_reply_markup(chat_id=ADMIN, message_id=sent.message_id, reply_markup=keyboard)
    with pytest.raises(TelegramBadRequest, match="not modified"):
        await bot_env.bot.edit_message_reply_markup(chat_id=ADMIN, message_id=sent.message_id, reply_markup=keyboard)


async def test_edit_and_delete_of_a_missing_message_fail_like_telegram(bot_env):
    with pytest.raises(TelegramBadRequest, match="to edit not found"):
        await bot_env.bot.edit_message_text("x", chat_id=ADMIN, message_id=1)
    with pytest.raises(TelegramBadRequest, match="to delete not found"):
        await bot_env.bot.delete_message(ADMIN, 1)

    sent = await _send(bot_env, "текст")
    assert await bot_env.bot.delete_message(ADMIN, sent.message_id) is True
    with pytest.raises(TelegramBadRequest, match="to delete not found"):
        await bot_env.bot.delete_message(ADMIN, sent.message_id)


async def test_bot_cannot_edit_a_users_message(bot_env):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="a", callback_data="a")]])
    seen = []

    async def edits_own_input(message):
        for edit in (
            message.edit_text("x"),
            message.edit_reply_markup(reply_markup=keyboard),
        ):
            try:
                await edit
            except TelegramBadRequest as error:
                seen.append(error.message)
        await message.answer("ok")

    bot_env.dp.message.register(edits_own_input, F.text == "edit me")
    await bot_env.say(ADMIN, "edit me")

    assert len(seen) == 2
    assert all("can't be edited" in message for message in seen)


async def test_reply_to_a_missing_message_fails_unless_allowed(bot_env):
    missing = ReplyParameters(message_id=1)
    with pytest.raises(TelegramBadRequest, match="to be replied not found"):
        await _send(bot_env, "x", reply_parameters=missing)
    with pytest.raises(TelegramBadRequest, match="to be replied not found"):
        await bot_env.bot.send_photo(ADMIN, "https://example.com/p.jpg", reply_parameters=missing)

    allowed = ReplyParameters(message_id=1, allow_sending_without_reply=True)
    assert (await _send(bot_env, "x", reply_parameters=allowed)).text == "x"
    known = await _send(bot_env, "старое")
    assert (await _send(bot_env, "x", reply_parameters=ReplyParameters(message_id=known.message_id))).text == "x"


async def test_tap_under_a_deleted_message_does_not_recreate_it(bot_env):
    errors = []

    async def probe(callback):
        await callback.answer()
        try:
            await callback.message.edit_text("новый")
        except TelegramBadRequest as error:
            errors.append(error.message)

    bot_env.dp.callback_query.register(probe, F.data == "probe")
    sent = await _send(bot_env, "текст")
    await bot_env.bot.delete_message(ADMIN, sent.message_id)

    await bot_env.tap(ADMIN, sent, "probe")

    assert len(errors) == 1 and "to edit not found" in errors[0]
    assert bot_env.tg.known_message(ADMIN, sent.message_id) is None


async def test_tap_without_a_message_gets_an_off_journal_one(bot_env):
    seen = []

    async def probe(callback):
        await callback.message.edit_text("новый")
        seen.append(callback.message.message_id)
        await callback.answer()

    bot_env.dp.callback_query.register(probe, F.data == "probe")

    await bot_env.tap(ADMIN, None, "probe")

    assert bot_env.tg.known_message(ADMIN, seen[0]).text == "новый"


async def test_photo_has_a_caption_and_cannot_be_edited_as_text(bot_env):
    sent = await bot_env.bot.send_photo(ADMIN, "https://example.com/poster.jpg", caption="<b>подпись</b>")
    assert sent.caption == "подпись"
    assert sent.photo

    with pytest.raises(TelegramBadRequest, match="no text in the message"):
        await bot_env.bot.edit_message_text("x", chat_id=ADMIN, message_id=sent.message_id)
    with pytest.raises(TelegramContractViolation):
        await bot_env.bot.send_photo(ADMIN, "https://example.com/poster.jpg", caption="x" * 1025)
    bot_env.tg.violations.clear()


async def test_find_button_lists_what_exists(bot_env):
    async def with_keyboard(message):
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Есть", callback_data="yes")]])
        await message.answer("текст", reply_markup=keyboard)

    bot_env.dp.message.register(with_keyboard, F.text == "kb")
    calls = await bot_env.say(ADMIN, "kb")

    assert find_button(calls, "Есть").button.callback_data == "yes"
    assert buttons(calls)[1][0].text == "Есть"
    with pytest.raises(pytest.fail.Exception, match="Есть.*yes"):
        find_button(calls, "Нет")


async def test_tap_shows_the_handler_the_current_message(bot_env):
    seen = []

    async def probe(callback):
        seen.append(callback.message.text)
        await callback.answer()

    bot_env.dp.callback_query.register(probe, F.data == "probe")
    sent = await _send(bot_env, "старый")
    await bot_env.bot.edit_message_text("новый", chat_id=ADMIN, message_id=sent.message_id)

    await bot_env.tap(ADMIN, sent, "probe")

    assert seen == ["новый"]


# ------------------------------------------------------------------- FakeBackend


async def test_backend_records_and_404s_unmatched_routes():
    backend = FakeBackend("radarr")
    await backend.start()
    try:
        async with httpx.AsyncClient(base_url=backend.url) as client:
            status = await client.get("/api/v3/system/status")
            missing = await client.post("/api/v3/nope?x=1&x=2&y=3", json={"a": 1})
    finally:
        await backend.stop()

    assert status.status_code == 200
    assert missing.status_code == 404
    assert [request.path for request in backend.unmatched] == ["/api/v3/nope"]
    recorded = backend.unmatched[0]
    assert recorded.method == "POST"
    assert recorded.query == {"x": ["1", "2"], "y": "3"}
    assert recorded.json == {"a": 1}


async def test_backend_modes_and_per_test_routes():
    backend = FakeBackend("radarr")
    await backend.start()
    try:
        async with httpx.AsyncClient(base_url=backend.url) as client:
            backend.route("GET", "/api/v3/qualityprofile", [{"id": 9}])
            assert (await client.get("/api/v3/qualityprofile")).json() == [{"id": 9}]

            backend.route("GET", "/api/v3/movie/{id}", responder=lambda request: {"path": request.path})
            assert (await client.get("/api/v3/movie/7")).json() == {"path": "/api/v3/movie/7"}
            assert len(backend.calls("GET", "/api/v3/movie/{id}")) == 1

            backend.fail_all(500)
            assert (await client.get("/api/v3/qualityprofile")).status_code == 500
            backend.recover()

            backend.hang_all()
            with pytest.raises(httpx.ReadTimeout):
                await client.get("/api/v3/qualityprofile", timeout=0.1)
    finally:
        await asyncio.wait_for(backend.stop(), timeout=5)


async def test_recover_answers_hanging_requests():
    backend = FakeBackend("radarr")
    await backend.start()
    try:
        async with httpx.AsyncClient(base_url=backend.url) as client:
            backend.hang_all()
            pending = asyncio.create_task(client.get("/api/v3/qualityprofile", timeout=5))
            while not backend.requests:
                await asyncio.sleep(0.01)
            assert not pending.done()

            backend.recover()

            response = await asyncio.wait_for(pending, timeout=2)
            assert response.status_code == 200

            # После recover режим можно включить снова.
            backend.hang_all()
            with pytest.raises(httpx.ReadTimeout):
                await client.get("/api/v3/qualityprofile", timeout=0.1)
    finally:
        await asyncio.wait_for(backend.stop(), timeout=5)


async def test_fixed_status_is_not_counted_as_unmatched():
    backend = FakeBackend("radarr")
    await backend.start()
    try:
        async with httpx.AsyncClient(base_url=backend.url) as client:
            backend.fail_all(500)
            await client.get("/api/v3/nope")
    finally:
        await backend.stop()

    assert backend.unmatched == []


async def test_unmatched_request_fails_teardown_check_unless_opted_out(bot_env):
    async with httpx.AsyncClient(base_url=bot_env.radarr.url) as client:
        await client.get("/api/v3/nope")

    with pytest.raises(pytest.fail.Exception, match="radarr.*GET /api/v3/nope"):
        bot_env.check_unmatched()

    bot_env.allow_unmatched = True
    bot_env.check_unmatched()


# ---------------------------------------------------------------------- Изоляция


async def test_all_settings_urls_point_at_localhost(bot_env):
    from bot.config import get_settings

    settings = get_settings()
    assert settings.radarr_url == bot_env.radarr.url
    assert settings.radarr_url.startswith("http://127.0.0.1:")
    assert settings.lidarr_url is None
    assert settings.deezer_enabled is False


async def test_settings_url_outside_localhost_is_refused(make_e2e_stack):
    with pytest.raises(AssertionError, match="lidarr_url"):
        await make_e2e_stack(env={"LIDARR_URL": "http://198.51.100.1", "LIDARR_API_KEY": "x"})


async def test_developer_environment_and_dotenv_do_not_leak(monkeypatch, make_e2e_stack, tmp_path):
    from bot.config import Settings, get_settings

    monkeypatch.setenv("LIDARR_URL", "http://198.51.100.2")
    monkeypatch.setenv("LIDARR_API_KEY", "exported")
    (tmp_path / ".env").write_text("SLSKD_URL=http://198.51.100.3\nSLSKD_API_KEY=leak\n", encoding="utf-8")

    await make_e2e_stack()

    assert Path.cwd().resolve() == tmp_path.resolve()
    assert Settings.model_config["env_file"] is None
    assert get_settings().lidarr_url is None
    assert get_settings().slskd_url is None


async def test_client_cannot_leave_localhost(bot_env, e2e_network_guard):
    async with httpx.AsyncClient() as client:
        with pytest.raises(AssertionError, match="запрещён"):
            await client.get("http://203.0.113.1/")
    assert e2e_network_guard == ["http://203.0.113.1/"]
    e2e_network_guard.clear()


def test_reset_clears_state_and_keeps_constants():
    from bot.clients import registry
    from bot.handlers import downloads, search
    from bot.handlers.search import grab, services
    from bot.handlers.torrserver import _tokens
    from bot.main import _chat_send_locks

    registry._radarr = object()
    downloads._views[1] = ("x", 0)
    services._grab_in_progress.add(1)
    _chat_send_locks[1] = asyncio.Lock()
    _tokens.issue(1)
    presets = set(grab._SEASON_PRESETS)

    reset_process_state()

    assert registry._radarr is None
    assert downloads._views == {}
    assert search._grab_in_progress == set()
    assert _chat_send_locks == {}
    assert _tokens.current(1) == ""
    assert presets and grab._SEASON_PRESETS == presets
    # Служебные атрибуты модуля под именами `__x__` живы: иначе функции, созданные
    # в его глобалах, падают на builtins.
    assert registry.__name__ == "bot.clients.registry"
    assert registry.__spec__ is not None
    assert registry.__builtins__ is not None


async def test_reset_restores_semaphore_capacity():
    from bot.services import search_service

    semaphore = search_service._DETECTION_SEMAPHORE
    capacity = semaphore._value
    await semaphore.acquire()
    assert semaphore._value == capacity - 1

    reset_process_state()

    assert semaphore._value == capacity


async def test_contended_module_lock_does_not_poison_the_next_loop():
    """Сброс не даёт замку остаться привязанным к циклу упавшего теста."""
    from bot.handlers.search import services

    lock = services._grab_guard_lock
    await lock.acquire()
    waiter = asyncio.create_task(lock.acquire())
    await asyncio.sleep(0)
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    assert lock._loop is not None

    reset_process_state()

    assert lock._loop is None
    assert not lock.locked()


# ----------------------------------------------------------------------- Проводка


async def test_build_dispatcher_wires_everything(bot_env):
    from bot.main import handle_unexpected_error

    dispatcher = bot_env.dp
    message_chain = [type(m) for m in dispatcher.message.middleware]
    callback_chain = [type(m) for m in dispatcher.callback_query.middleware]
    assert message_chain == [LoggingMiddleware, RateLimitMiddleware, AuthMiddleware]
    assert callback_chain == [LoggingMiddleware, RateLimitMiddleware, AuthMiddleware, StaleMessageMiddleware]
    assert dispatcher.message.middleware[1] is dispatcher.callback_query.middleware[1]
    assert any(handler.callback is handle_unexpected_error for handler in dispatcher.errors.handlers)
    assert dispatcher.sub_routers


async def test_notification_service_reaches_handlers_as_workflow_data(make_bot_env):
    service = object()
    env = await make_bot_env(notification_service=service)
    received = []

    async def probe(message, notification_service):
        received.append(notification_service)
        await message.answer("ok")

    env.dp.message.register(probe, F.text == "probe")
    await env.say(ADMIN, "probe")

    assert received == [service]
