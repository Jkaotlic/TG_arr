# План фиксов TG_arr — 2026-04-18

## Phase 1: Quick wins (безопасные, локальные)

- [x] **BUG-01**: `log = logger.bind(...)` вынесен перед `try:` в `process_search` (search.py:138-141)
- [x] **BUG-05**: `detect_content_type` теперь проверяет `isinstance(movies_result, list)` вместо отрицания Exception (search_service.py)
- [x] **BUG-17**: `.replace(CallbackData.X, "")` → `.removeprefix(CallbackData.X)` во всех handlers (search, settings, trending, music, downloads)
- [x] **SEC-04**: Маскирование query-string `apikey/token/passkey` в логах через `_mask_url` (add_service.py)
- [x] **SEC-07**: `html.escape(str(e.message))` в emby.py ошибочных сообщениях
- [x] **DEAD-01**: Удалён BUGFIX_REPORT.md
- [x] **DEAD-02**: Удалены IMPROVEMENTS.md, docs/FEATURE_QBITTORRENT.md, docs/QUALITY_REPORT.md
- [x] **DEAD-03**: Удалён .coverage, добавлен `.coverage` и `analysis/` в .dockerignore
- [x] **DEAD-16**: Удалены неиспользуемые `CallbackData.MOVIE` и `CallbackData.SERIES`
- [x] **DEAD-17**: `F.data.startswith("speed:")` → `F.data.startswith(CallbackData.SPEED_LIMIT)` в downloads.py
- [x] **DEAD-25**: Удалён `version: "3.9"` из docker-compose.override.yml
- [x] **DEP-04**: `orjson` удалён из pyproject.toml и requirements.txt

## Phase 2: Functional fixes

- [x] **BUG-04**: Retryable codes расширены до 429/500/502/503/504 (base.py)
- [x] **BUG-19**: Tenacity уменьшен до `stop_after_attempt(2)` + `wait_exponential(min=1, max=5)` — укладываемся в ~15s Telegram callback окно
- [x] **BUG-23**: `get_settings.cache_clear()` в autouse-фикстуре (tests/conftest.py)
- [x] **SEC-01/11**: `_validate_download_url` стал async, использует `asyncio.to_thread(socket.getaddrinfo)` и проверяет ВСЕ A/AAAA записи (отклоняет хост с любым приватным адресом)
- [x] **LOGIC-07**: Lazy-инициализация Settings в `BaseAPIClient` (устранили side-effect at import)
- [x] **DEP-01/03**: pyproject.toml использует диапазоны (>=3.20,<4), requirements.txt содержит только prod deps, dev-зависимости в новом `requirements-dev.txt`

## Phase 3: Refactoring — ОТЛОЖЕНО

Намеренно не сделано из-за большого scope (отдельный PR):

- LOGIC-01: ArrBaseClient для Radarr/Sonarr (~500 строк дублирования)
- LOGIC-02/03: разбиение god-files (search.py 726, formatters.py 890, keyboards.py 803)
- LOGIC-04/05: единая логика grab_release
- LOGIC-11: SearchFlowService

## Lidarr integration (новое)

- [x] Config: `lidarr_url`, `lidarr_api_key`, `deezer_enabled`, `lidarr_enabled` property
- [x] Models: `ContentType.MUSIC`, `ArtistInfo`, `AlbumInfo`, `MetadataProfile`, `UserPreferences.lidarr_*`
- [x] Clients: `LidarrClient` (API v1), `DeezerClient` (public API, без ключа)
- [x] Registry: `get_lidarr()`, `get_deezer()`, `close_all()`
- [x] Prowlarr: `MUSIC_CATEGORIES` (3000-3060), music detection в `_normalize_result`
- [x] SearchService: `lookup_artist`, `lookup_album`, `detect_content_type` возвращает MUSIC
- [x] AddService: `add_artist`, `grab_music_release`, `get_lidarr_profiles/metadata_profiles/root_folders`
- [x] Handlers: `bot/handlers/music.py` (/music, выбор артиста, confirm add, trending music)
- [x] Интеграция в `calendar.py` / `status.py` / `settings.py` / `trending.py` / `search.py:MENU_BUTTONS`
- [x] UI: `Keyboards.artist_list/artist_details/trending_artists/metadata_profiles`, `settings_menu(lidarr_enabled=True)`, `trending_menu(show_music=True)`, `content_type_selection(show_music=...)`
- [x] Formatters: `format_artist_info/format_album_info/format_trending_artists`, `format_calendar(albums=...)`
- [x] `.env.example` + welcome / help в `start.py`
- [x] README: секция "Музыка" + `/music` в таблице команд + переменные окружения

## Tests

Добавлен `tests/test_lidarr.py` (27 новых тестов, всего 247/247 pass):

- `TestLidarrClient`: `_parse_artist`, `_parse_album`, `lookup_artist`, `add_artist` payload, `check_connection` v1 endpoint
- `TestDeezerClient`: `_get_headers` без X-Api-Key, `get_trending_artists`, network-failure
- `TestProwlarrMusicDetection`: MUSIC по категориям 3000-3060
- `TestUrlMasking`: `_mask_url` маскирует apikey/token/passkey, magnet/empty не ломаются
- `TestDownloadUrlValidation`: async `_validate_download_url` для private IP / unknown scheme / DNS-rebinding
- `TestSearchServiceMusicDetection`: `detect_content_type` возвращает MUSIC при совпадении имени артиста
- `TestAddServiceMusic`: `add_artist` без Lidarr, existing, `grab_music_release` без Lidarr

## Verification

- [x] Все Python-файлы компилируются (`py_compile`)
- [x] Все phase 1+2 items отмечены [x]
- [x] `pytest` проходит: 247/247 (было 220/220 до добавления музыки)
- [x] Нет сломанных импортов
- [x] README обновлён (музыка + Lidarr/Deezer)
- [x] git diff: 20+ файлов изменено, новые: `bot/clients/lidarr.py`, `bot/clients/deezer.py`, `bot/handlers/music.py`, `tests/test_lidarr.py`, `requirements-dev.txt`
- [x] Удалённые файлы: BUGFIX_REPORT.md, IMPROVEMENTS.md, docs/FEATURE_QBITTORRENT.md, docs/QUALITY_REPORT.md, .coverage
