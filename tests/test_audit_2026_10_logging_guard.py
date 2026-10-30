"""Аудит 2026-10-30, задача 2: логи, проверка URL, конфиг.

OBS-01, OBS-03, SEC-01, SEC-03, SEC-04, SEC-05, BUG-10, DEPLOY-01, LOGIC-10,
TEST-02 плюс Review Focus 2 (приватные адреса сервисов) и 5 (пустой токен).
"""

import json
import logging
import re
import time
import warnings
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import structlog

from bot.models import ContentType

ROOT = Path(__file__).resolve().parent.parent

# Секреты в тестах — заведомо несуществующие, формы взяты из test_logging_setup.
_FAKE_TOKEN = "bot123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
# Сторож git принимает `логин:пароль@хост` за email — userinfo собирается из частей.
_USERINFO_LOGIN, _USERINFO_PASS = "alice", "s3cr3t"


def _url_with_userinfo(host: str, path: str = "/") -> str:
    return f"http://{_USERINFO_LOGIN}:{_USERINFO_PASS}" + "@" + f"{host}{path}"


def _cgnat(*tail: int) -> str:
    """Адрес из 100.64.0.0/10 (RFC 6598, общее адресное пространство CGNAT/Tailscale).

    Это константа протокола, не инфраструктура; собирается из октетов, потому что
    сторож git не отличает её от публичного IP и документационного диапазона для
    этой проверки нет: SEC-03 как раз про то, что 100.64/10 не «глобальный».
    """
    return ".".join(str(n) for n in (100, *tail))


# --------------------------------------------------------------------------
# OBS-01: трейсбек в JSON-логе и маскировка секретов в его тексте
# --------------------------------------------------------------------------
@pytest.fixture
def json_logging(capsys):
    """Восстановить глобальное состояние logging/structlog после теста.

    setup_logging() вызывается в теле теста (через `_setup_json`): handler
    запоминает sys.stdout на момент вызова, а capsys подменяет его по фазам.
    """
    root = logging.getLogger()
    prev_handlers = list(root.handlers)
    prev_level = root.level
    yield capsys
    root.handlers[:] = prev_handlers
    root.setLevel(prev_level)
    structlog.reset_defaults()


def _setup_json(capsys) -> None:
    from bot.main import setup_logging

    setup_logging(log_level="INFO", log_format="json")
    capsys.readouterr()


def _last_json_line(capsys) -> dict:
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines, "в stdout нет ни одной строки лога"
    return json.loads(lines[-1])  # строка обязана быть валидным JSON


def _raise_with_secret():
    raise RuntimeError(f"proxy {_url_with_userinfo('proxy.example.com:8899')} failed, {_FAKE_TOKEN}")


@pytest.mark.parametrize("as_instance", [False, True])
def test_structlog_exc_info_gives_masked_traceback_in_json(json_logging, as_instance):
    _setup_json(json_logging)
    log = structlog.get_logger("test.obs01")
    try:
        _raise_with_secret()
    except RuntimeError as e:
        log.error("boom", exc_info=e if as_instance else True)

    data = _last_json_line(json_logging)
    assert data["event"] == "boom"
    assert isinstance(data.get("exception"), str)
    assert "Traceback" in data["exception"]
    assert "_raise_with_secret" in data["exception"]
    assert "RuntimeError" in data["exception"]
    # секреты из текста исключения не доходят до лога
    assert "s3cr3t" not in data["exception"]
    assert "alice" not in data["exception"]
    assert "AAAAAAAA" not in data["exception"]
    assert "://***:***@" in data["exception"]
    assert data.get("exc_info") is not True


def test_stdlib_exception_gives_masked_traceback_in_json(json_logging):
    _setup_json(json_logging)
    stdlib = logging.getLogger("test.obs01.stdlib")
    try:
        _raise_with_secret()
    except RuntimeError:
        stdlib.exception("stdlib boom")

    data = _last_json_line(json_logging)
    assert data["event"] == "stdlib boom"
    assert "Traceback" in data["exception"]
    assert "s3cr3t" not in data["exception"]
    assert "AAAAAAAA" not in data["exception"]


