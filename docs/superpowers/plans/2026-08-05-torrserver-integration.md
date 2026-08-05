# TorrServer в TG_arr — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить в бота раздел TorrServer — поиск раздач, добавление одной кнопкой, немедленную публикацию `.strm` в Emby через хук на Homeserver, список раздач с удалением и карточку статуса.

**Architecture:** Клиент `TorrServerClient` наследует `BaseAPIClient` (ретраи, пул, TTL-кеш) и переопределяет заголовки на Basic-авторизацию. Сценарий «добавить и опубликовать» живёт в `TorrServerService`: добавить → дождаться `file_stats` → дёрнуть HTTP-хук на Homeserver, который запускает `Sync-TorrServerToEmby.py --apply`. Хендлеры — отдельный роутер, включённый **до** `search_router`, потому что `handle_text_search` перехватывает любой текст.

**Tech Stack:** Python 3.12, aiogram 3.x, httpx, pydantic v2 + pydantic-settings, structlog, tenacity, pytest + `unittest.mock`.

**Спека:** `docs/superpowers/specs/2026-08-05-torrserver-integration-design.md` — все контракты API там сняты с живого сервера 2026-08-05.

## Global Constraints

- Слой 1 (код в `bot/`) — **TDD обязателен**: падающий тест → минимальный код → зелено. Исключение: Task 13 (скрипты на Homeserver) — слой 3, вместо тестов идемпотентность и проверка постусловия.
- Готово = `make test` зелёный **и** `make lint` чистый, с показанным выводом. `make typecheck` в гейт не входит (DEP-03).
- Тестам нужны env-переменные конфига: `TELEGRAM_BOT_TOKEN=test ALLOWED_TG_IDS=1 SCRYER_URL=http://x SCRYER_USERNAME=x SCRYER_PASSWORD=x python -m pytest tests/ -q --tb=short`.
- Мокинг HTTP — только `unittest.mock.AsyncMock`/`MagicMock`/`patch`. `respx` в проект не добавляется.
- Диапазоны зависимостей не расширять (комментарии `DEP-xx` в `pyproject.toml`). Новых зависимостей эта работа не требует.
- Весь пользовательский текст — по-русски, HTML parse_mode, пользовательские данные через `html.escape`.
- Базовый URL TorrServer: `http://192.168.31.95:8090`. Хук: `http://192.168.31.95:8099`.
- Ссылка на поток строится ровно так: `{base}/stream/{quote(имя файла)}?link={hash}&index={file_id}&play`.
- Деструктивные действия (удаление раздачи) — только для `ADMIN_TG_IDS`.

## File Structure

**Создаётся:**

| Файл | Ответственность |
| --- | --- |
| `bot/clients/torrserver.py` | HTTP-клиент TorrServer: поиск, список, get, add, rem, настройки, health |
| `bot/clients/emby_sync_hook.py` | Клиент хука синка: один метод `trigger_sync()`, никогда не бросает |
| `bot/services/torrserver_service.py` | Сценарий add → ждать метаданные → синк → собрать ответ |
| `bot/handlers/torrserver.py` | Роутер: панель, поиск, карточка, добавление, список, удаление |
| `bot/ui/keyboards/torrserver.py` | Миксин `_TorrServerKeyboards` |
| `bot/ui/formatters/torrserver.py` | Миксин `_TorrServerFormatters` |
| `tests/test_torrserver_models.py` | Чистые функции: парсинг размера, санитизация названия |
| `tests/test_torrserver_client.py` | Клиент: разбор ответов, ошибки, контракт запроса |
| `tests/test_torrserver_service.py` | Сценарий добавления и деградация при отказе хука |
| `tests/test_torrserver_handlers.py` | Хендлеры: панель, ForceReply-маршрутизация, права |
| `C:\Tools\TorrServer\Emby-Sync-Hook.py` | Служба-хук на Homeserver (слой 3) |
| `C:\Tools\TorrServer\Install-EmbySyncHook.ps1` | Идемпотентный установщик службы (слой 3) |

**Модифицируется:** `bot/config.py`, `bot/models.py`, `bot/clients/registry.py`, `bot/ui/menu.py`, `bot/ui/keyboards/menu.py`, `bot/ui/keyboards/__init__.py`, `bot/ui/keyboards/_constants.py`, `bot/ui/formatters/__init__.py`, `bot/ui/callbacks.py`, `bot/handlers/__init__.py`, `bot/handlers/status.py`, `bot/ui/commands.py`, `.env.example`, `docker-compose.yml`.

---

### Task 1: Конфигурация TorrServer и хука

**Files:**
- Modify: `bot/config.py`
- Modify: `.env.example`
- Modify: `docker-compose.yml`
- Test: `tests/test_torrserver_config.py`

**Interfaces:**
- Consumes: ничего.
- Produces: `Settings.torrserver_url/torrserver_username/torrserver_password/torrserver_timeout/torrserver_search_timeout/torrserver_metadata_timeout/emby_sync_hook_url/emby_sync_hook_token/emby_sync_hook_timeout`, свойства `Settings.torrserver_enabled -> bool`, `Settings.emby_sync_hook_enabled -> bool`.

- [ ] **Step 1: Написать падающий тест**

```python
"""Конфигурация раздела TorrServer: свойства-переключатели и предупреждения
о полу-настроенной интеграции (LOGIC-09-стиль, как у Lidarr/Emby/qBittorrent)."""

import warnings

import pytest


def _settings(**overrides):
    from bot.config import Settings

    return Settings(**overrides)


def test_torrserver_disabled_by_default():
    s = _settings(notify_download_complete=False)
    assert s.torrserver_enabled is False


def test_torrserver_fully_configured_enables():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        s = _settings(
            torrserver_url="http://192.168.31.95:8090",
            torrserver_username="admin",
            torrserver_password="pw",
            emby_sync_hook_url="http://192.168.31.95:8099",
            emby_sync_hook_token="tok",
            notify_download_complete=False,
        )
    assert s.torrserver_enabled is True
    assert s.emby_sync_hook_enabled is True


def test_torrserver_without_password_warns():
    with pytest.warns(UserWarning, match="TorrServer"):
        s = _settings(torrserver_url="http://ts:8090", torrserver_username="admin",
                      notify_download_complete=False)
    assert s.torrserver_enabled is False


def test_torrserver_without_hook_warns_about_delayed_emby():
    with pytest.warns(UserWarning, match="синхронизац"):
        _settings(
            torrserver_url="http://ts:8090", torrserver_username="admin",
            torrserver_password="pw", notify_download_complete=False,
        )


def test_torrserver_url_trailing_slash_stripped():
    s = _settings(
        torrserver_url="http://ts:8090/", torrserver_username="admin",
        torrserver_password="pw", emby_sync_hook_url="http://ts:8099/",
        emby_sync_hook_token="tok", notify_download_complete=False,
    )
    assert s.torrserver_url == "http://ts:8090"
    assert s.emby_sync_hook_url == "http://ts:8099"
```

- [ ] **Step 2: Запустить тест и убедиться, что падает**

Run: `TELEGRAM_BOT_TOKEN=test ALLOWED_TG_IDS=1 SCRYER_URL=http://x SCRYER_USERNAME=x SCRYER_PASSWORD=x python -m pytest tests/test_torrserver_config.py -q`
Expected: FAIL — `ValidationError`/`AttributeError`, полей `torrserver_*` не существует.

- [ ] **Step 3: Добавить поля и свойства в `bot/config.py`**

Поля — сразу после блока Emby:

```python
    # TorrServer (optional) — streaming contour: "watch now" instead of
    # "have it in the library". Separate from Scryer on purpose.
    torrserver_url: Optional[str] = Field(default=None, description="TorrServer base URL")
    torrserver_username: Optional[str] = Field(default=None, description="TorrServer basic-auth user")
    torrserver_password: Optional[str] = Field(default=None, description="TorrServer basic-auth password")
    torrserver_timeout: float = Field(default=30.0, ge=5.0, description="TorrServer request timeout in seconds")
    # A torznab search fans out to every configured Prowlarr indexer, so it is
    # far slower than the plain API calls — same split as scryer_search_timeout.
    torrserver_search_timeout: float = Field(
        default=60.0, ge=10.0, le=300.0, description="TorrServer torznab search timeout (seconds)"
    )
    # How long to wait for a freshly added torrent to report its files. Without
    # them the Emby sync would publish an empty release.
    torrserver_metadata_timeout: float = Field(
        default=30.0, ge=5.0, le=180.0, description="How long to wait for torrent metadata (seconds)"
    )

    # Emby sync hook on Homeserver — the bot's container has neither ssh nor
    # curl, so the forced `.strm` sync is reached over HTTP.
    emby_sync_hook_url: Optional[str] = Field(default=None, description="Emby sync hook base URL")
    emby_sync_hook_token: Optional[str] = Field(default=None, description="Emby sync hook token")
    emby_sync_hook_timeout: float = Field(
        default=90.0, ge=5.0, le=600.0, description="Emby sync hook timeout (seconds)"
    )
```

В валидатор `strip_trailing_slash_optional` добавить два имени:

```python
    @field_validator(
        "qbittorrent_url", "emby_url", "lidarr_url", "slskd_url", "navidrome_url",
        "torrserver_url", "emby_sync_hook_url", mode="after",
    )
```

Свойства — рядом с `emby_enabled`:

```python
    @property
    def torrserver_enabled(self) -> bool:
        """Check if TorrServer integration is configured."""
        return (
            self.torrserver_url is not None
            and self.torrserver_username is not None
            and self.torrserver_password is not None
        )

    @property
    def emby_sync_hook_enabled(self) -> bool:
        """Check if the forced Emby sync hook is configured."""
        return self.emby_sync_hook_url is not None and self.emby_sync_hook_token is not None
```

В `_warn_on_inconsistent_integration_config` — два блока перед `return self`:

```python
        ts_parts = (self.torrserver_url, self.torrserver_username, self.torrserver_password)
        if any(p is not None for p in ts_parts) and not self.torrserver_enabled:
            warnings.warn(
                "TorrServer partially configured: TORRSERVER_URL, TORRSERVER_USERNAME "
                "and TORRSERVER_PASSWORD are all required — the TorrServer section "
                "will stay disabled.",
                stacklevel=2,
            )
        if (self.emby_sync_hook_url is None) != (self.emby_sync_hook_token is None):
            warnings.warn(
                "Emby sync hook partially configured: both EMBY_SYNC_HOOK_URL and "
                "EMBY_SYNC_HOOK_TOKEN are required.",
                stacklevel=2,
            )
        if self.torrserver_enabled and not self.emby_sync_hook_enabled:
            warnings.warn(
                "TorrServer настроен без хука синхронизации: раздачи будут "
                "добавляться, но в Emby попадут только штатной задачей раз в "
                "10 минут.",
                stacklevel=2,
            )
```

- [ ] **Step 4: Запустить тест и убедиться, что проходит**

Run: `TELEGRAM_BOT_TOKEN=test ALLOWED_TG_IDS=1 SCRYER_URL=http://x SCRYER_USERNAME=x SCRYER_PASSWORD=x python -m pytest tests/test_torrserver_config.py -q`
Expected: PASS, 5 тестов.

- [ ] **Step 5: Дописать `.env.example` и `docker-compose.yml`**

В `.env.example` перед блоком «Optional Settings»:

```bash
# TorrServer (streaming contour — "watch now")
# TORRSERVER_URL=http://192.168.31.95:8090
# TORRSERVER_USERNAME=admin
# TORRSERVER_PASSWORD=
# TORRSERVER_TIMEOUT=30.0
# TORRSERVER_SEARCH_TIMEOUT=60.0
# TORRSERVER_METADATA_TIMEOUT=30.0

# Emby sync hook on Homeserver (forced .strm sync right after adding a torrent)
# EMBY_SYNC_HOOK_URL=http://192.168.31.95:8099
# EMBY_SYNC_HOOK_TOKEN=
# EMBY_SYNC_HOOK_TIMEOUT=90.0
```

В `docker-compose.yml` в блок `environment:` после строк `EMBY_*`:

```yaml
      TORRSERVER_URL: ${TORRSERVER_URL:-}
      TORRSERVER_USERNAME: ${TORRSERVER_USERNAME:-admin}
      TORRSERVER_PASSWORD: ${TORRSERVER_PASSWORD:-}
      TORRSERVER_TIMEOUT: ${TORRSERVER_TIMEOUT:-30.0}
      TORRSERVER_SEARCH_TIMEOUT: ${TORRSERVER_SEARCH_TIMEOUT:-60.0}
      TORRSERVER_METADATA_TIMEOUT: ${TORRSERVER_METADATA_TIMEOUT:-30.0}
      EMBY_SYNC_HOOK_URL: ${EMBY_SYNC_HOOK_URL:-}
      EMBY_SYNC_HOOK_TOKEN: ${EMBY_SYNC_HOOK_TOKEN:-}
      EMBY_SYNC_HOOK_TIMEOUT: ${EMBY_SYNC_HOOK_TIMEOUT:-90.0}
```

- [ ] **Step 6: Прогнать весь набор тестов и линт**

Run: `make test` и `make lint`
Expected: тесты зелёные (существующие тесты конфига не сломаны), ruff чист.

- [ ] **Step 7: Коммит**

```bash
git add bot/config.py tests/test_torrserver_config.py .env.example docker-compose.yml
git commit -m "feat: configuration for the TorrServer section and its Emby sync hook"
```

---

### Task 2: Модели и чистые функции разбора

**Files:**
- Modify: `bot/models.py`
- Test: `tests/test_torrserver_models.py`

**Interfaces:**
- Consumes: ничего.
- Produces: `parse_torrserver_size(value: str | int | None) -> int`, `sanitize_torrent_title(title: str) -> str`, модели `TorrServerFile(id: int, path: str, length: int)`, `TorrServerRelease(title: str, size: int, seeders: int, peers: int, link: str, tracker: str, year: int | None)`, `TorrServerTorrent(hash: str, title: str, category: str, poster: str, size: int, stat: int, stat_string: str, files: list[TorrServerFile])`, `TorrServerStats(version: str, torrent_count: int, total_size: int, cache_size: int, use_disk: bool, source_count: int)`, `TorrServerTorrent.video_files -> list[TorrServerFile]`.

