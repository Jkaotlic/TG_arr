"""Фикстуры e2e. Главная трудность здесь — изоляция.

Реестр клиентов, `get_settings`, кэши хендлеров и замки живут на уровне модуля,
middleware читает настройки при создании, а рядом ~2000 тестов со своим окружением.
Поэтому состояние процесса сбрасывается и до, и после каждого e2e-теста, а окружение
собирается с нуля и указывает только на 127.0.0.1.

Использование::

    async def test_x(bot_env):
        stock_radarr_movie(bot_env.radarr)
        calls = await bot_env.say(bot_env.ADMIN, "/movie Дюна")
        calls = await bot_env.press(bot_env.ADMIN, calls, "2160p")

    async def test_music(make_bot_env):
        env = await make_bot_env(backends=("lidarr",))
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Iterator
from urllib.parse import urlsplit

import httpx
import pytest
import structlog
from aiogram import Bot, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from tests.e2e.fakes import BOT_TOKEN, BotEnv, FakeBackend, FakeTelegram

_HERE = Path(__file__).resolve().parent

LOCAL_HOST = "127.0.0.1"

#: Прокси не должны перехватить запросы к подделкам. NO_PROXY заодно отключает
#: чтение системного прокси из реестра Windows: `urllib` идёт в реестр, только если
#: в окружении нет ни одной proxy-переменной.
_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")

REQUIRED_BACKENDS = ("radarr", "sonarr", "prowlarr")

#: Переменные, включающие необязательный сервис. `{url}` заменяется адресом подделки.
_SERVICE_ENV: dict[str, dict[str, str]] = {
    "radarr": {"RADARR_URL": "{url}", "RADARR_API_KEY": "e2e-radarr-key"},
    "sonarr": {"SONARR_URL": "{url}", "SONARR_API_KEY": "e2e-sonarr-key"},
    "prowlarr": {"PROWLARR_URL": "{url}", "PROWLARR_API_KEY": "e2e-prowlarr-key"},
    "lidarr": {"LIDARR_URL": "{url}", "LIDARR_API_KEY": "e2e-lidarr-key"},
    "slskd": {"SLSKD_URL": "{url}", "SLSKD_API_KEY": "e2e-slskd-key"},
    "qbittorrent": {
        "QBITTORRENT_URL": "{url}",
        "QBITTORRENT_USERNAME": "e2e",
        "QBITTORRENT_PASSWORD": "e2e-qbit-password",
    },
    "emby": {"EMBY_URL": "{url}", "EMBY_API_KEY": "e2e-emby-key"},
    "navidrome": {
        "NAVIDROME_URL": "{url}",
        "NAVIDROME_USERNAME": "e2e",
        "NAVIDROME_PASSWORD": "e2e-navidrome-password",
    },
    "torrserver": {
        "TORRSERVER_URL": "{url}",
        "TORRSERVER_USERNAME": "e2e",
        "TORRSERVER_PASSWORD": "e2e-torrserver-password",
    },
    "emby_sync_hook": {"EMBY_SYNC_HOOK_URL": "{url}", "EMBY_SYNC_HOOK_TOKEN": "e2e-hook-token"},
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Всё в tests/e2e получает маркер `e2e` без `pytestmark` в каждом файле."""
    for item in items:
        if _HERE in item.path.parents:
            item.add_marker(pytest.mark.e2e)


def reset_process_state() -> None:
    """Обнулить всё процессное состояние бота.

    Правило обхода опирается на соглашение проекта: изменяемое состояние модуля
    называется `_lower_case`, а константы `_UPPER_CASE`. Поэтому новый кэш в
    хендлере сбрасывается сам, без правки этого списка.
    """
    from bot.clients import registry
    from bot.config import get_settings
    from bot.services import search_service
    from bot.ui.list_token import ListTokenRegistry

    get_settings.cache_clear()
    search_service._reset_module_state()

    for name in list(vars(registry)):
        if re.fullmatch(r"_[a-z_]+", name) and not name.endswith("_lock"):
            setattr(registry, name, None)

    for module_name, module in list(sys.modules.items()):
        if module is None or not (module_name == "bot" or module_name.startswith("bot.")):
            continue
        for name, value in list(vars(module).items()):
            if isinstance(value, (asyncio.Lock, asyncio.Semaphore)):
                _unbind_primitive(value)
            elif isinstance(value, ListTokenRegistry):
                value._tokens.clear()
            elif isinstance(value, Router):
                # Роутер модуля подключается к родителю один раз на процесс, а
                # `setup_routers()` зовут и e2e, и test_torrserver_handlers.py.
                value._parent_router = None
            elif re.fullmatch(r"_[a-z][a-z0-9_]*", name) and isinstance(value, (dict, set, list)):
                value.clear()


def _unbind_primitive(primitive: Any) -> None:
    """Замок модуля привязывается к циклу при первой конкуренции, а цикл у каждого
    теста свой. Без отвязки двойной тап в следующем тесте упадёт с RuntimeError
    «is bound to a different event loop»."""
    primitive._loop = None
    primitive._waiters = None
    if isinstance(primitive, asyncio.Lock):
        primitive._locked = False