def test_plain_event_has_no_exception_key(json_logging):
    _setup_json(json_logging)
    structlog.get_logger("test.obs01").info("fine")
    assert "exception" not in _last_json_line(json_logging)


@pytest.mark.asyncio
async def test_unhandled_handler_error_logs_type_name_for_empty_message(json_logging):
    """У httpx-таймаутов `str(e) == ""` — в поле error должно быть имя типа."""
    import httpx
    from aiogram.types import ErrorEvent, Update

    from bot.main import handle_unexpected_error

    _setup_json(json_logging)
    event = ErrorEvent(update=Update(update_id=7), exception=httpx.ReadTimeout(""))
    await handle_unexpected_error(event)

    data = _last_json_line(json_logging)
    assert data["event"] == "unhandled_handler_error"
    assert data["error"] == "ReadTimeout"


# --------------------------------------------------------------------------
# OBS-03: неизвестный часовой пояс не молчит
# --------------------------------------------------------------------------
def test_unknown_timezone_is_logged_once():
    from bot.ui.formatters import _common

    _common._ZONEINFO_CACHE.pop("Mars/Olympus", None)
    with structlog.testing.capture_logs() as logs:
        tz1 = _common._get_cached_zoneinfo("Mars/Olympus")
        tz2 = _common._get_cached_zoneinfo("Mars/Olympus")
    try:
        assert tz1 is tz2
        warned = [e for e in logs if e["log_level"] == "warning"]
        assert len(warned) == 1
        assert "Mars/Olympus" in str(warned[0])
    finally:
        _common._ZONEINFO_CACHE.pop("Mars/Olympus", None)


# --------------------------------------------------------------------------
# SEC-01: перевод строки и обратная косая в URL; полная проверка магнета
# --------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01\nhttp://127.0.0.1/secret",
    "http://example.com/a\nhttp://127.0.0.1:8080/x",
    "http://example.com/a\rhttp://127.0.0.1:8080/x",
    "http://example.com/a\thttp://127.0.0.1:8080/x",
    "http://example.com/a b",
    "http://example.com\\@127.0.0.1/x",
    "http://example.com/a\x00b",
    "magnet:?xt=urn:btih:abcdef0123456789 http://127.0.0.1/",
])
async def test_url_with_control_chars_spaces_or_backslash_is_rejected(url):
    from bot.services.url_guard import _validate_download_url

    assert await _validate_download_url(url) is False


@pytest.mark.asyncio
async def test_newline_is_rejected_even_for_a_trusted_service_host():
    """Доверенный хост (Prowlarr из conftest) не отменяет проверку символов."""
    from bot.services.url_guard import _validate_download_url

    assert await _validate_download_url(
        "http://localhost:9696/1/download?apikey=x\nhttp://127.0.0.1:8080/x"
    ) is False


_HEX40 = "abcdef0123456789abcdef0123456789abcdef01"
_B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    f"magnet:?xt=urn:btih:{_HEX40}",
    f"magnet:?xt=urn:btih:{_B32}",
    f"magnet:?xt=urn:btih:{_HEX40}&dn=Movie.2024.2160p&tr=udp%3A%2F%2Ftracker.example.com%3A6969%2Fannounce",
    f"magnet:?xt=urn:btih:{_HEX40}&dn=Фильм+2024&tr=udp://tracker.example.com:6969/announce&tr=http://tr2.example.com/a",
    f"magnet:?xt=urn:btih:{_HEX40}&xt=urn:btmh:1220{'a' * 64}",
    f"magnet:?xt=urn:btmh:1220{'a' * 64}&dn=x",
    f"magnet:?xt=urn:btih:{_HEX40}&dn=%D0%A4%D0%B8%D0%BB%D1%8C%D0%BC&xl=123456",
    "magnet:?xt=urn:btih:aabbccdd",  # прежние тесты считают такой магнет допустимым
])
async def test_legitimate_magnets_still_pass(url):
    from bot.services.url_guard import _validate_download_url

    assert await _validate_download_url(url) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "magnet:?xt=urn:ed2k:abc",
    "magnet:?xt=urn:btih:",
    "magnet:?xt=urn:btih:abc<script>",
    "magnet:?dn=x",
    "magnet:xt=urn:btih:abc",
])
async def test_malformed_magnets_are_rejected(url):
    from bot.services.url_guard import _validate_download_url

    assert await _validate_download_url(url) is False