- [ ] **Step 1: Написать падающий тест**

```python
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
```

- [ ] **Step 2: Запустить тест и убедиться, что падает**

Run: `python -m pytest tests/test_torrserver_models.py -q`
Expected: FAIL — `ImportError: cannot import name 'parse_torrserver_size'`.

- [ ] **Step 3: Реализовать в `bot/models.py`**

Добавить в конец файла (рядом с существующими `format_bytes`/`format_speed`):

```python
#: Extensions Emby will actually play from a `.strm`; everything else in a
#: release (subtitles, posters, BDMV service files) is noise for the user.
VIDEO_FILE_EXTENSIONS = (".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mov", ".wmv")

#: TorrServer renders sizes as e.g. "2.5 GCiB" — a number, a binary-prefix
#: letter, then filler. Only the number and the prefix letter carry meaning.
_TS_SIZE_RE = re.compile(r"([\d]+(?:[.,][\d]+)?)\s*([KMGTP])?", re.IGNORECASE)
_TS_SIZE_POWERS = {"k": 1, "m": 2, "g": 3, "t": 4, "p": 5}


def parse_torrserver_size(value: "str | int | float | None") -> int:
    """Bytes from TorrServer's search-result size.

    The torznab search returns the size as a *string* ("2.5 GCiB"), unlike the
    torrent API which returns plain bytes — so both shapes are accepted.
    Anything unparseable is 0: a wrong size must never abort a search.
    """
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    match = _TS_SIZE_RE.search(str(value))
    if not match:
        return 0
    number = float(match.group(1).replace(",", "."))
    prefix = (match.group(2) or "").lower()
    return int(number * (1024 ** _TS_SIZE_POWERS.get(prefix, 0)))


def sanitize_torrent_title(title: str, max_length: int = 200) -> str:
    """Title safe to use as a directory name in TorrServer's virtual FS.

    A slash in a release title (routine on Russian trackers: "Холодное сердце 2 /
    Frozen II [...]") makes `PROPFIND` on its category return 500 with no
    entries at all — the folder looks empty in WebDAV and DLNA. TorrServer has a
    scheduled sanitizer, but it runs every 15 minutes; adding an already-clean
    title means the release is browsable immediately.
    """
    cleaned = re.sub(r"[\\/]+", " - ", title or "")
    cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_length]


class TorrServerFile(BaseModel):
    """One file inside a torrent."""

    id: int
    path: str
    length: int = 0


class TorrServerRelease(BaseModel):
    """A search hit from TorrServer's torznab endpoint."""

    title: str
    size: int = 0
    seeders: int = 0
    peers: int = 0
    link: str = ""
    tracker: str = ""
    year: Optional[int] = None


class TorrServerTorrent(BaseModel):
    """A torrent known to TorrServer."""

    hash: str
    title: str
    category: str = ""
    poster: str = ""
    size: int = 0
    stat: int = 0
    stat_string: str = ""
    files: list[TorrServerFile] = Field(default_factory=list)

    @property
    def video_files(self) -> list[TorrServerFile]:
        """Playable files, largest first."""
        videos = [f for f in self.files if f.path.lower().endswith(VIDEO_FILE_EXTENSIONS)]
        return sorted(videos, key=lambda f: f.length, reverse=True)


class TorrServerStats(BaseModel):
    """Snapshot for the status card."""

    version: str = "unknown"
    torrent_count: int = 0
    total_size: int = 0
    cache_size: int = 0
    use_disk: bool = False
    source_count: int = 0
```

Если `re` не импортирован в `bot/models.py` — добавить `import re` в шапку.

- [ ] **Step 4: Запустить тест и убедиться, что проходит**

Run: `python -m pytest tests/test_torrserver_models.py -q`
Expected: PASS, все кейсы.

- [ ] **Step 5: Коммит**

```bash
git add bot/models.py tests/test_torrserver_models.py
git commit -m "feat: models and parsers for TorrServer releases and torrents"
```

---

### Task 3: Клиент TorrServer — список, карточка, настройки, здоровье

**Files:**
- Create: `bot/clients/torrserver.py`
- Test: `tests/test_torrserver_client.py`

**Interfaces:**
- Consumes: модели из Task 2.
- Produces: `TorrServerError(Exception)`, `TorrServerClient(base_url: str, username: str, password: str, timeout: float = 30.0, search_timeout: float = 60.0)` с методами `get_version() -> str`, `list_torrents() -> list[TorrServerTorrent]`, `get_torrent(torrent_hash: str) -> TorrServerTorrent | None`, `get_server_settings() -> dict`, `get_stats() -> TorrServerStats`, `check_connection() -> tuple[bool, str | None, float]`, `close()`.

- [ ] **Step 1: Написать падающий тест**

```python
"""Клиент TorrServer: разбор ответов реального сервера (контракты сняты
живьём 2026-08-05) и поведение при ошибках."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.clients.base import AuthenticationError
from bot.clients.torrserver import TorrServerClient, TorrServerError

LIST_RESPONSE = [
    {
        "title": "Vice Principals - S2E1-9 [2017, WEB-DL 1080p]",
        "category": "tv",
        "poster": "https://image.tmdb.org/t/p/w300/x.jpg",
        "data": json.dumps({"TorrServer": {"Files": [
            {"id": 1, "path": "Vice Principals/S02E01.mkv", "length": 2423476098},
            {"id": 2, "path": "Vice Principals/S02E02.mkv", "length": 2257100799},
        ]}}),
        "timestamp": 1785923402,
        "hash": "536d2ce72b6ad45d21829c1eac5398276fa69be5",
        "stat": 5,
        "stat_string": "Torrent in db",
        "torrent_size": 20875200614,
    },
    {
        "title": "Broken data",
        "category": "movie",
        "data": "not json at all",
        "hash": "ffff",
        "stat": 5,
        "stat_string": "Torrent in db",
        "torrent_size": 100,
    },
]


@pytest.fixture
def client():
    return TorrServerClient("http://ts:8090", "admin", "pw")


def _patch_post(client, result):
    return patch.object(client, "post", new_callable=AsyncMock, return_value=result)


@pytest.mark.asyncio
async def test_list_torrents_parses_files_from_data(client):
    with _patch_post(client, LIST_RESPONSE):
        torrents = await client.list_torrents()

    assert len(torrents) == 2
    first = torrents[0]
    assert first.hash == "536d2ce72b6ad45d21829c1eac5398276fa69be5"
    assert first.size == 20875200614
    assert [f.id for f in first.files] == [1, 2]


@pytest.mark.asyncio
async def test_list_torrents_survives_unparseable_data(client):
    """Один битый элемент не должен ронять весь список."""
    with _patch_post(client, LIST_RESPONSE):
        torrents = await client.list_torrents()

    assert torrents[1].hash == "ffff"
    assert torrents[1].files == []


@pytest.mark.asyncio
async def test_list_torrents_sends_the_list_action(client):
    with _patch_post(client, []) as mocked:
        await client.list_torrents()

    assert mocked.await_args.kwargs["json_data"] == {"action": "list"}


@pytest.mark.asyncio
async def test_get_torrent_prefers_file_stats(client):
    payload = {
        "hash": "abc", "title": "T", "stat": 3, "stat_string": "Torrent working",
        "torrent_size": 276445467, "category": "movie", "poster": "",
        "file_stats": [
            {"id": 1, "path": "Big Buck Bunny/Big Buck Bunny.en.srt", "length": 140},
            {"id": 2, "path": "Big Buck Bunny/Big Buck Bunny.mp4", "length": 276134947},
        ],
        "data": "",
    }
    with _patch_post(client, payload):
        torrent = await client.get_torrent("abc")

    assert [f.id for f in torrent.files] == [1, 2]
    assert [f.id for f in torrent.video_files] == [2]


@pytest.mark.asyncio
async def test_get_torrent_returns_none_for_empty_answer(client):
    with _patch_post(client, {}):
        assert await client.get_torrent("nope") is None


@pytest.mark.asyncio
async def test_auth_error_is_translated_to_credentials_message(client):
    with patch.object(client, "post", new_callable=AsyncMock,
                      side_effect=AuthenticationError("boom", status_code=401)):
        with pytest.raises(TorrServerError, match="логин"):
            await client.list_torrents()


@pytest.mark.asyncio
async def test_get_stats_combines_version_settings_and_list(client):
    settings_payload = {"CacheSize": 1610612736, "UseDisk": False, "TorznabUrls": [
        {"Host": "http://p:9696/2", "Key": "k", "Name": "RuTracker.org"},
        {"Host": "http://p:9696/15", "Key": "k", "Name": "The Pirate Bay"},
    ]}

    async def fake_post(endpoint, json_data=None, **kwargs):
        if endpoint == "/settings":
            return settings_payload
        return LIST_RESPONSE

    with patch.object(client, "post", new=AsyncMock(side_effect=fake_post)), \
         patch.object(client, "get_version", new_callable=AsyncMock, return_value="MatriX.142.2"):
        stats = await client.get_stats()

    assert stats.version == "MatriX.142.2"
    assert stats.torrent_count == 2
    assert stats.total_size == 20875200614 + 100
    assert stats.cache_size == 1610612736
    assert stats.use_disk is False
    assert stats.source_count == 2


@pytest.mark.asyncio
async def test_basic_auth_header_is_built_from_credentials(client):
    headers = client._get_headers()
    assert headers["Authorization"] == "Basic YWRtaW46cHc="


@pytest.mark.asyncio
async def test_check_connection_reports_version(client):
    with patch.object(client, "get_version", new_callable=AsyncMock, return_value="MatriX.142.2"):
        available, version, elapsed = await client.check_connection()

    assert available is True
    assert version == "MatriX.142.2"
    assert elapsed >= 0


@pytest.mark.asyncio
async def test_check_connection_reports_failure(client):
    with patch.object(client, "get_version", new_callable=AsyncMock,
                      side_effect=TorrServerError("dead")):
        available, version, _ = await client.check_connection()

    assert available is False
    assert version is None
```

- [ ] **Step 2: Запустить тест и убедиться, что падает**

Run: `python -m pytest tests/test_torrserver_client.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'bot.clients.torrserver'`.

- [ ] **Step 3: Реализовать `bot/clients/torrserver.py`**

```python
"""TorrServer API client — the streaming contour ("watch now").

TorrServer speaks a small POST-with-an-`action`-field API rather than REST, and
authenticates with Basic auth instead of the `X-Api-Key` every other backend
here uses. Everything else (retries, pooling, slow-call logging, the TTL cache)
comes from BaseAPIClient unchanged.

All contracts below were taken from the live server on 2026-08-05, including a
probe torrent that was added and removed again — see the spec for the raw
responses.
"""

import base64
import json
import time
from typing import Any, Optional

import structlog

from bot.clients.base import APIError, AuthenticationError, BaseAPIClient, ServiceConnectionError
from bot.models import (
    TorrServerFile,
    TorrServerStats,
    TorrServerTorrent,
    parse_torrserver_size,
)

logger = structlog.get_logger()


class TorrServerError(Exception):
    """TorrServer API error, already phrased for the user."""


class TorrServerClient(BaseAPIClient):
    """Client for the TorrServer HTTP API."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout: float = 30.0,
        search_timeout: float = 60.0,
    ):
        # BaseAPIClient's api_key is the X-Api-Key value, which TorrServer has
        # no concept of — the credentials live in the Authorization header
        # built by _get_headers() below.
        super().__init__(base_url, api_key="", service_name="TorrServer")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.search_timeout = search_timeout

    def _get_headers(self) -> dict[str, str]:
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        return {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "TG_arr-bot/1.0",
        }

    async def _torrents(self, payload: dict[str, Any]) -> Any:
        """POST /torrents, translating transport/auth errors into TorrServerError."""
        try:
            return await self.post("/torrents", json_data=payload, timeout=self.timeout)
        except AuthenticationError as e:
            raise TorrServerError("Неверный логин или пароль TorrServer") from e
        except ServiceConnectionError as e:
            raise TorrServerError("TorrServer недоступен") from e
        except APIError as e:
            raise TorrServerError(f"Ошибка TorrServer: {e.message}") from e

    @staticmethod
    def _files_from_payload(item: dict[str, Any]) -> list[TorrServerFile]:
        """File list of a torrent.

        `file_stats` is populated only while a torrent is active; for the rest
        the composition lives in the `data` blob as a JSON *string*. A release
        whose blob doesn't parse still belongs in the list — it just has no
        known files.
        """
        raw_files = item.get("file_stats")
        if not raw_files:
            blob = item.get("data") or ""
            try:
                raw_files = (json.loads(blob).get("TorrServer") or {}).get("Files") or []
            except (ValueError, TypeError, AttributeError):
                logger.debug("torrserver_unparseable_data", torrent_hash=item.get("hash"))
                raw_files = []

        files = []
        for entry in raw_files:
            try:
                files.append(TorrServerFile(
                    id=int(entry["id"]),
                    path=str(entry.get("path", "")),
                    length=int(entry.get("length", 0)),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return files

    @classmethod
    def _to_torrent(cls, item: dict[str, Any]) -> TorrServerTorrent:
        return TorrServerTorrent(
            hash=str(item.get("hash", "")),
            title=str(item.get("title") or item.get("name") or "Без названия"),
            category=str(item.get("category") or ""),
            poster=str(item.get("poster") or ""),
            size=parse_torrserver_size(item.get("torrent_size")),
            stat=int(item.get("stat") or 0),
            stat_string=str(item.get("stat_string") or ""),
            files=cls._files_from_payload(item),
        )

    async def get_version(self) -> str:
        """Server version from /echo (the one endpoint open without auth)."""
        client = await self._get_client()
        try:
            response = await client.get("/echo", timeout=self.timeout)
        except Exception as e:
            raise TorrServerError("TorrServer недоступен") from e
        if response.status_code >= 400:
            raise TorrServerError(f"TorrServer вернул {response.status_code}")
        return (response.text or "").strip() or "unknown"

    async def list_torrents(self) -> list[TorrServerTorrent]:
        """All torrents known to the server."""
        result = await self._torrents({"action": "list"})
        if not isinstance(result, list):
            return []
        return [self._to_torrent(item) for item in result if isinstance(item, dict)]

    async def get_torrent(self, torrent_hash: str) -> Optional[TorrServerTorrent]:
        """One torrent by hash, or None if the server doesn't know it."""
        result = await self._torrents({"action": "get", "hash": torrent_hash})
        if not isinstance(result, dict) or not result.get("hash"):
            return None
        return self._to_torrent(result)

    async def get_server_settings(self) -> dict[str, Any]:
        """Raw settings object (cache size, torznab sources, ...)."""
        try:
            result = await self.post("/settings", json_data={"action": "get"}, timeout=self.timeout)
        except AuthenticationError as e:
            raise TorrServerError("Неверный логин или пароль TorrServer") from e
        except (ServiceConnectionError, APIError) as e:
            raise TorrServerError("TorrServer недоступен") from e
        return result if isinstance(result, dict) else {}

    async def get_stats(self) -> TorrServerStats:
        """Everything the status card shows, in one call site."""
        version = await self.get_version()
        settings = await self.get_server_settings()
        torrents = await self.list_torrents()
        return TorrServerStats(
            version=version,
            torrent_count=len(torrents),
            total_size=sum(t.size for t in torrents),
            cache_size=int(settings.get("CacheSize") or 0),
            use_disk=bool(settings.get("UseDisk")),
            source_count=len(settings.get("TorznabUrls") or []),
        )

    async def check_connection(self) -> tuple[bool, Optional[str], Optional[float]]:
        """Health probe for /status and the health monitor."""
        start_time = time.monotonic()
        try:
            version = await self.get_version()
            return True, version, round((time.monotonic() - start_time) * 1000, 2)
        except Exception as e:
            logger.warning("health_check_failed", service="TorrServer", error=str(e))
            return False, None, round((time.monotonic() - start_time) * 1000, 2)
```