@pytest.fixture
def e2e_network_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Клиенты бота физически не могут уйти дальше 127.0.0.1. Хендлер может
    проглотить исключение, поэтому попытки ещё и пишутся: тест падает на разборе."""
    blocked: list[str] = []
    original = httpx.AsyncHTTPTransport.handle_async_request

    async def guarded(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        if request.url.host != LOCAL_HOST:
            blocked.append(str(request.url))
            raise AssertionError(f"e2e: запрос наружу запрещён: {request.url}")
        return await original(self, request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", guarded)
    yield blocked
    assert not blocked, f"e2e: бот пытался уйти в сеть: {blocked}"


class E2EStack:
    """Поддельные бэкенды и окружение, указывающее на них."""

    def __init__(self, backends: dict[str, FakeBackend], db_path: Path) -> None:
        self.backends = backends
        self.db_path = db_path


def _configure_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    backends: dict[str, FakeBackend],
    overrides: dict[str, str],
) -> Path:
    """Собрать окружение с нуля. После этого настройки не зависят от машины."""
    from bot.config import Settings, get_settings

    # Экспортированный LIDARR_URL разработчика включил бы интеграцию и увёл бы её
    # на настоящий адрес, поэтому чистится всё, что знает Settings.
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    for variable in _PROXY_VARS:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("NO_PROXY", LOCAL_HOST)
    monkeypatch.setenv("no_proxy", LOCAL_HOST)

    # Два независимых заслона от .env из корня репо: файл не читается вовсе, и
    # рабочий каталог не корень репо.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.chdir(tmp_path)

    db_path = tmp_path / "bot.db"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("ADMIN_TG_IDS", str(BotEnv.ADMIN))
    monkeypatch.setenv("ALLOWED_TG_IDS", str(BotEnv.USER))
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    # Deezer включён по умолчанию и ходит на публичный API.
    monkeypatch.setenv("DEEZER_ENABLED", "false")
    # Без qBittorrent настройка только сыпет предупреждениями. Сценарий с
    # qBittorrent включит её через `env`.
    monkeypatch.setenv("NOTIFY_DOWNLOAD_COMPLETE", "false")
    for service, backend in backends.items():
        for variable, template in _SERVICE_ENV[service].items():
            monkeypatch.setenv(variable, template.format(url=backend.url))
    for variable, value in overrides.items():
        monkeypatch.setenv(variable, value)

    get_settings.cache_clear()
    assert_only_localhost(get_settings())
    return db_path


def assert_only_localhost(settings: Any) -> None:
    """Каждый URL в настройках обязан смотреть на 127.0.0.1."""
    for field in type(settings).model_fields:
        if field.endswith("_url"):
            value = getattr(settings, field)
            if value is not None:
                host = urlsplit(value).hostname
                assert host == LOCAL_HOST, f"e2e: {field} указывает на {value!r}"


@pytest.fixture
async def make_e2e_stack(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, e2e_network_guard: list[str]
) -> AsyncIterator[Callable[..., Awaitable[E2EStack]]]:
    """Поддельные бэкенды плюс окружение, без бота: для тестов самого `main()`."""
    from bot.clients.registry import close_all

    started: list[FakeBackend] = []

    # Красивый трейсбек rich стоит ~1,4 с на упавший хендлер. Логи остаются: pytest
    # покажет их у упавшего теста.
    saved_logging = structlog.get_config()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(colors=False, exception_formatter=structlog.dev.plain_traceback),
        ]
    )

    async def make(backends: tuple[str, ...] = (), env: dict[str, str] | None = None) -> E2EStack:
        if started:
            pytest.fail("Настройки и реестр клиентов на процесс: второй стек в одном тесте невозможен")
        reset_process_state()
        by_name: dict[str, FakeBackend] = {}
        for service in (*REQUIRED_BACKENDS, *backends):
            backend = FakeBackend(service)
            await backend.start()
            started.append(backend)
            by_name[service] = backend
        db_path = _configure_environment(monkeypatch, tmp_path, by_name, env or {})
        return E2EStack(by_name, db_path)

    yield make

    await close_all()
    for backend in started:
        await backend.stop()
    reset_process_state()
    structlog.configure(**saved_logging)


@pytest.fixture
async def make_bot_env(
    make_e2e_stack: Callable[..., Awaitable[E2EStack]],
) -> AsyncIterator[Callable[..., Awaitable[BotEnv]]]:
    """Фабрика для сценариев с необязательными сервисами и своими настройками:
    `await make_bot_env(backends=("lidarr",), env={"X": "y"}, notification_service=ns)`."""
    from bot.db import Database
    from bot.main import build_dispatcher

    created: list[BotEnv] = []

    async def make(
        backends: tuple[str, ...] = (),
        env: dict[str, str] | None = None,
        notification_service: Any = None,
    ) -> BotEnv:
        stack = await make_e2e_stack(backends, env)
        db = Database(str(stack.db_path))
        await db.connect()
        try:
            dispatcher = build_dispatcher(db, notification_service)
        except BaseException:
            # Поток aiosqlite не демон: незакрытая БД не даст процессу pytest выйти.
            await db.close()
            raise
        tg = FakeTelegram()
        bot = Bot(BOT_TOKEN, session=tg, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        bot_env_instance = BotEnv(dispatcher, bot, tg, db, stack.backends)
        created.append(bot_env_instance)
        return bot_env_instance

    yield make

    for instance in created:
        await instance.db.close()
        await instance.bot.session.close()
        instance.check_contract()


@pytest.fixture
async def bot_env(make_bot_env: Callable[..., Awaitable[BotEnv]]) -> BotEnv:
    """Бот с Radarr, Sonarr и Prowlarr. Для другого набора сервисов есть `make_bot_env`."""
    return await make_bot_env()