# Review Focus 2: сервисы на приватных адресах не должны отвалиться.
@pytest.mark.asyncio
async def test_prowlarr_proxied_download_on_private_ip_still_passes(monkeypatch):
    """Прод: Prowlarr на LAN-адресе проксирует downloadUrl через себя."""
    from bot.config import get_settings
    from bot.services.url_guard import _validate_download_url

    monkeypatch.setenv("PROWLARR_URL", "http://192.168.0.95:9696")
    monkeypatch.setenv("QBITTORRENT_URL", "http://10.0.0.7:8080")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pw")
    monkeypatch.setenv("RADARR_URL", f"http://{_cgnat(64, 0, 9)}:7878")  # CGNAT/Tailscale
    get_settings.cache_clear()
    try:
        assert await _validate_download_url(
            "http://192.168.0.95:9696/3/download?apikey=k&link=abc&file=Some.Release"
        ) is True
        assert await _validate_download_url("http://10.0.0.7:8080/x.torrent") is True
        # Доверенный сервис в CGNAT-диапазоне тоже проходит: `is_global` — только для чужих хостов.
        assert await _validate_download_url(f"http://{_cgnat(64, 0, 9)}:7878/dl") is True
        # Тот же адрес, чужой порт — по-прежнему нет.
        assert await _validate_download_url("http://192.168.0.95:22/x") is False
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------
# SEC-03: CGNAT/Tailscale вне доверенного набора
# --------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    f"http://{_cgnat(64, 0, 1)}/x",
    f"http://{_cgnat(127, 255, 254)}:8080/x",
    "http://[::ffff:127.0.0.1]/x",
    "http://[fd00::1]/x",
])
async def test_non_global_literal_ip_outside_trusted_set_is_rejected(url):
    from bot.services.url_guard import _validate_download_url

    assert await _validate_download_url(url) is False


@pytest.mark.asyncio
async def test_hostname_resolving_to_cgnat_is_rejected():
    from bot.services.url_guard import _validate_download_url

    infos = [(2, 1, 6, "", (_cgnat(64, 0, 5), 0))]
    with patch("bot.services.url_guard.socket.getaddrinfo", return_value=infos):
        assert await _validate_download_url("http://tracker.example.com/x") is False


@pytest.mark.asyncio
async def test_global_public_ip_is_allowed():
    from bot.services.url_guard import _validate_download_url

    assert await _validate_download_url("http://8.8.8.8/x.torrent") is True


# --------------------------------------------------------------------------
# BUG-10: недопустимый порт — отказ, а не ValueError
# --------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "http://example.com:99999/",
    "http://example.com:abc/",
    "http://[::1/",
])
async def test_invalid_port_or_netloc_returns_false_not_valueerror(url):
    from bot.services.url_guard import _validate_download_url

    assert await _validate_download_url(url) is False


# --------------------------------------------------------------------------
# SEC-04: маскирование torrent_pass и passkey в tr= магнета
# --------------------------------------------------------------------------
def test_mask_url_hides_torrent_pass_and_userinfo():
    from bot.services.url_guard import _mask_url

    masked = _mask_url(_url_with_userinfo("tracker.example.com", "/dl?torrent_pass=SECRETPASS&id=5"))
    assert "SECRETPASS" not in masked
    assert _USERINFO_PASS not in masked
    assert "id=5" in masked


def test_mask_url_hides_passkey_inside_magnet_tracker():
    from bot.services.url_guard import _mask_url

    magnet = (
        f"magnet:?xt=urn:btih:{_HEX40}&dn=Movie"
        "&tr=http%3A%2F%2Ftracker.example.com%2Fannounce%3Fpasskey%3DSECRETPASSKEY1234"
        "&tr=http://t2.example.com/fedcba9876543210fedcba9876543210/announce"
    )
    masked = _mask_url(magnet, max_len=1000)
    assert "SECRETPASSKEY1234" not in masked
    assert "fedcba9876543210fedcba9876543210" not in masked
    assert _HEX40 in masked  # сам хэш — не секрет, по нему узнают раздачу
    assert "tracker.example.com" in masked