- [ ] **Step 4: Запустить тест и убедиться, что проходит**

Run: `python -m pytest tests/test_torrserver_client.py -q`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add bot/clients/torrserver.py tests/test_torrserver_client.py
git commit -m "feat: TorrServer client for torrent listing, details and stats"
```

---

### Task 4: Клиент TorrServer — поиск по torznab

**Files:**
- Modify: `bot/clients/torrserver.py`
- Modify: `tests/test_torrserver_client.py`

**Interfaces:**
- Consumes: `TorrServerClient` из Task 3.
- Produces: `TorrServerClient.search(query: str, limit: int = 30) -> list[TorrServerRelease]`, `TorrServerClient.get_source_names() -> dict[str, str]`.

- [ ] **Step 1: Написать падающий тест (дописать в тот же файл)**

```python
SEARCH_RESPONSE = [
    {
        "Title": "Interstellar 2014 BDRemux 1080p", "Name": "Interstellar 2014 BDRemux 1080p",
        "Size": "2.5 GCiB", "CreateDate": "2026-07-18T20:49:00+03:00", "Tracker": "",
        "Link": "http://192.168.31.95:9696/2/download?apikey=k&link=abc&file=x",
        "Year": 2014, "Peer": 5, "Seed": 5, "Magnet": "", "Hash": "", "IMDBID": "tt0816692",
    },
    {
        "Title": "Interstellar 2014 WEBDL 2160p", "Name": "Interstellar 2014 WEBDL 2160p",
        "Size": "40 GCiB", "CreateDate": "", "Tracker": "Knaben",
        "Link": "http://192.168.31.95:9696/13/download?apikey=k&link=def",
        "Year": 0, "Peer": 90, "Seed": 120, "Magnet": "magnet:?xt=urn:btih:dead", "Hash": "",
    },
]

SETTINGS_WITH_SOURCES = {"CacheSize": 1, "UseDisk": False, "TorznabUrls": [
    {"Host": "http://192.168.31.95:9696/2", "Key": "k", "Name": "RuTracker.org"},
    {"Host": "http://192.168.31.95:9696/13", "Key": "k", "Name": "Knaben"},
]}


@pytest.mark.asyncio
async def test_search_sorts_by_seeders_and_parses_sizes(client):
    with patch.object(client, "get", new_callable=AsyncMock, return_value=SEARCH_RESPONSE), \
         patch.object(client, "get_server_settings", new_callable=AsyncMock,
                      return_value=SETTINGS_WITH_SOURCES):
        releases = await client.search("Interstellar")

    assert [r.seeders for r in releases] == [120, 5]
    assert releases[1].size == int(2.5 * 1024 ** 3)
    assert releases[0].link.endswith("link=def")


@pytest.mark.asyncio
async def test_search_fills_tracker_name_from_link_when_empty(client):
    with patch.object(client, "get", new_callable=AsyncMock, return_value=SEARCH_RESPONSE), \
         patch.object(client, "get_server_settings", new_callable=AsyncMock,
                      return_value=SETTINGS_WITH_SOURCES):
        releases = await client.search("Interstellar")

    by_title = {r.title: r for r in releases}
    assert by_title["Interstellar 2014 BDRemux 1080p"].tracker == "RuTracker.org"
    assert by_title["Interstellar 2014 WEBDL 2160p"].tracker == "Knaben"


@pytest.mark.asyncio
async def test_search_passes_query_as_query_string_not_path(client):
    """Гоча: /torznab/search/<строка> уходит пустым запросом и возвращает
    ленту последних раздач — «поиск работает, но нерелевантно»."""
    with patch.object(client, "get", new_callable=AsyncMock, return_value=[]) as mocked, \
         patch.object(client, "get_server_settings", new_callable=AsyncMock, return_value={}):
        await client.search("Дюна 2021")

    assert mocked.await_args.args[0] == "/torznab/search/"
    assert mocked.await_args.kwargs["params"] == {"query": "Дюна 2021"}


@pytest.mark.asyncio
async def test_search_truncates_to_limit(client):
    many = [dict(SEARCH_RESPONSE[0], Seed=i) for i in range(50)]
    with patch.object(client, "get", new_callable=AsyncMock, return_value=many), \
         patch.object(client, "get_server_settings", new_callable=AsyncMock, return_value={}):
        releases = await client.search("x", limit=30)

    assert len(releases) == 30


@pytest.mark.asyncio
async def test_search_returns_empty_list_on_unexpected_payload(client):
    with patch.object(client, "get", new_callable=AsyncMock, return_value={"error": "nope"}), \
         patch.object(client, "get_server_settings", new_callable=AsyncMock, return_value={}):
        assert await client.search("x") == []


@pytest.mark.asyncio
async def test_search_survives_missing_source_settings(client):
    """Настройки не прочитались — поиск всё равно работает, просто без имён."""
    with patch.object(client, "get", new_callable=AsyncMock, return_value=SEARCH_RESPONSE), \
         patch.object(client, "get_server_settings", new_callable=AsyncMock,
                      side_effect=TorrServerError("boom")):
        releases = await client.search("Interstellar")

    assert len(releases) == 2
    assert releases[1].tracker == ""
```

- [ ] **Step 2: Запустить тест и убедиться, что падает**

Run: `python -m pytest tests/test_torrserver_client.py -q -k search`
Expected: FAIL — `AttributeError: 'TorrServerClient' object has no attribute 'search'`.

- [ ] **Step 3: Дописать поиск в `bot/clients/torrserver.py`**

Импорты дополнить `re`, `TorrServerRelease`. Константу и методы добавить в класс:

```python
#: Prowlarr download links carry the indexer id: .../9696/<id>/download?...
_INDEXER_ID_RE = re.compile(r"/(\d+)/download")

#: TorznabUrls entries end with the same id: http://host:9696/<id>
_SOURCE_ID_RE = re.compile(r"/(\d+)/?$")
```

```python
    async def get_source_names(self) -> dict[str, str]:
        """Indexer id → display name, from the server's torznab settings.

        Cached for 10 minutes: sources change only when the operator edits them,
        and every search would otherwise pay an extra round-trip.
        """
        async def fetch() -> dict[str, str]:
            settings = await self.get_server_settings()
            names: dict[str, str] = {}
            for source in settings.get("TorznabUrls") or []:
                match = _SOURCE_ID_RE.search(str(source.get("Host", "")))
                if match:
                    names[match.group(1)] = str(source.get("Name") or "")
            return names

        return await self._ttl_cached("torznab_sources", 600.0, fetch)

    async def search(self, query: str, limit: int = 30) -> list[TorrServerRelease]:
        """Search releases through TorrServer's torznab bridge.

        The query MUST travel in the query-string: `/torznab/search/<text>`
        reaches the server as an *empty* query and answers with an RSS-ish feed
        of recent releases — which looks like a working but wildly irrelevant
        search. A single answer is ~470 KB, so the list is cut to `limit`
        immediately and the raw payload is never stored.
        """
        try:
            result = await self.get(
                "/torznab/search/", params={"query": query}, timeout=self.search_timeout,
            )
        except AuthenticationError as e:
            raise TorrServerError("Неверный логин или пароль TorrServer") from e
        except ServiceConnectionError as e:
            raise TorrServerError("TorrServer не ответил на поиск") from e
        except APIError as e:
            raise TorrServerError(f"Ошибка поиска: {e.message}") from e

        if not isinstance(result, list):
            return []

        try:
            source_names = await self.get_source_names()
        except TorrServerError:
            # Names are cosmetic — a search must not fail because of them.
            source_names = {}

        releases = []
        for item in result:
            if not isinstance(item, dict):
                continue
            link = str(item.get("Link") or item.get("Magnet") or "")
            if not link:
                continue
            tracker = str(item.get("Tracker") or "")
            if not tracker:
                match = _INDEXER_ID_RE.search(link)
                tracker = source_names.get(match.group(1), "") if match else ""
            year = item.get("Year") or None
            releases.append(TorrServerRelease(
                title=str(item.get("Title") or item.get("Name") or "Без названия"),
                size=parse_torrserver_size(item.get("Size")),
                seeders=int(item.get("Seed") or 0),
                peers=int(item.get("Peer") or 0),
                link=link,
                tracker=tracker,
                year=int(year) if year else None,
            ))

        releases.sort(key=lambda r: r.seeders, reverse=True)
        return releases[:limit]
```

- [ ] **Step 4: Запустить тест и убедиться, что проходит**

Run: `python -m pytest tests/test_torrserver_client.py -q`
Expected: PASS — и новые тесты поиска, и тесты из Task 3.

- [ ] **Step 5: Коммит**

```bash
git add bot/clients/torrserver.py tests/test_torrserver_client.py
git commit -m "feat: torznab search in the TorrServer client"
```

---

### Task 5: Клиент TorrServer — добавление и удаление

**Files:**
- Modify: `bot/clients/torrserver.py`
- Modify: `tests/test_torrserver_client.py`

**Interfaces:**
- Consumes: `TorrServerClient` из Task 3–4.
- Produces: `TorrServerClient.add_torrent(link: str, title: str, poster: str = "") -> TorrServerTorrent`, `TorrServerClient.remove_torrent(torrent_hash: str) -> None`, `TorrServerClient.stream_url(torrent_hash: str, file_id: int, file_name: str) -> str`.

- [ ] **Step 1: Написать падающий тест (дописать в тот же файл)**

```python
@pytest.mark.asyncio
async def test_add_torrent_sends_sanitized_title(client):
    """Слэш в названии ломает листинг WebDAV — чистим до добавления."""
    added = {"title": "X", "hash": "abc", "stat": 1,
             "stat_string": "Torrent getting info", "torrent_size": None}
    with _patch_post(client, added) as mocked:
        torrent = await client.add_torrent(
            "http://p:9696/2/download?link=a",
            "Холодное сердце 2 / Frozen II [2019]",
        )

    payload = mocked.await_args.kwargs["json_data"]
    assert payload["action"] == "add"
    assert payload["title"] == "Холодное сердце 2 - Frozen II [2019]"
    assert payload["link"] == "http://p:9696/2/download?link=a"
    assert payload["save_to_db"] is True
    assert torrent.hash == "abc"
    assert torrent.size == 0  # torrent_size приходит null сразу после добавления


@pytest.mark.asyncio
async def test_add_torrent_without_hash_is_an_error(client):
    with _patch_post(client, {"title": "X"}):
        with pytest.raises(TorrServerError, match="не принял"):
            await client.add_torrent("http://link", "X")


@pytest.mark.asyncio
async def test_remove_torrent_sends_rem_action(client):
    with _patch_post(client, "") as mocked:
        await client.remove_torrent("abc")

    assert mocked.await_args.kwargs["json_data"] == {"action": "rem", "hash": "abc"}


def test_stream_url_matches_the_working_sync_script(client):
    url = client.stream_url("abc", 2, "Big Buck Bunny/Big Buck Bunny.mp4")
    assert url == "http://ts:8090/stream/Big%20Buck%20Bunny.mp4?link=abc&index=2&play"