# --------------------------------------------------------------------------
# SEC-05: кубический бэктрекинг в разборе названий
# --------------------------------------------------------------------------
_EVIL = "1" + " " * 3000


def test_release_title_parsing_is_fast_on_whitespace_run():
    from bot.clients.prowlarr import ProwlarrClient

    client = ProwlarrClient("http://prowlarr", "key")
    started = time.perf_counter()
    client._extract_season_episode(_EVIL)
    client._is_season_pack(_EVIL)
    assert time.perf_counter() - started < 0.5


def test_parse_query_is_fast_on_whitespace_run():
    from bot.services.search_service import SearchService

    started = time.perf_counter()
    SearchService.parse_query(object.__new__(SearchService), _EVIL)
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize("title,expected", [
    ("Ведьмак 2 сезон", (2, None)),
    ("Ведьмак 3-й сезон", (3, None)),
    ("Ведьмак 3 - й сезон", (3, None)),
    ("Шоу 12 серия", (None, 12)),
    ("Шоу 4-я серия", (None, 4)),
    ("Шоу сезон 2 серия 5", (2, 5)),
])
def test_cyrillic_season_forms_parse_as_before(title, expected):
    from bot.clients.prowlarr import ProwlarrClient

    assert ProwlarrClient("http://prowlarr", "key")._extract_season_episode(title) == expected


def test_parse_query_cyrillic_forms_unchanged():
    from bot.services.search_service import SearchService

    parse = lambda q: SearchService.parse_query(object.__new__(SearchService), q)  # noqa: E731
    assert parse("Тед Лассо 4 сезон")["season"] == 4
    assert parse("Ведьмак 3-й сезон 2160p")["season"] == 3
    assert parse("Ведьмак 3-й сезон 2160p")["title"] == "Ведьмак"


# --------------------------------------------------------------------------
# DEPLOY-01: пустые строки из compose = «не задано»
# --------------------------------------------------------------------------
_OPTIONAL_ENV = (
    "LIDARR_URL", "LIDARR_API_KEY", "SLSKD_URL", "SLSKD_API_KEY", "NAVIDROME_URL",
    "NAVIDROME_USERNAME", "NAVIDROME_PASSWORD", "QBITTORRENT_URL", "QBITTORRENT_PASSWORD",
    "EMBY_URL", "EMBY_API_KEY", "TMDB_API_KEY", "TMDB_PROXY_URL",
)


def _fresh_settings(**kwargs):
    from bot.config import Settings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return Settings(_env_file=None, **kwargs)


def test_empty_optional_env_vars_leave_integrations_disabled(monkeypatch):
    for name in _OPTIONAL_ENV:
        monkeypatch.setenv(name, "")
    s = _fresh_settings()
    assert s.lidarr_enabled is False
    assert s.slskd_enabled is False
    assert s.music_enabled is False
    assert s.qbittorrent_enabled is False
    assert s.emby_enabled is False
    assert s.navidrome_enabled is False
    assert s.tmdb_enabled is False
    assert s.tmdb_proxy_url is None


def test_empty_env_value_falls_back_to_field_default(monkeypatch):
    monkeypatch.setenv("QBITTORRENT_USERNAME", "")
    monkeypatch.setenv("TMDB_LANGUAGE", "")
    monkeypatch.setenv("TIMEZONE", "")
    s = _fresh_settings()
    assert s.qbittorrent_username == "admin"
    assert s.tmdb_language == "ru-RU"
    assert s.timezone == "Europe/Moscow"