```

- [ ] **Step 2: Запустить тест и убедиться, что падает**

Run: `python -m pytest tests/test_torrserver_client.py -q -k "add_torrent or remove_torrent or stream_url"`
Expected: FAIL — `AttributeError: ... has no attribute 'add_torrent'`.

- [ ] **Step 3: Дописать методы в `bot/clients/torrserver.py`**

Импорты дополнить: `from pathlib import PurePosixPath`, `from urllib.parse import quote`, `from bot.models import sanitize_torrent_title`.

```python
    async def add_torrent(self, link: str, title: str, poster: str = "") -> TorrServerTorrent:
        """Add a torrent by link (Prowlarr download URL or magnet).

        Answers immediately, before metadata is fetched: `stat` is 1
        ("Torrent getting info") and `torrent_size` is null — the caller has to
        poll `get_torrent` if it needs the file list.
        """
        payload = {
            "action": "add",
            "link": link,
            "title": sanitize_torrent_title(title),
            "poster": poster,
            "save_to_db": True,
        }
        result = await self._torrents(payload)
        if not isinstance(result, dict) or not result.get("hash"):
            raise TorrServerError("TorrServer не принял раздачу")
        return self._to_torrent(result)

    async def remove_torrent(self, torrent_hash: str) -> None:
        """Remove a torrent. The `.strm` files it produced are cleaned up by
        the sync script on its next pass, not here."""
        await self._torrents({"action": "rem", "hash": torrent_hash})

    def stream_url(self, torrent_hash: str, file_id: int, file_name: str) -> str:
        """Direct HTTP stream link for one file — same shape the working
        Sync-TorrServerToEmby.py writes into every `.strm`."""
        name = quote(PurePosixPath(file_name).name)
        return f"{self.base_url}/stream/{name}?link={torrent_hash}&index={file_id}&play"
```

- [ ] **Step 4: Запустить тест и убедиться, что проходит**

Run: `python -m pytest tests/test_torrserver_client.py -q`
Expected: PASS, весь файл.

- [ ] **Step 5: Коммит**

```bash
git add bot/clients/torrserver.py tests/test_torrserver_client.py
git commit -m "feat: add, remove and stream-link helpers in the TorrServer client"
```

---

### Task 6: Клиент хука синхронизации с Emby

**Files:**
- Create: `bot/clients/emby_sync_hook.py`
- Modify: `bot/models.py`
- Test: `tests/test_emby_sync_hook.py`

**Interfaces:**
- Consumes: ничего из предыдущих задач.
- Produces: `SyncHookResult(status: str, duration_s: float | None, error: str | None)` в `bot/models.py` со `status` из `{"ok", "already_running", "failed"}`; `EmbySyncHookClient(base_url: str, token: str, timeout: float = 90.0)` с `trigger_sync() -> SyncHookResult` и `close()`.

- [ ] **Step 1: Написать падающий тест**

```python
"""Клиент хука принудительного синка: любая беда хука — это деградация,
а не отказ, поэтому trigger_sync никогда не бросает."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from bot.clients.emby_sync_hook import EmbySyncHookClient


@pytest.fixture
def hook():
    return EmbySyncHookClient("http://hs:8099", "secret-token")


def _response(status_code: int, payload: dict | None = None):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload if payload is not None else {}
    response.text = "" if payload is None else "body"
    return response


@pytest.mark.asyncio
async def test_successful_sync_reports_ok_with_duration(hook):
    http = AsyncMock()
    http.post.return_value = _response(200, {"status": "ok", "duration_s": 4.2})
    with patch.object(hook, "_get_client", new_callable=AsyncMock, return_value=http):
        result = await hook.trigger_sync()

    assert result.status == "ok"
    assert result.duration_s == 4.2
    assert result.error is None


@pytest.mark.asyncio
async def test_token_is_sent_in_header(hook):
    http = AsyncMock()
    http.post.return_value = _response(200, {"status": "ok"})
    with patch.object(hook, "_get_client", new_callable=AsyncMock, return_value=http):
        await hook.trigger_sync()

    assert http.post.await_args.kwargs["headers"]["X-Token"] == "secret-token"
    assert http.post.await_args.args[0] == "/sync"


@pytest.mark.asyncio
async def test_202_means_a_sync_is_already_running(hook):
    http = AsyncMock()
    http.post.return_value = _response(202, {"status": "already_running"})
    with patch.object(hook, "_get_client", new_callable=AsyncMock, return_value=http):
        result = await hook.trigger_sync()

    assert result.status == "already_running"


@pytest.mark.asyncio
async def test_403_is_reported_as_failure_with_reason(hook):
    http = AsyncMock()
    http.post.return_value = _response(403)
    with patch.object(hook, "_get_client", new_callable=AsyncMock, return_value=http):
        result = await hook.trigger_sync()

    assert result.status == "failed"
    assert "403" in result.error


@pytest.mark.asyncio
async def test_connection_error_never_raises(hook):
    http = AsyncMock()
    http.post.side_effect = httpx.ConnectError("no route")
    with patch.object(hook, "_get_client", new_callable=AsyncMock, return_value=http):
        result = await hook.trigger_sync()

    assert result.status == "failed"
    assert result.error


@pytest.mark.asyncio
async def test_timeout_never_raises(hook):
    http = AsyncMock()
    http.post.side_effect = httpx.TimeoutException("slow")
    with patch.object(hook, "_get_client", new_callable=AsyncMock, return_value=http):
        result = await hook.trigger_sync()

    assert result.status == "failed"
    assert "аймаут" in result.error


@pytest.mark.asyncio
async def test_non_json_success_body_is_still_ok(hook):
    response = _response(200)
    response.json.side_effect = ValueError("not json")
    http = AsyncMock()
    http.post.return_value = response
    with patch.object(hook, "_get_client", new_callable=AsyncMock, return_value=http):
        result = await hook.trigger_sync()

    assert result.status == "ok"
    assert result.duration_s is None
```

- [ ] **Step 2: Запустить тест и убедиться, что падает**

Run: `python -m pytest tests/test_emby_sync_hook.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'bot.clients.emby_sync_hook'`.

- [ ] **Step 3: Добавить модель в `bot/models.py`**

```python
class SyncHookResult(BaseModel):
    """Outcome of a forced Emby sync request.

    `status` is one of "ok" (sync ran), "already_running" (a pass was in
    flight, ours was folded into it) or "failed" (the hook could not be
    reached or refused) — a failure here never invalidates the torrent that
    was already added.
    """

    status: str = "failed"
    duration_s: Optional[float] = None
    error: Optional[str] = None
```

- [ ] **Step 4: Реализовать `bot/clients/emby_sync_hook.py`**

```python
"""Client for the forced Emby sync hook running next to TorrServer.

The bot's container has neither ssh nor curl, so the `.strm` sync on Homeserver
is reached over plain HTTP. Every failure mode here is a *degradation*: the
torrent has already been added, and the scheduled `TorrServer-EmbySync` task
will publish it within ten minutes anyway. That is why nothing in this module
raises.
"""

from typing import Optional

import httpx
import structlog

from bot.models import SyncHookResult

logger = structlog.get_logger()


class EmbySyncHookClient:
    """Triggers `Sync-TorrServerToEmby.py --apply` on Homeserver."""

    def __init__(self, base_url: str, token: str, timeout: float = 90.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout),
                limits=httpx.Limits(max_keepalive_connections=1, max_connections=2),
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def trigger_sync(self) -> SyncHookResult:
        """Ask the hook to publish `.strm` files and refresh Emby now."""
        client = await self._get_client()
        try:
            response = await client.post("/sync", headers={"X-Token": self.token})
        except httpx.TimeoutException:
            logger.warning("emby_sync_hook", status="timeout")
            return SyncHookResult(status="failed", error="таймаут хука синхронизации")
        except httpx.HTTPError as e:
            logger.warning("emby_sync_hook", status="unreachable", error=str(e))
            return SyncHookResult(status="failed", error="хук синхронизации недоступен")

        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        if response.status_code == 202:
            logger.info("emby_sync_hook", status="already_running")
            return SyncHookResult(status="already_running")

        if response.status_code >= 400:
            logger.warning("emby_sync_hook", status="rejected", status_code=response.status_code)
            return SyncHookResult(
                status="failed", error=f"хук ответил {response.status_code}",
            )

        duration = payload.get("duration_s")
        logger.info("emby_sync_hook", status="ok", duration_s=duration)
        return SyncHookResult(
            status="ok",
            duration_s=float(duration) if isinstance(duration, (int, float)) else None,
        )
```

- [ ] **Step 5: Запустить тест и убедиться, что проходит**

Run: `python -m pytest tests/test_emby_sync_hook.py -q`
Expected: PASS.

- [ ] **Step 6: Коммит**

```bash
git add bot/clients/emby_sync_hook.py bot/models.py tests/test_emby_sync_hook.py
git commit -m "feat: client for the forced Emby sync hook"
```

---

### Task 7: Сервис «добавить и опубликовать» и регистрация клиентов

**Files:**
- Create: `bot/services/torrserver_service.py`
- Modify: `bot/clients/registry.py`
- Test: `tests/test_torrserver_service.py`

**Interfaces:**
- Consumes: `TorrServerClient`, `EmbySyncHookClient`, `SyncHookResult`, `TorrServerTorrent`.
- Produces: `AddResult(torrent: TorrServerTorrent, metadata_ready: bool, sync: SyncHookResult | None, stream_url: str | None)` (dataclass в `bot/services/torrserver_service.py`); `TorrServerService(client, hook=None, metadata_timeout=30.0, poll_interval=2.0)` с `add_and_publish(link: str, title: str, poster: str = "") -> AddResult`; `registry.get_torrserver() -> TorrServerClient | None`, `registry.get_emby_sync_hook() -> EmbySyncHookClient | None`, `registry.get_torrserver_service() -> TorrServerService | None`.

- [ ] **Step 1: Написать падающий тест**

```python
"""Сценарий «добавил раздачу — опубликовал в Emby».

Главное свойство: раздача уже добавлена, поэтому ни таймаут метаданных, ни
отказ хука не превращают операцию в неудачу — меняется только текст ответа.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.models import SyncHookResult, TorrServerFile, TorrServerTorrent
from bot.services.torrserver_service import TorrServerService

ADDED = TorrServerTorrent(hash="abc", title="Dune 2021", stat=1,
                          stat_string="Torrent getting info")
READY = TorrServerTorrent(
    hash="abc", title="Dune 2021", stat=3, stat_string="Torrent working", size=100,
    files=[
        TorrServerFile(id=1, path="Dune/Dune.2021.mkv", length=90),
        TorrServerFile(id=2, path="Dune/Dune.srt", length=1),
    ],
)


def _client(get_results):
    client = MagicMock()
    client.add_torrent = AsyncMock(return_value=ADDED)
    client.get_torrent = AsyncMock(side_effect=get_results)
    client.stream_url = MagicMock(return_value="http://ts:8090/stream/Dune.2021.mkv?link=abc&index=1&play")
    return client


def _hook(status="ok"):
    hook = MagicMock()
    hook.trigger_sync = AsyncMock(return_value=SyncHookResult(status=status, duration_s=1.0))
    return hook


@pytest.mark.asyncio
async def test_waits_for_metadata_then_triggers_sync():
    client = _client([ADDED, READY])
    hook = _hook()
    service = TorrServerService(client, hook, metadata_timeout=10.0, poll_interval=0)

    result = await service.add_and_publish("http://link", "Dune 2021")

    assert result.metadata_ready is True
    assert result.sync.status == "ok"
    assert result.stream_url.endswith("index=1&play")
    hook.trigger_sync.assert_awaited_once()


@pytest.mark.asyncio
async def test_metadata_timeout_still_counts_as_added_and_skips_sync():
    """Синк по раздаче без файлов создал бы пустышку — не зовём его."""
    client = _client([ADDED, ADDED, ADDED])
    hook = _hook()
    service = TorrServerService(client, hook, metadata_timeout=0.05, poll_interval=0.01)

    result = await service.add_and_publish("http://link", "Dune 2021")

    assert result.torrent.hash == "abc"
    assert result.metadata_ready is False
    assert result.sync is None
    assert result.stream_url is None
    hook.trigger_sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_hook_failure_does_not_break_the_add():
    client = _client([READY])
    hook = _hook(status="failed")
    service = TorrServerService(client, hook, metadata_timeout=10.0, poll_interval=0)

    result = await service.add_and_publish("http://link", "Dune 2021")

    assert result.metadata_ready is True
    assert result.sync.status == "failed"
    assert result.stream_url


@pytest.mark.asyncio
async def test_missing_hook_is_allowed():
    client = _client([READY])
    service = TorrServerService(client, None, metadata_timeout=10.0, poll_interval=0)

    result = await service.add_and_publish("http://link", "Dune 2021")

    assert result.sync is None
    assert result.metadata_ready is True


@pytest.mark.asyncio
async def test_release_without_video_files_has_no_stream_link():
    audio_only = TorrServerTorrent(
        hash="abc", title="OST", stat=3, stat_string="Torrent working",
        files=[TorrServerFile(id=1, path="OST/track.flac", length=10)],
    )
    client = _client([audio_only])
    service = TorrServerService(client, _hook(), metadata_timeout=10.0, poll_interval=0)

    result = await service.add_and_publish("http://link", "OST")

    assert result.metadata_ready is True
    assert result.stream_url is None
```

- [ ] **Step 2: Запустить тест и убедиться, что падает**

Run: `python -m pytest tests/test_torrserver_service.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'bot.services.torrserver_service'`.

- [ ] **Step 3: Реализовать `bot/services/torrserver_service.py`**

```python
"""Adding a torrent to TorrServer and publishing it to Emby in one go.

The whole point of the section is "watch it now", so the bot does not leave the
user waiting for the scheduled sync: it adds the torrent, waits until the
server actually knows the file list, and only then asks the hook to write the
`.strm` files and refresh Emby.

Waiting matters. Right after `add` the torrent reports `stat: 1` ("Torrent
getting info") with no files at all; syncing at that moment would publish an
empty release and the next scheduled pass would have to fix it.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

import structlog

from bot.clients.emby_sync_hook import EmbySyncHookClient
from bot.clients.torrserver import TorrServerClient
from bot.models import SyncHookResult, TorrServerTorrent

logger = structlog.get_logger()


@dataclass
class AddResult:
    """Everything the answer message needs after an add."""

    torrent: TorrServerTorrent
    metadata_ready: bool
    sync: Optional[SyncHookResult] = None
    stream_url: Optional[str] = None


class TorrServerService:
    """Orchestrates add → wait for metadata → forced Emby sync."""

    def __init__(
        self,
        client: TorrServerClient,
        hook: Optional[EmbySyncHookClient] = None,
        metadata_timeout: float = 30.0,
        poll_interval: float = 2.0,
    ):
        self.client = client
        self.hook = hook
        self.metadata_timeout = metadata_timeout
        self.poll_interval = poll_interval

    async def _wait_for_files(self, torrent_hash: str) -> Optional[TorrServerTorrent]:
        """Poll until the torrent reports its files, or the budget runs out."""
        deadline = time.monotonic() + self.metadata_timeout
        while True:
            torrent = await self.client.get_torrent(torrent_hash)
            if torrent and torrent.files:
                return torrent
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(self.poll_interval)

    async def add_and_publish(self, link: str, title: str, poster: str = "") -> AddResult:
        """Add a release and make it visible in Emby as soon as possible."""
        added = await self.client.add_torrent(link, title, poster)
        logger.info("torrserver_add", torrent_hash=added.hash, title=added.title)

        ready = await self._wait_for_files(added.hash)
        if ready is None:
            logger.warning("torrserver_metadata_timeout", torrent_hash=added.hash)
            return AddResult(torrent=added, metadata_ready=False)

        stream_url = None
        videos = ready.video_files
        if videos:
            stream_url = self.client.stream_url(ready.hash, videos[0].id, videos[0].path)

        sync = await self.hook.trigger_sync() if self.hook else None
        return AddResult(
            torrent=ready, metadata_ready=True, sync=sync, stream_url=stream_url,
        )
```

- [ ] **Step 4: Запустить тест и убедиться, что проходит**

Run: `python -m pytest tests/test_torrserver_service.py -q`
Expected: PASS.

- [ ] **Step 5: Зарегистрировать клиенты в `bot/clients/registry.py`**

В `TYPE_CHECKING`-блок добавить импорты `EmbySyncHookClient`, `TorrServerClient`, `TorrServerService`. Рядом с остальными локами и синглтонами:

```python
_torrserver_lock = asyncio.Lock()
_emby_sync_hook_lock = asyncio.Lock()

_torrserver: Optional["TorrServerClient"] = None
_emby_sync_hook: Optional["EmbySyncHookClient"] = None
```

Функции — после `get_emby()`:

```python
async def get_torrserver() -> Optional["TorrServerClient"]:
    """Get or create the TorrServer client singleton (if configured)."""
    global _torrserver
    settings = get_settings()
    if not settings.torrserver_enabled:
        return None
    async with _torrserver_lock:
        if _torrserver is None:
            from bot.clients.torrserver import TorrServerClient

            _torrserver = TorrServerClient(
                settings.torrserver_url,
                settings.torrserver_username,
                settings.torrserver_password,
                timeout=settings.torrserver_timeout,
                search_timeout=settings.torrserver_search_timeout,
            )
    return _torrserver


async def get_emby_sync_hook() -> Optional["EmbySyncHookClient"]:
    """Get or create the Emby sync hook client singleton (if configured)."""
    global _emby_sync_hook
    settings = get_settings()
    if not settings.emby_sync_hook_enabled:
        return None
    async with _emby_sync_hook_lock:
        if _emby_sync_hook is None:
            from bot.clients.emby_sync_hook import EmbySyncHookClient

            _emby_sync_hook = EmbySyncHookClient(
                settings.emby_sync_hook_url,
                settings.emby_sync_hook_token,
                timeout=settings.emby_sync_hook_timeout,
            )
    return _emby_sync_hook


async def get_torrserver_service() -> Optional["TorrServerService"]:
    """Compose the TorrServer client and (optional) sync hook into the service.

    Cheap to build and stateless, so it is assembled per call rather than kept
    as another singleton — the expensive parts (the HTTP clients) are cached.
    """
    client = await get_torrserver()
    if client is None:
        return None
    from bot.services.torrserver_service import TorrServerService

    settings = get_settings()
    return TorrServerService(
        client,
        await get_emby_sync_hook(),
        metadata_timeout=settings.torrserver_metadata_timeout,
    )
```

В `close_all()` добавить в объявление `global` имена `_torrserver, _emby_sync_hook` и закрытие:

```python
    if _torrserver:
        await _torrserver.close()
        _torrserver = None
    if _emby_sync_hook:
        await _emby_sync_hook.close()
        _emby_sync_hook = None
```

- [ ] **Step 6: Прогнать тесты и линт**

Run: `make test` и `make lint`
Expected: зелено и чисто.

- [ ] **Step 7: Коммит**

```bash
git add bot/services/torrserver_service.py bot/clients/registry.py tests/test_torrserver_service.py
git commit -m "feat: service that adds a torrent and publishes it to Emby at once"
```

---

### Task 8: UI — кнопка меню, клавиатуры, форматтеры

**Files:**
- Modify: `bot/ui/menu.py`, `bot/ui/keyboards/menu.py`, `bot/ui/keyboards/_constants.py`, `bot/ui/keyboards/__init__.py`, `bot/ui/formatters/__init__.py`, `bot/ui/callbacks.py`
- Create: `bot/ui/keyboards/torrserver.py`, `bot/ui/formatters/torrserver.py`
- Test: `tests/test_torrserver_ui.py`

**Interfaces:**
- Consumes: модели из Task 2.
- Produces: `MENU_TORRSERVER = "▶️ Смотреть"` и `TORRSERVER_PROMPT = "▶️ Что найти в TorrServer?"` в `bot/ui/menu.py`; `TsReleaseCB(idx: int)`, `TsPageCB(page: int)`, `TsTorrentCB(action: str, h: str)` в `bot/ui/callbacks.py`; `CallbackData.TS_SEARCH/TS_LIST/TS_REFRESH/TS_CLOSE/TS_BACK`; `Keyboards.torrserver_panel()`, `Keyboards.torrserver_results(releases, page, total_pages)`, `Keyboards.torrserver_release(idx)`, `Keyboards.torrserver_list(torrents, is_admin)`, `Keyboards.torrserver_confirm_delete(torrent_hash)`; `Formatters.format_torrserver_status(stats)`, `Formatters.format_torrserver_results(releases, page, per_page, total)`, `Formatters.format_torrserver_release(release)`, `Formatters.format_torrserver_torrents(torrents)`, `Formatters.format_torrserver_added(result)`.

- [ ] **Step 1: Написать падающий тест**

```python
"""Кнопки и тексты раздела TorrServer."""

import pytest

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
```

- [ ] **Step 2: Запустить тест и убедиться, что падает**

Run: `python -m pytest tests/test_torrserver_ui.py -q`
Expected: FAIL — `ImportError: cannot import name 'MENU_TORRSERVER'`.

- [ ] **Step 3: Константы меню и callback-данных**

`bot/ui/menu.py` — добавить константы и включить кнопку в набор:

```python
MENU_TORRSERVER = "▶️ Смотреть"

#: Text of the ForceReply prompt that asks for a TorrServer query. The handler
#: matches replies against this exact string, so it lives here next to the
#: button labels for the same reason they do: filter and message must not drift.
TORRSERVER_PROMPT = "▶️ Что найти в TorrServer?"
```

и добавить `MENU_TORRSERVER,` в `MENU_BUTTONS`.

`bot/ui/keyboards/menu.py` — импортировать `MENU_TORRSERVER` и заменить последний ряд:

```python
                [KeyboardButton(text=MENU_TORRSERVER), KeyboardButton(text=MENU_HISTORY)],
```

`bot/ui/keyboards/_constants.py` — добавить в класс `CallbackData`:

```python
    # TorrServer section ("watch now")
    TS_SEARCH = "ts_search"  # Ask for a query
    TS_LIST = "ts_list"  # Show torrents currently on the server
    TS_REFRESH = "ts_refresh"  # Re-render the panel
    TS_CLOSE = "ts_close"  # Close the panel message
    TS_BACK = "ts_back"  # Back to the panel
```

`bot/ui/callbacks.py` — добавить фабрики:

```python
class TsReleaseCB(CallbackData, prefix="tsr"):
    """Pick one TorrServer search hit. Only the index travels: the release
    itself (title + Prowlarr link) blows past callback_data's 64-byte budget,
    so the handler resolves it from the per-user result cache."""

    idx: int


class TsPageCB(CallbackData, prefix="tsp"):
    """Pagination inside a TorrServer result list."""

    page: int


class TsTorrentCB(CallbackData, prefix="tst"):
    """Per-torrent action on the server. ``action``: del (asks) | delconf (does it).
    ``h`` carries the full 40-hex hash — "tst:delconf:<40 hex>" packs to 53
    bytes, under the 64-byte limit."""

    action: str
    h: str


class TsAddCB(CallbackData, prefix="tsa"):
    """Add the selected hit to TorrServer and publish it to Emby.

    Separate from ``TsReleaseCB`` (which only opens the card) so the two
    actions can never be confused by a shared prefix.
    """

    idx: int
```

- [ ] **Step 4: Создать `bot/ui/keyboards/torrserver.py`**

```python
"""Inline keyboards for the TorrServer section."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.models import TorrServerRelease, TorrServerTorrent
from bot.ui.callbacks import TsAddCB, TsPageCB, TsReleaseCB, TsTorrentCB
from bot.ui.keyboards._constants import CallbackData


class _TorrServerKeyboards:
    """TorrServer section keyboards mixin."""

    @staticmethod
    def torrserver_panel() -> InlineKeyboardMarkup:
        """Main panel under the status card."""
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔎 Найти", callback_data=CallbackData.TS_SEARCH),
                InlineKeyboardButton(text="📋 Раздачи", callback_data=CallbackData.TS_LIST),
            ],
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data=CallbackData.TS_REFRESH),
                InlineKeyboardButton(text="✖️ Закрыть", callback_data=CallbackData.TS_CLOSE),
            ],
        ])

    @staticmethod
    def torrserver_results(
        releases: list[TorrServerRelease], page: int, total_pages: int, offset: int = 0
    ) -> InlineKeyboardMarkup:
        """One button per hit on this page, plus pagination.

        `offset` is the absolute position of the first item on this page: the
        handler resolves a pick against the whole cached result list, so a
        page-relative index would open the wrong release on page two.
        """
        builder = InlineKeyboardBuilder()
        for position, release in enumerate(releases):
            idx = offset + position
            builder.row(InlineKeyboardButton(
                text=f"{idx + 1}. {release.title[:40]}",
                callback_data=TsReleaseCB(idx=idx).pack(),
            ))

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                text="⬅️", callback_data=TsPageCB(page=page - 1).pack()))
        if total_pages > 1:
            nav.append(InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(
                text="➡️", callback_data=TsPageCB(page=page + 1).pack()))
        if nav:
            builder.row(*nav)

        builder.row(InlineKeyboardButton(text="⬅️ В меню", callback_data=CallbackData.TS_BACK))
        return builder.as_markup()

    @staticmethod
    def torrserver_release(idx: int) -> InlineKeyboardMarkup:
        """Card of one hit: watch it, or go back to the list.

        The watch button is its own callback family (`TsAddCB`) rather than a
        suffixed `TsReleaseCB` — appending to a packed payload would break
        aiogram's unpacking.
        """
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Смотреть", callback_data=TsAddCB(idx=idx).pack())],
            [InlineKeyboardButton(text="⬅️ К результатам", callback_data=TsPageCB(page=0).pack())],
        ])

    @staticmethod
    def torrserver_list(
        torrents: list[TorrServerTorrent], is_admin: bool
    ) -> InlineKeyboardMarkup:
        """Torrents on the server; deletion is admin-only."""
        builder = InlineKeyboardBuilder()
        if is_admin:
            for torrent in torrents:
                builder.row(InlineKeyboardButton(
                    text=f"🗑 {torrent.title[:35]}",
                    callback_data=TsTorrentCB(action="del", h=torrent.hash).pack(),
                ))
        builder.row(InlineKeyboardButton(text="⬅️ В меню", callback_data=CallbackData.TS_BACK))
        return builder.as_markup()

    @staticmethod
    def torrserver_confirm_delete(torrent_hash: str) -> InlineKeyboardMarkup:
        """Deletion is destructive — ask first, same as the qBittorrent flow."""
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Удалить",
                    callback_data=TsTorrentCB(action="delconf", h=torrent_hash).pack()),
                InlineKeyboardButton(text="⬅️ Отмена", callback_data=CallbackData.TS_LIST),
            ],
        ])
```

`bot/ui/keyboards/__init__.py` — импортировать `_TorrServerKeyboards` и добавить в базы `Keyboards`.

- [ ] **Step 5: Создать `bot/ui/formatters/torrserver.py`**

```python
"""Message formatters for the TorrServer section (HTML parse mode)."""

import html

from bot.models import (
    TorrServerRelease,
    TorrServerStats,
    TorrServerTorrent,
    format_bytes,
)

#: Shown whenever a release will reach Emby by the scheduled pass rather than
#: by our forced sync — the user should know they are not stuck.
_SCHEDULED_FALLBACK = "В Emby попадёт штатной задачей в течение 10 минут."