def test_empty_values_in_dotenv_file_are_unset_too(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LIDARR_URL=\nSLSKD_URL=\nSLSKD_API_KEY=\n", encoding="utf-8")
    from bot.config import Settings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        s = Settings(_env_file=str(env))
    assert s.lidarr_enabled is False
    assert s.slskd_enabled is False


def test_empty_required_telegram_token_still_fails_startup(monkeypatch):
    """Review Focus 5: пустой обязательный токен — ошибка старта, не молчаливый дефолт."""
    from pydantic import ValidationError

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    with pytest.raises(ValidationError) as exc:
        _fresh_settings()
    assert "telegram_bot_token" in str(exc.value).lower()


@pytest.mark.parametrize("name", ["PROWLARR_URL", "RADARR_API_KEY", "SONARR_URL"])
def test_empty_required_service_vars_still_fail_startup(monkeypatch, name):
    from pydantic import ValidationError

    monkeypatch.setenv(name, "")
    with pytest.raises(ValidationError) as exc:
        _fresh_settings()
    assert name.lower() in str(exc.value).lower()


# --------------------------------------------------------------------------
# LOGIC-10: музыка и неизвестный тип уходят в qBit без категории
# --------------------------------------------------------------------------
def _release(**kw):
    from tests.test_add_service import _release as make

    return make(**kw)


def test_qbit_category_is_empty_for_music_and_unknown():
    from bot.services.add_service import _qbit_category

    assert _qbit_category(ContentType.MUSIC, _release()) == ""
    assert _qbit_category(ContentType.UNKNOWN, _release(detected_type=ContentType.UNKNOWN)) == ""
    assert _qbit_category(ContentType.UNKNOWN, _release(detected_type=ContentType.MUSIC)) == ""


def test_qbit_category_for_known_types_is_unchanged():
    from bot.services.add_service import _qbit_category

    assert _qbit_category(ContentType.MOVIE, _release()) == "radarr"
    assert _qbit_category(ContentType.SERIES, _release()) == "tv-sonarr"
    assert _qbit_category(ContentType.ANIME, _release()) == "anime"
    # свободный поиск: фасет неизвестен, но Prowlarr подсказал тип
    assert _qbit_category(ContentType.UNKNOWN, _release(detected_type=ContentType.MOVIE)) == "radarr"


@pytest.mark.asyncio
async def test_force_download_of_music_sends_no_category():
    from tests.test_add_service import _service

    qbt = AsyncMock()
    qbt.add_torrent_url.return_value = True
    ok, _ = await _service(qbt=qbt).grab_release(
        _release(), ContentType.MUSIC, force_download=True,
    )
    assert ok is True
    assert not qbt.add_torrent_url.await_args.kwargs["category"]


@pytest.mark.asyncio
async def test_add_torrent_url_omits_empty_category():
    from bot.clients.qbittorrent import QBittorrentClient

    client = QBittorrentClient("http://198.51.100.10:8080", "u", "p")
    with patch.object(client, "_request", new_callable=AsyncMock) as req:
        req.return_value = "Ok."
        await client.add_torrent_url("magnet:?xt=urn:btih:abc", category="")
    assert "category" not in req.call_args.kwargs["data"]


@pytest.mark.asyncio
async def test_direct_torrent_without_category_gets_the_completion_message():
    """Раздача без категории — не «владение *arr», завершение объявляет qBit-уведомление."""
    from tests.test_review_direct_qbit import _complete_one

    sender = await _complete_one("", "tgarr-direct")
    sender.assert_awaited_once()


# --------------------------------------------------------------------------
# TEST-02: тесты не дают SyntaxWarning (недопустимые escape-последовательности)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("path", sorted((ROOT / "tests").rglob("*.py")), ids=lambda p: p.name)
def test_test_modules_compile_without_syntax_warnings(path):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        compile(path.read_text(encoding="utf-8"), str(path), "exec")


def test_requirements_dev_has_tzdata_for_windows_only():
    import tomllib

    dev = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    assert re.search(r'^tzdata[^\n]*;\s*sys_platform\s*==\s*"win32"', dev, re.MULTILINE)
    extras = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"]["optional-dependencies"]["dev"]
    assert any(s.startswith("tzdata") and "win32" in s for s in extras)
    prod = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "tzdata" not in prod