class _TorrServerFormatters:
    """TorrServer section formatters mixin."""

    @staticmethod
    def format_torrserver_status(stats: TorrServerStats) -> str:
        cache_mode = "диск" if stats.use_disk else "RAM"
        return (
            "▶️ <b>TorrServer</b>\n\n"
            f"Версия: <code>{html.escape(stats.version)}</code>\n"
            f"Раздач: <b>{stats.torrent_count}</b> ({format_bytes(stats.total_size)})\n"
            f"Кеш: {format_bytes(stats.cache_size)} в {cache_mode}\n"
            f"Источников поиска: {stats.source_count}"
        )

    @staticmethod
    def format_torrserver_results(
        releases: list[TorrServerRelease], page: int, per_page: int, total: int
    ) -> str:
        start = page * per_page
        lines = [f"🔎 <b>Найдено раздач: {total}</b>", ""]
        for idx, release in enumerate(releases, start=start + 1):
            tracker = f" · {html.escape(release.tracker)}" if release.tracker else ""
            lines.append(
                f"<b>{idx}.</b> {html.escape(release.title[:90])}\n"
                f"    {format_bytes(release.size)} · 🌱 {release.seeders}{tracker}"
            )
        return "\n".join(lines)

    @staticmethod
    def format_torrserver_release(release: TorrServerRelease) -> str:
        year = f" ({release.year})" if release.year else ""
        tracker = html.escape(release.tracker) if release.tracker else "неизвестен"
        return (
            f"🎬 <b>{html.escape(release.title)}</b>{year}\n\n"
            f"Размер: {format_bytes(release.size)}\n"
            f"Сиды: {release.seeders} · Пиры: {release.peers}\n"
            f"Трекер: {tracker}\n\n"
            "Раздача не скачивается на диск — TorrServer отдаёт её потоком."
        )

    @staticmethod
    def format_torrserver_torrents(torrents: list[TorrServerTorrent]) -> str:
        if not torrents:
            return "📋 <b>Раздачи TorrServer</b>\n\nСписок пуст."
        lines = [f"📋 <b>Раздачи TorrServer: {len(torrents)}</b>", ""]
        for torrent in torrents:
            lines.append(
                f"• <b>{html.escape(torrent.title[:70])}</b>\n"
                f"    {format_bytes(torrent.size)} · файлов: {len(torrent.files)} · "
                f"{html.escape(torrent.stat_string)}"
            )
        return "\n".join(lines)

    @staticmethod
    def format_torrserver_added(result) -> str:
        """Answer after an add. `result` is services.torrserver_service.AddResult
        (imported lazily by the caller to keep formatters free of service deps).
        """
        torrent = result.torrent
        lines = [f"✅ <b>{html.escape(torrent.title)}</b>", ""]

        if not result.metadata_ready:
            lines.append("Раздача добавлена, файлы ещё подтягиваются.")
            lines.append(_SCHEDULED_FALLBACK)
            return "\n".join(lines)

        videos = torrent.video_files
        lines.append(f"Файлов: {len(torrent.files)} · видео: {len(videos)}")
        for file in videos[:10]:
            name = file.path.rsplit("/", 1)[-1]
            lines.append(f"  🎞 {html.escape(name)} — {format_bytes(file.length)}")
        if len(videos) > 10:
            lines.append(f"  … и ещё {len(videos) - 10}")

        if result.stream_url:
            lines.append("")
            lines.append(f"▶️ <a href=\"{html.escape(result.stream_url)}\">Открыть поток</a>")

        lines.append("")
        status = getattr(result.sync, "status", None)
        if status == "ok":
            lines.append("📺 Опубликовано в Emby — ищи в «Торренты (фильмы/сериалы)».")
        elif status == "already_running":
            lines.append("📺 Синхронизация с Emby уже идёт — раздача попадёт в неё.")
        else:
            reason = getattr(result.sync, "error", None)
            if reason:
                lines.append(f"⚠️ Синхронизация не запустилась: {html.escape(reason)}.")
            else:
                lines.append("⚠️ Хук синхронизации не настроен.")
            lines.append(_SCHEDULED_FALLBACK)

        return "\n".join(lines)
```

`bot/ui/formatters/__init__.py` — импортировать `_TorrServerFormatters` и добавить в базы `Formatters`.

- [ ] **Step 6: Запустить тест и убедиться, что проходит**

Run: `python -m pytest tests/test_torrserver_ui.py -q`
Expected: PASS.

- [ ] **Step 7: Коммит**

```bash
git add bot/ui tests/test_torrserver_ui.py
git commit -m "feat: menu button, keyboards and formatters for the TorrServer section"
```

---

### Task 9: Хендлеры — панель, статус, регистрация роутера

**Files:**
- Create: `bot/handlers/torrserver.py`
- Modify: `bot/handlers/__init__.py`
- Test: `tests/test_torrserver_handlers.py`

**Interfaces:**
- Consumes: `registry.get_torrserver()`, `Keyboards.torrserver_panel()`, `Formatters.format_torrserver_status()`.
- Produces: `bot.handlers.torrserver.router`, `bot.handlers.torrserver.render_panel() -> tuple[str, InlineKeyboardMarkup | None]`, `bot.handlers.torrserver._results: dict[int, list[TorrServerRelease]]` (кеш результатов на пользователя).

- [ ] **Step 1: Написать падающий тест**

```python
"""Хендлеры раздела TorrServer: панель и её деградация."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.handlers import torrserver as ts_handlers
from bot.models import TorrServerStats


@pytest.mark.asyncio
async def test_panel_says_how_to_configure_when_disabled():
    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock, return_value=None):
        text, keyboard = await ts_handlers.render_panel()

    assert "TORRSERVER_URL" in text
    assert keyboard is None


@pytest.mark.asyncio
async def test_panel_renders_stats():
    client = MagicMock()
    client.get_stats = AsyncMock(return_value=TorrServerStats(
        version="MatriX.142.2", torrent_count=6, total_size=1024, cache_size=1024,
        use_disk=False, source_count=6,
    ))
    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock, return_value=client):
        text, keyboard = await ts_handlers.render_panel()

    assert "MatriX.142.2" in text
    assert keyboard is not None


@pytest.mark.asyncio
async def test_panel_survives_a_dead_server():
    client = MagicMock()
    client.get_stats = AsyncMock(side_effect=ts_handlers.TorrServerError("недоступен"))
    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock, return_value=client):
        text, keyboard = await ts_handlers.render_panel()

    assert "недоступен" in text
    assert keyboard is not None  # кнопка «Обновить» должна остаться


def test_router_is_registered_before_search_router():
    """handle_text_search ловит любой текст, а aiogram не каскадирует
    обработчики после совпадения — наш роутер обязан идти раньше."""
    from bot.handlers import setup_routers

    router = setup_routers()
    names = [r.name for r in router.sub_routers]
    assert names.index("torrserver") < names.index("search")
```

- [ ] **Step 2: Запустить тест и убедиться, что падает**

Run: `python -m pytest tests/test_torrserver_handlers.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'bot.handlers.torrserver'`.

- [ ] **Step 3: Создать `bot/handlers/torrserver.py` (панель)**

```python
"""TorrServer section — the "watch it now" contour.

Kept apart from the Scryer search flow on purpose: Scryer answers "I want to
own this", TorrServer answers "I want to watch this tonight". They share no
state.
"""

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot.clients.registry import get_torrserver, get_torrserver_service
from bot.clients.torrserver import TorrServerError
from bot.handlers.common import accessible_message, safe_edit
from bot.models import TorrServerRelease
from bot.ui.formatters import Formatters
from bot.ui.keyboards import CallbackData, Keyboards
from bot.ui.menu import MENU_TORRSERVER

logger = structlog.get_logger()
router = Router(name="torrserver")

#: Per-user search hits. They are far too large for callback_data, so buttons
#: carry only an index into this list — the same trick the Soulseek flow uses.
_results: dict[int, list[TorrServerRelease]] = {}

#: Cap on remembered result sets, evicting the oldest — never clear() the whole
#: dict, that would wipe every other user's in-flight selection.
_MAX_CACHED_USERS = 50

_NOT_CONFIGURED = (
    "❌ TorrServer не настроен. Добавьте <code>TORRSERVER_URL</code>, "
    "<code>TORRSERVER_USERNAME</code> и <code>TORRSERVER_PASSWORD</code> в конфигурацию."
)


async def render_panel() -> tuple[str, InlineKeyboardMarkup | None]:
    """Build the panel card without touching Telegram, so both the command and
    the refresh button can decide themselves how to deliver it."""
    client = await get_torrserver()
    if not client:
        return _NOT_CONFIGURED, None

    try:
        stats = await client.get_stats()
    except TorrServerError as e:
        return Formatters.format_error(str(e)), Keyboards.torrserver_panel()
    except Exception as e:
        logger.error("torrserver_panel_failed", error=str(e), exc_info=True)
        return Formatters.format_error("Не удалось получить статус TorrServer"), Keyboards.torrserver_panel()

    return Formatters.format_torrserver_status(stats), Keyboards.torrserver_panel()


@router.message(F.text == MENU_TORRSERVER)
@router.message(Command("ts"))
async def cmd_torrserver(message: Message) -> None:
    """Open the TorrServer panel."""
    text, keyboard = await render_panel()
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == CallbackData.TS_REFRESH)
@router.callback_query(F.data == CallbackData.TS_BACK)
async def handle_panel_refresh(callback: CallbackQuery) -> None:
    """Re-render the panel in place (exactly one callback.answer())."""
    text, keyboard = await render_panel()
    if (message := accessible_message(callback)) is not None:
        await safe_edit(message, text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == CallbackData.TS_CLOSE)
async def handle_close(callback: CallbackQuery) -> None:
    """Close the panel message."""
    if (message := accessible_message(callback)) is not None:
        await message.delete()
    await callback.answer()
```

- [ ] **Step 4: Зарегистрировать роутер в `bot/handlers/__init__.py`**

Импорт рядом с остальными:

```python
from bot.handlers.torrserver import router as torrserver_router
```

Включение — **сразу после `start_router`, до `search_router`**:

```python
    main_router.include_router(start_router)
    # Before search_router: handle_text_search claims any plain text, and
    # aiogram does not cascade handlers after a routed match.
    main_router.include_router(torrserver_router)
    main_router.include_router(search_router)
```

- [ ] **Step 5: Запустить тест и убедиться, что проходит**

Run: `python -m pytest tests/test_torrserver_handlers.py -q`
Expected: PASS. Если `test_router_is_registered_before_search_router` падает из-за отсутствия имён у существующих роутеров — задать им имена нельзя (чужой код), поэтому в этом случае заменить проверку на позиционную:

```python
    from bot.handlers import search as search_module
    from bot.handlers import torrserver as ts_module

    subs = setup_routers().sub_routers
    assert subs.index(ts_module.router) < subs.index(search_module.router)
```

- [ ] **Step 6: Коммит**

```bash
git add bot/handlers/torrserver.py bot/handlers/__init__.py tests/test_torrserver_handlers.py
git commit -m "feat: TorrServer panel handler wired before the search router"
```

---

### Task 10: Хендлеры — поиск через ForceReply, страницы, карточка, добавление

**Files:**
- Modify: `bot/handlers/torrserver.py`
- Modify: `tests/test_torrserver_handlers.py`

**Interfaces:**
- Consumes: `render_panel`, `_results` из Task 9; `TorrServerService.add_and_publish` из Task 7; клавиатуры/форматтеры из Task 8.
- Produces: хендлеры `handle_search_prompt`, `handle_search_reply`, `handle_page`, `handle_release`, `handle_add`; хелпер `_render_results(message, user_id, page)`.

- [ ] **Step 1: Написать падающий тест (дописать в тот же файл)**

```python
from aiogram.types import ForceReply

from bot.models import SyncHookResult, TorrServerFile, TorrServerRelease, TorrServerTorrent
from bot.services.torrserver_service import AddResult
from bot.ui.menu import TORRSERVER_PROMPT

HIT = TorrServerRelease(title="Dune 2021 BDRemux", size=1024, seeders=10,
                        link="http://p:9696/2/download?link=a", tracker="RuTracker.org")


def _message(text="Dune"):
    message = MagicMock()
    message.text = text
    message.answer = AsyncMock()
    message.from_user = MagicMock(id=7)
    return message


@pytest.mark.asyncio
async def test_search_prompt_uses_force_reply_with_the_exact_marker():
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.answer = AsyncMock()

    with patch.object(ts_handlers, "accessible_message", return_value=callback.message):
        await ts_handlers.handle_search_prompt(callback)

    text = callback.message.answer.await_args.args[0]
    markup = callback.message.answer.await_args.kwargs["reply_markup"]
    assert text == TORRSERVER_PROMPT
    assert isinstance(markup, ForceReply)


@pytest.mark.asyncio
async def test_reply_runs_a_search_and_caches_hits():
    client = MagicMock()
    client.search = AsyncMock(return_value=[HIT])
    status = MagicMock()
    status.edit_text = AsyncMock()
    message = _message("Dune 2021")
    message.answer = AsyncMock(return_value=status)

    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock, return_value=client):
        await ts_handlers.handle_search_reply(message)

    client.search.assert_awaited_once()
    assert ts_handlers._results[7][0].title == "Dune 2021 BDRemux"
    assert "Найдено" in status.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_reply_rejects_a_too_short_query():
    message = _message("a")
    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock) as client_getter:
        await ts_handlers.handle_search_reply(message)

    client_getter.assert_not_awaited()
    assert "коротк" in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_empty_search_result_is_reported():
    client = MagicMock()
    client.search = AsyncMock(return_value=[])
    status = MagicMock()
    status.edit_text = AsyncMock()
    message = _message("асдфасдф")
    message.answer = AsyncMock(return_value=status)

    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock, return_value=client):
        await ts_handlers.handle_search_reply(message)

    assert "не найдено" in status.edit_text.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_add_publishes_and_answers_with_the_stream_link():
    ts_handlers._results[7] = [HIT]
    torrent = TorrServerTorrent(hash="abc", title="Dune 2021", stat=3,
                                stat_string="Torrent working",
                                files=[TorrServerFile(id=1, path="Dune/Dune.mkv", length=10)])
    service = MagicMock()
    service.add_and_publish = AsyncMock(return_value=AddResult(
        torrent=torrent, metadata_ready=True,
        sync=SyncHookResult(status="ok", duration_s=1.0),
        stream_url="http://ts:8090/stream/Dune.mkv?link=abc&index=1&play",
    ))

    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.from_user = MagicMock(id=7)
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    from bot.ui.callbacks import TsAddCB

    with patch.object(ts_handlers, "get_torrserver_service", new_callable=AsyncMock,
                      return_value=service), \
         patch.object(ts_handlers, "accessible_message", return_value=callback.message):
        await ts_handlers.handle_add(callback, TsAddCB(idx=0))

    service.add_and_publish.assert_awaited_once_with(HIT.link, HIT.title, "")
    assert "Emby" in callback.message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_add_after_cache_expiry_asks_to_search_again():
    ts_handlers._results.pop(7, None)
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.from_user = MagicMock(id=7)
    callback.message = MagicMock()

    from bot.ui.callbacks import TsAddCB

    with patch.object(ts_handlers, "accessible_message", return_value=callback.message):
        await ts_handlers.handle_add(callback, TsAddCB(idx=0))

    assert callback.answer.await_args.kwargs.get("show_alert") is True
```

- [ ] **Step 2: Запустить тест и убедиться, что падает**

Run: `python -m pytest tests/test_torrserver_handlers.py -q -k "search or add"`
Expected: FAIL — `AttributeError: module 'bot.handlers.torrserver' has no attribute 'handle_search_prompt'`.

- [ ] **Step 3: Дописать поиск и добавление в `bot/handlers/torrserver.py`**

Импорты дополнить: `html`, `from aiogram.types import ForceReply`, `from bot.config import get_settings`, `from bot.handlers._cache import remember_lru`, `from bot.ui.callbacks import TsAddCB, TsPageCB, TsReleaseCB`, `from bot.ui.menu import TORRSERVER_PROMPT`.

```python
MAX_QUERY_LENGTH = 100
#: One torznab answer is ~470 KB; nobody scrolls past the top hits anyway.
SEARCH_LIMIT = 30


@router.callback_query(F.data == CallbackData.TS_SEARCH)
async def handle_search_prompt(callback: CallbackQuery) -> None:
    """Ask for a query with ForceReply.

    No state is stored: the reply carries the prompt with it, so a user who
    changes their mind and types something else simply gets the normal Scryer
    search instead of a stale "waiting for TorrServer query" flag.
    """
    if (message := accessible_message(callback)) is not None:
        await message.answer(
            TORRSERVER_PROMPT,
            reply_markup=ForceReply(input_field_placeholder="Название раздачи"),
        )
    await callback.answer()


async def _render_results(message: Message, user_id: int, page: int) -> None:
    """Render one page of cached hits into `message` (edit in place)."""
    releases = _results.get(user_id) or []
    per_page = get_settings().results_per_page
    total_pages = max(1, (len(releases) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    chunk = releases[page * per_page:(page + 1) * per_page]

    await safe_edit(
        message,
        Formatters.format_torrserver_results(chunk, page, per_page, len(releases)),
        reply_markup=Keyboards.torrserver_results(
            chunk, page, total_pages, offset=page * per_page,
        ),
        parse_mode="HTML",
    )


@router.message(F.reply_to_message.text == TORRSERVER_PROMPT)
async def handle_search_reply(message: Message) -> None:
    """Search TorrServer for the text the user replied with."""
    query = (message.text or "").strip()
    if len(query) < 2:
        await message.answer("❌ Запрос слишком короткий (мин. 2 символа)")
        return
    if len(query) > MAX_QUERY_LENGTH:
        await message.answer(f"❌ Запрос слишком длинный (макс. {MAX_QUERY_LENGTH} символов)")
        return

    client = await get_torrserver()
    if not client:
        await message.answer(_NOT_CONFIGURED, parse_mode="HTML")
        return

    status_msg = await message.answer("🔎 Ищу раздачи в TorrServer...")
    try:
        releases = await client.search(query, limit=SEARCH_LIMIT)
    except TorrServerError as e:
        await status_msg.edit_text(Formatters.format_error(html.escape(str(e))))
        return
    except Exception as e:
        logger.error("torrserver_search_failed", error=str(e), exc_info=True)
        await status_msg.edit_text(Formatters.format_error("Поиск временно недоступен"))
        return

    logger.info("torrserver_search", query=query, results=len(releases))

    if not releases:
        await status_msg.edit_text(
            Formatters.format_warning(f"Ничего не найдено для <b>{html.escape(query)}</b>"),
            reply_markup=Keyboards.torrserver_panel(),
            parse_mode="HTML",
        )
        return

    user_id = message.from_user.id
    remember_lru(_results, user_id, releases, _MAX_CACHED_USERS)
    await _render_results(status_msg, user_id, 0)


@router.callback_query(TsPageCB.filter())
async def handle_page(callback: CallbackQuery, callback_data: TsPageCB) -> None:
    """Flip between result pages."""
    message = accessible_message(callback)
    if message is None:
        return
    if not _results.get(callback.from_user.id):
        await callback.answer("Результаты устарели — повторите поиск", show_alert=True)
        return
    await _render_results(message, callback.from_user.id, callback_data.page)
    await callback.answer()


@router.callback_query(TsReleaseCB.filter())
async def handle_release(callback: CallbackQuery, callback_data: TsReleaseCB) -> None:
    """Open the card of one hit."""
    message = accessible_message(callback)
    if message is None:
        return
    releases = _results.get(callback.from_user.id) or []
    per_page = get_settings().results_per_page
    # The button index is relative to its page, but the card is rendered from
    # the absolute position in the cached list.
    absolute = callback_data.idx
    if absolute >= len(releases):
        await callback.answer("Результаты устарели — повторите поиск", show_alert=True)
        return

    release = releases[absolute]
    await safe_edit(
        message,
        Formatters.format_torrserver_release(release),
        reply_markup=Keyboards.torrserver_release(absolute),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(TsAddCB.filter())
async def handle_add(callback: CallbackQuery, callback_data: TsAddCB) -> None:
    """Add the chosen release and publish it to Emby."""
    message = accessible_message(callback)
    if message is None:
        return

    releases = _results.get(callback.from_user.id) or []
    if callback_data.idx >= len(releases):
        await callback.answer("Результаты устарели — повторите поиск", show_alert=True)
        return
    release = releases[callback_data.idx]

    service = await get_torrserver_service()
    if service is None:
        await callback.answer("TorrServer не настроен", show_alert=True)
        return

    await callback.answer("Добавляю...")
    await message.edit_text("⏳ Добавляю раздачу и жду метаданные...")

    try:
        result = await service.add_and_publish(release.link, release.title, "")
    except TorrServerError as e:
        await message.edit_text(Formatters.format_error(html.escape(str(e))))
        return
    except Exception as e:
        logger.error("torrserver_add_failed", error=str(e), exc_info=True)
        await message.edit_text(Formatters.format_error("Не удалось добавить раздачу"))
        return

    await message.edit_text(
        Formatters.format_torrserver_added(result),
        reply_markup=Keyboards.torrserver_panel(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
```

Клавиатура из Task 8 уже принимает `offset` — в `_render_results` он обязателен, иначе вторая страница откроет не тот релиз.

- [ ] **Step 4: Запустить тест и убедиться, что проходит**

Run: `python -m pytest tests/test_torrserver_handlers.py -q`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add bot/handlers/torrserver.py bot/ui/keyboards/torrserver.py tests/test_torrserver_handlers.py
git commit -m "feat: TorrServer search, result pages and one-tap add"
```

---

### Task 11: Хендлеры — список раздач и удаление

**Files:**
- Modify: `bot/handlers/torrserver.py`
- Modify: `tests/test_torrserver_handlers.py`

**Interfaces:**
- Consumes: `TsTorrentCB`, `Keyboards.torrserver_list`, `Keyboards.torrserver_confirm_delete`, `Formatters.format_torrserver_torrents`.
- Produces: хендлеры `handle_list`, `handle_delete_prompt`, `handle_delete_confirm`.

- [ ] **Step 1: Написать падающий тест (дописать в тот же файл)**

```python
@pytest.mark.asyncio
async def test_list_shows_torrents():
    client = MagicMock()
    client.list_torrents = AsyncMock(return_value=[TorrServerTorrent(
        hash="abc", title="Dune 2021", size=1024, stat=5, stat_string="Torrent in db")])
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock, return_value=client), \
         patch.object(ts_handlers, "accessible_message", return_value=callback.message):
        await ts_handlers.handle_list(callback, is_admin=True)

    assert "Dune 2021" in callback.message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_delete_is_refused_for_non_admins():
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.message = MagicMock()

    from bot.ui.callbacks import TsTorrentCB

    await ts_handlers.handle_delete_confirm(
        callback, TsTorrentCB(action="delconf", h="abc"), is_admin=False)

    assert callback.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_delete_removes_and_warns_about_the_delayed_strm_cleanup():
    client = MagicMock()
    client.remove_torrent = AsyncMock()
    client.list_torrents = AsyncMock(return_value=[])
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    from bot.ui.callbacks import TsTorrentCB

    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock, return_value=client), \
         patch.object(ts_handlers, "accessible_message", return_value=callback.message):
        await ts_handlers.handle_delete_confirm(
            callback, TsTorrentCB(action="delconf", h="abc"), is_admin=True)

    client.remove_torrent.assert_awaited_once_with("abc")
    assert "10 минут" in callback.message.edit_text.await_args.args[0]
```

- [ ] **Step 2: Запустить тест и убедиться, что падает**

Run: `python -m pytest tests/test_torrserver_handlers.py -q -k "list or delete"`
Expected: FAIL — `AttributeError: ... has no attribute 'handle_list'`.

- [ ] **Step 3: Дописать список и удаление**

Импорт дополнить `TsTorrentCB`.

```python
@router.callback_query(F.data == CallbackData.TS_LIST)
async def handle_list(callback: CallbackQuery, is_admin: bool = False) -> None:
    """Show what is currently on the server."""
    message = accessible_message(callback)
    if message is None:
        return
    client = await get_torrserver()
    if not client:
        await callback.answer("TorrServer не настроен", show_alert=True)
        return

    try:
        torrents = await client.list_torrents()
    except TorrServerError as e:
        await callback.answer(str(e)[:180], show_alert=True)
        return

    await safe_edit(
        message,
        Formatters.format_torrserver_torrents(torrents),
        reply_markup=Keyboards.torrserver_list(torrents, is_admin),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(TsTorrentCB.filter(F.action == "del"))
async def handle_delete_prompt(
    callback: CallbackQuery, callback_data: TsTorrentCB, is_admin: bool = False
) -> None:
    """Ask before removing a torrent."""
    if not is_admin:
        await callback.answer("Недостаточно прав для удаления", show_alert=True)
        return
    message = accessible_message(callback)
    if message is None:
        return
    await safe_edit(
        message,
        "⚠️ <b>Удалить раздачу из TorrServer?</b>\n\n"
        "Поток перестанет открываться. Файлы <code>.strm</code> уберёт "
        "синхронизация в течение 10 минут.",
        reply_markup=Keyboards.torrserver_confirm_delete(callback_data.h),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(TsTorrentCB.filter(F.action == "delconf"))
async def handle_delete_confirm(
    callback: CallbackQuery, callback_data: TsTorrentCB, is_admin: bool = False
) -> None:
    """Remove the torrent."""
    if not is_admin:
        await callback.answer("Недостаточно прав для удаления", show_alert=True)
        return
    message = accessible_message(callback)
    if message is None:
        return
    client = await get_torrserver()
    if not client:
        await callback.answer("TorrServer не настроен", show_alert=True)
        return

    try:
        await client.remove_torrent(callback_data.h)
    except TorrServerError as e:
        await callback.answer(str(e)[:180], show_alert=True)
        return

    logger.info("torrserver_remove", torrent_hash=callback_data.h, user_id=callback.from_user.id)
    await callback.answer("Раздача удалена")

    try:
        torrents = await client.list_torrents()
    except TorrServerError:
        torrents = []

    await safe_edit(
        message,
        "🗑 <b>Раздача удалена.</b> Из Emby пропадёт в течение 10 минут.\n\n"
        + Formatters.format_torrserver_torrents(torrents),
        reply_markup=Keyboards.torrserver_list(torrents, is_admin),
        parse_mode="HTML",
    )
```

- [ ] **Step 4: Запустить тест и убедиться, что проходит**

Run: `python -m pytest tests/test_torrserver_handlers.py -q`
Expected: PASS.

- [ ] **Step 5: Прогнать всё и линт**

Run: `make test` и `make lint`
Expected: зелено и чисто.

- [ ] **Step 6: Коммит**

```bash
git add bot/handlers/torrserver.py tests/test_torrserver_handlers.py
git commit -m "feat: list and delete torrents from the TorrServer section"
```

---

### Task 12: TorrServer в /status и в списке команд

**Files:**
- Modify: `bot/handlers/status.py`
- Modify: `bot/ui/commands.py`
- Test: `tests/test_torrserver_status.py`

**Interfaces:**
- Consumes: `registry.get_torrserver()`, `TorrServerClient.check_connection()`.
- Produces: строка TorrServer в общей карточке `/status`; команда `/ts` в меню команд Telegram.

- [ ] **Step 1: Написать падающий тест**

```python
"""TorrServer должен попадать в общую карточку /status."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.handlers import status as status_handlers


@pytest.mark.asyncio
async def test_status_includes_torrserver_when_configured():
    client = MagicMock()
    client.check_connection = AsyncMock(return_value=(True, "MatriX.142.2", 12.0))

    with patch.object(status_handlers, "get_torrserver", new_callable=AsyncMock,
                      return_value=client):
        statuses = await status_handlers.gather_service_statuses()

    names = [s.service for s in statuses]
    assert "TorrServer" in names


@pytest.mark.asyncio
async def test_status_skips_torrserver_when_not_configured():
    with patch.object(status_handlers, "get_torrserver", new_callable=AsyncMock,
                      return_value=None):
        statuses = await status_handlers.gather_service_statuses()

    assert "TorrServer" not in [s.service for s in statuses]


def test_ts_command_is_advertised():
    from bot.ui.commands import get_bot_commands

    assert any(c.command == "ts" for c in get_bot_commands())
```

- [ ] **Step 2: Запустить тест и убедиться, что падает**

Run: `python -m pytest tests/test_torrserver_status.py -q`
Expected: FAIL — в `bot/handlers/status.py` нет ни `get_torrserver`, ни функции сбора статусов с таким именем.

- [ ] **Step 3: Встроить TorrServer в статус**

Прочитать `bot/handlers/status.py` целиком и найти место, где собираются `SystemStatus` по сервисам (Scryer/Lidarr/qBittorrent/Emby/...). Если сбор размазан по хендлеру — выделить его в функцию `gather_service_statuses() -> list[SystemStatus]` без побочных эффектов Telegram и вызвать её из хендлера (тот же приём, что `_render_status_text` в `bot/handlers/emby.py`). Добавить в импорт `get_torrserver`, а в сбор — ветку:

```python
    torrserver = await get_torrserver()
    if torrserver:
        available, version, elapsed = await torrserver.check_connection()
        statuses.append(SystemStatus(
            service="TorrServer", available=available, version=version, response_time=elapsed,
        ))
```

Имена полей `SystemStatus` взять из `bot/models.py` — если поле времени называется иначе, использовать существующее имя, а не вводить новое.

В `bot/ui/commands.py` добавить команду в список:

```python
        BotCommand(command="ts", description="▶️ TorrServer — смотреть сейчас"),
```

- [ ] **Step 4: Запустить тест и убедиться, что проходит**

Run: `python -m pytest tests/test_torrserver_status.py tests/test_command_catalog.py -q`
Expected: PASS. `test_command_catalog.py` существует и проверяет каталог команд — если он требует синхронизации со списком хендлеров, обновить и его ожидания.

- [ ] **Step 5: Коммит**

```bash
git add bot/handlers/status.py bot/ui/commands.py tests/test_torrserver_status.py
git commit -m "feat: report TorrServer health in /status and advertise /ts"
```

---

### Task 13: Хук синхронизации на Homeserver (слой 3 — инфра-скрипт)

**Files:**
- Create: `C:\Tools\TorrServer\Emby-Sync-Hook.py` (на Homeserver, 192.168.31.95)
- Create: `C:\Tools\TorrServer\Install-EmbySyncHook.ps1` (там же)
- Modify: `docs/superpowers/specs/2026-08-05-torrserver-integration-design.md` (раздел «Готовность» — отметить факт установки)

**Interfaces:**
- Consumes: существующий `C:\Tools\TorrServer\Sync-TorrServerToEmby.py --apply`.
- Produces: HTTP-служба `TorrServerEmbySyncHook` на `0.0.0.0:8099`: `POST /sync` с заголовком `X-Token` → `200 {"status":"ok","duration_s":N}` / `202 {"status":"already_running"}` / `403`; `GET /health` → `200 {"status":"ok"}` без токена.

**Слой 3 — TDD неприменим.** Гарантии здесь другие: идемпотентность установщика и проверка постусловия. Скрипты пишутся сразу, без падающего теста.

- [ ] **Step 1: Зафиксировать состояние ДО**

Run (на Homeserver): `schtasks /query /tn TorrServer-EmbySync /fo LIST` и `sc query TorrServerEmbySyncHook`
Expected: задача существует и Ready; службы `TorrServerEmbySyncHook` ещё нет (`FAILED 1060`). Записать вывод.

- [ ] **Step 2: Написать `C:\Tools\TorrServer\Emby-Sync-Hook.py`**

```python
"""HTTP-хук принудительного синка TorrServer → Emby.

Бот живёт в контейнере на RPi4, где нет ни ssh, ни curl, поэтому запуск
`Sync-TorrServerToEmby.py --apply` доступен ему только по HTTP.

Служба намеренно крошечная: один рабочий поток синка, один токен, никаких
зависимостей вне стандартной библиотеки.
"""

import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(r"C:\Tools\TorrServer")
SYNC_SCRIPT = ROOT / "Sync-TorrServerToEmby.py"
TOKEN_FILE = ROOT / "secrets" / "hook-token.txt"
LOG = ROOT / "logs" / "emby-sync-hook.log"
PORT = 8099

_lock = threading.Lock()
_running = False


def log(message):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def read_token():
    return TOKEN_FILE.read_text(encoding="utf-8").strip()


def run_sync():
    """Запустить синк. Возвращает (код возврата, длительность в секундах)."""
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(SYNC_SCRIPT), "--apply"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    duration = round(time.monotonic() - started, 2)
    tail = (result.stdout or "").strip().splitlines()[-3:]
    log(f"sync rc={result.returncode} duration={duration}s tail={' | '.join(tail)}")
    return result.returncode, duration


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            self._reply(200, {"status": "ok"})
            return
        self._reply(404, {"status": "not_found"})

    def do_POST(self):
        global _running

        if self.path.rstrip("/") != "/sync":
            self._reply(404, {"status": "not_found"})
            return

        if self.headers.get("X-Token", "") != read_token():
            log(f"rejected request from {self.client_address[0]}: bad token")
            self._reply(403, {"status": "forbidden"})
            return

        # Параллельный запуск плодил бы процессы синка поверх штатной задачи —
        # второй запрос просто присоединяется к идущему проходу.
        with _lock:
            if _running:
                self._reply(202, {"status": "already_running"})
                return
            _running = True

        try:
            code, duration = run_sync()
        except Exception as exc:  # noqa: BLE001 — служба не должна падать целиком
            log(f"sync crashed: {exc}")
            self._reply(500, {"status": "failed", "error": str(exc)})
            return
        finally:
            with _lock:
                _running = False

        if code != 0:
            self._reply(500, {"status": "failed", "error": f"rc={code}"})
            return
        self._reply(200, {"status": "ok", "duration_s": duration})

    def log_message(self, fmt, *args):
        """Заглушить встроенный stderr-лог: свой лог пишется через log()."""


def main():
    LOG.parent.mkdir(parents=True, exist_ok=True)
    read_token()  # падаем сразу, если токена нет — иначе служба «работает», но 403 на всё
    log(f"hook listening on 0.0.0.0:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Написать идемпотентный установщик `Install-EmbySyncHook.ps1`**

```powershell
<#  Устанавливает службу TorrServerEmbySyncHook рядом с TorrServer.
    Идемпотентен: повторный запуск не пересоздаёт токен и не ломает службу. #>
[CmdletBinding()]
param(
    [string]$Root = 'C:\Tools\TorrServer',
    [string]$ServiceName = 'TorrServerEmbySyncHook',
    [string]$Nssm = 'C:\Tools\decluttarr\nssm.exe'
)

$ErrorActionPreference = 'Stop'

$tokenFile = Join-Path $Root 'secrets\hook-token.txt'
$script    = Join-Path $Root 'Emby-Sync-Hook.py'
$logDir    = Join-Path $Root 'logs'
$python    = (Get-Command python).Source

New-Item -ItemType Directory -Force -Path (Split-Path $tokenFile), $logDir | Out-Null

if (-not (Test-Path $tokenFile)) {
    $bytes = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    [Convert]::ToBase64String($bytes).TrimEnd('=') | Out-File $tokenFile -Encoding ascii -NoNewline
    Write-Host "Token created: $tokenFile"
} else {
    Write-Host "Token already present, kept as is"
}

$existing = & $Nssm status $ServiceName 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "Service exists, reinstalling parameters"
    & $Nssm stop $ServiceName | Out-Null
} else {
    & $Nssm install $ServiceName $python $script | Out-Null
}

& $Nssm set $ServiceName AppDirectory $Root | Out-Null
& $Nssm set $ServiceName AppStdout (Join-Path $logDir 'emby-sync-hook.out.log') | Out-Null
& $Nssm set $ServiceName AppStderr (Join-Path $logDir 'emby-sync-hook.err.log') | Out-Null
& $Nssm set $ServiceName Start SERVICE_AUTO_START | Out-Null
& $Nssm set $ServiceName AppRestartDelay 10000 | Out-Null
& $Nssm start $ServiceName | Out-Null

New-NetFirewallRule -DisplayName 'TorrServer Emby Sync Hook' -Direction Inbound `
    -Protocol TCP -LocalPort 8099 -Action Allow -ErrorAction SilentlyContinue | Out-Null

Start-Sleep -Seconds 2
$health = Invoke-RestMethod -Uri 'http://127.0.0.1:8099/health' -TimeoutSec 5
if ($health.status -ne 'ok') { throw "Health check failed: $($health | ConvertTo-Json -Compress)" }
Write-Host "Postcondition OK: service running, /health answered"
Write-Host "Token for the bot: $(Get-Content $tokenFile -Raw)"
```

- [ ] **Step 4: Установить и проверить постусловие**

Run (на Homeserver, от админа): `powershell -ExecutionPolicy Bypass -File C:\Tools\TorrServer\Install-EmbySyncHook.ps1`
Expected: `Postcondition OK`, служба в `SERVICE_RUNNING`, напечатан токен.

- [ ] **Step 5: Проверить контракт хука вживую**

Run: `curl -s -o NUL -w "%{http_code}" -X POST -H "X-Token: bad" http://127.0.0.1:8099/sync`
Expected: `403`.

Run: `curl -s -X POST -H "X-Token: <токен>" http://127.0.0.1:8099/sync`
Expected: `{"status":"ok","duration_s":N}`, а в `C:\Tools\TorrServer\logs\emby-sync.log` — свежая строка `Emby refresh -> HTTP`.

Run (повторный запуск установщика): `powershell -ExecutionPolicy Bypass -File C:\Tools\TorrServer\Install-EmbySyncHook.ps1`
Expected: `Token already present, kept as is`, снова `Postcondition OK` — то есть идемпотентно.

- [ ] **Step 6: Проверить доступность хука с RPi4**

Run (на rpie4): `curl -s -m 5 http://192.168.31.95:8099/health`
Expected: `{"status": "ok"}`. Если пусто — правило файрвола не применилось, разобраться до перехода к следующей задаче.

---

### Task 14: Деплой и сквозная проверка

**Files:**
- Modify: `.env` на rpie4 (в клоне репозитория) — переменные из Task 1
- Modify: `docs/superpowers/specs/2026-08-05-torrserver-integration-design.md` (отметка о выполненной проверке)

**Interfaces:**
- Consumes: всё предыдущее.
- Produces: работающий раздел в проде.

- [ ] **Step 1: Убедиться, что гейт качества зелёный**

Run: `make test` и `make lint`
Expected: тесты зелёные, ruff чист. Вывод показать целиком — «посмотрел код» доказательством не является.

- [ ] **Step 2: Запушить ветку и задеплоить**

```bash
git push
```

Затем на rpie4 в клоне репозитория: `git pull && make deploy` (цель тегает предыдущий образ как `:prev`, откат — `make rollback`).

- [ ] **Step 3: Прописать переменные окружения**

В `.env` на rpie4 добавить `TORRSERVER_URL`, `TORRSERVER_USERNAME`, `TORRSERVER_PASSWORD` (пароль — из `C:\Tools\TorrServer\secrets\admin-password.txt`), `EMBY_SYNC_HOOK_URL=http://192.168.31.95:8099`, `EMBY_SYNC_HOOK_TOKEN` (из `secrets\hook-token.txt`), затем перезапустить контейнер.

- [ ] **Step 4: Проверить, что бот увидел TorrServer**

Run: `docker logs tg-arr-bot --tail 50`
Expected: нет предупреждений о полу-настроенной интеграции; `/status` в боте показывает строку TorrServer с версией `MatriX.142.2`.

- [ ] **Step 5: Сквозной сценарий в живом боте**

1. Нажать «▶️ Смотреть» → появилась карточка со статусом.
2. «🔎 Найти» → ответить на ForceReply запросом, у которого точно есть раздачи.
3. Выбрать раздачу → «▶️ Смотреть».
4. Дождаться ответа: должен прийти список видеофайлов, ссылка на поток и строка про Emby.

Проверить факты, а не только текст бота:

Run (Homeserver): `powershell -c "(irm http://127.0.0.1:8090/torrents -Method Post -Headers @{Authorization=...} -Body '{\"action\":\"list\"}' -ContentType 'application/json' | % title)"`
Expected: добавленная раздача в списке.

Run (Homeserver): `dir /s /b G:\torrstream\*.strm | findstr /i "<название>"`
Expected: `.strm` появился.

Ожидаемо в Emby: элемент виден в библиотеке «Торренты (фильмы)» или «Торренты (сериалы)».

- [ ] **Step 6: Проверить удаление**

В боте: «📋 Раздачи» → 🗑 у пробной раздачи → подтвердить.
Expected: бот отвечает, что удалено; `action: list` на сервере больше её не возвращает.

- [ ] **Step 7: Записать результат проверки в спеку и закоммитить**

В раздел «Готовность» спеки дописать дату и что именно прошло. Коммит:

```bash
git add docs/superpowers/specs/2026-08-05-torrserver-integration-design.md
git commit -m "docs: record the live end-to-end verification of the TorrServer section"
```

---

## Self-Review

**Покрытие спеки:**

| Требование спеки | Задача |
| --- | --- |
| Клиент TorrServer (поиск/список/get/add/rem/настройки/health) | 3, 4, 5 |
| Клиент хука | 6 |
| Сервис add → ждать метаданные → синк | 7 |
| Кнопка меню, клавиатуры, форматтеры | 8 |
| Панель и роутер до search_router | 9 |
| Поиск через ForceReply, страницы, карточка, добавление | 10 |
| Список раздач и удаление (только админ) | 11 |
| Карточка статуса и `/status` | 8, 9, 12 |
| Ссылка на поток после добавления | 5, 7, 8 |
| Санитизация названия до добавления | 2, 5 |
| Гоча query-string в поиске | 4 (отдельный тест-страж) |
| Конфигурация и предупреждения | 1 |
| Хук на Homeserver + идемпотентный установщик | 13 |
| Готовность: тесты, линт, живой сквозной сценарий | 14 |

**Плейсхолдеры:** не осталось — каждый шаг несёт исполнимый код или конкретную команду с ожидаемым результатом. Единственные два места с условной формулировкой (имя функции сбора статусов в Task 12 и имена роутеров в Task 9) снабжены явной инструкцией, что делать в каждом из вариантов.

**Согласованность типов:** `TorrServerRelease.link` → `TorrServerService.add_and_publish(link, title, poster)` → `TorrServerClient.add_torrent(link, title, poster)`; `AddResult.stream_url` строится через `TorrServerClient.stream_url(hash, file_id, file_name)`; `SyncHookResult.status` принимает те же три значения в клиенте хука (Task 6), сервисе (Task 7) и форматтере (Task 8). Индексы кнопок результатов абсолютные во всех трёх местах (клавиатура, `_render_results`, `handle_release`/`handle_add`) — поправка зафиксирована в Task 10 Step 3.
