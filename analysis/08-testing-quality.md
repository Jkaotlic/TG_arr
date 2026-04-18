# Testing Quality Audit — TG_arr

Дата: 2026-04-18.

## Обзор покрытия по модулям (оценка grep-ом)

| Модуль | Тестовый файл | Покрытие (качественно) |
|--------|---------------|------------------------|
| `bot/clients/base.py` | `test_clients.py::TestBaseAPIClient` | слабое (только init/headers) |
| `bot/clients/prowlarr.py` | `test_clients.py`, `test_parsing.py` | хорошее (parsing, normalize) |
| `bot/clients/radarr.py` | `test_clients.py::TestRadarrClient` | базовое (parse_movie) |
| `bot/clients/sonarr.py` | `test_clients.py::TestSonarrClient` | базовое (parse_series, monitor) |
| `bot/clients/lidarr.py` | `test_lidarr.py` | хорошее (parse, lookup, add_artist payload) |
| `bot/clients/deezer.py` | `test_lidarr.py::TestDeezerClient` | базовое |
| `bot/clients/qbittorrent.py` | `test_qbittorrent.py` | хорошее (login, parse_torrent, states) |
| `bot/clients/emby.py` | **НЕТ** | 0% |
| `bot/clients/tmdb.py` | **НЕТ** | 0% |
| `bot/services/scoring.py` | `test_scoring.py`, `test_services.py` | отличное |
| `bot/services/search_service.py` | `test_services.py`, `test_lidarr.py` | хорошее |
| `bot/services/add_service.py` | `test_lidarr.py::TestAddServiceMusic` + url-validation | частичное (music only, grab_movie/series не покрыты) |
| `bot/services/notification_service.py` | `test_qbittorrent.py::TestNotificationService` | базовое (subscribe, force_check) |
| `bot/db.py` | `test_db.py` | хорошее |
| `bot/handlers/*` | **НЕТ** | **0%** |
| `bot/middleware/*` | **НЕТ** | **0%** |
| `bot/main.py` | **НЕТ** | 0% |
| `bot/ui/formatters.py` | частично в `test_qbittorrent.py` | ~20% |
| `bot/ui/keyboards.py` | частично в `test_qbittorrent.py` | ~15% |

## TEST-01 — Handlers 0% coverage (HIGH)

Ни один `bot/handlers/*.py` файл не имеет тестов. Все callback-flow, error-branches, interaction с БД — непокрыты. Регрессии возможны при любом изменении.

**Решение:** для приоритетных handler'ов — `process_search`, `_execute_grab`, `handle_confirm_grab` — написать интеграционные тесты с моком Bot/CallbackQuery через aiogram-test или aiogram-tests.

## TEST-02 — Middleware 0% coverage (HIGH)

`AuthMiddleware`, `LoggingMiddleware`, `RateLimitMiddleware` — логика критична (доступ, rate-limit) и не тестирована. Багу в `is_user_allowed` / `create_user` conflict-handling можно пропустить.

## TEST-03 — `main.py` 0% — OK, но on_startup/shutdown логика значима (MED)

`on_startup`, `on_shutdown` запускают cleanup, notification-service, subscribe_user. Смысл тестировать как интеграция (тяжело).

## TEST-04 — Flaky-риск в conftest (MED)

Файл: `tests/conftest.py:12-35`
`_default_env` через `monkeypatch.setenv` + `get_settings.cache_clear()` — правильно. Однако:
- Фикстура `autouse=True` → влияет на все тесты; хорошо для изоляции, но если тест хочет протестить **отсутствие** переменной, придётся `monkeypatch.delenv(...)` внутри.
- `DATABASE_PATH=":memory:"` — работает, но каждый тест получает свою БД (хорошо).

`conftest.py` также **не** сбрасывает `bot.clients.registry._prowlarr` и другие singleton'ы. Если один тест инициализирует singleton, другой увидит его. Для unit-тестов не критично, но потенциальная flakeness.

**Решение:** `autouse=True` fixture, которая делает `from bot.clients.registry import close_all; await close_all(); _prowlarr=None; ...` между тестами.

## TEST-05 — Mocking boundaries: клиенты мокают `.get()`, но API contract не проверяется (MED)

Пример: `test_lidarr.py::test_lookup_artist_http`
```python
with patch.object(lidarr, "get", new=AsyncMock(return_value=[...])):
```
Тест проверяет, что `.lookup_artist()` корректно парсит возврат из `.get()`. Но `.get()` — уровень выше httpx. **Тесты не проверяют**:
- что реальный Lidarr endpoint возвращает такую форму (contract)
- что URL-формирование `/api/v1/artist/lookup?term=test` корректно
- что header `X-Api-Key` проходит

**Решение:** использовать `respx` (для httpx) для mock на уровне HTTP — более реалистично.

## TEST-06 — Missing edge cases (MED)

Не покрыты:
- Music callback flow от start до confirm (только unit на добавление артиста)
- DB migrations / schema change (при добавлении field в UserPreferences старые строки — не тестировано, это BUG-35)
- Concurrent user actions (2 callback одновременно от одного user)
- Session corruption recovery (partial: есть `test_db.py::test_delete_session`, но не на corrupt JSON)
- `handle_text_search` при message без text (только ранний return)
- `TelegramBadRequest` recovery
- `push_release` rejected but grab_release succeeds
- `grab_release` при `release.indexer_id == 0` (manual grab)

## TEST-07 — `pytest-asyncio` loop scope (INFO, OK)

Файл: `pyproject.toml:34-37`
```
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
```
Function-scoped loop ok для изоляции. Корректно с pytest-asyncio 1.x.

## TEST-08 — `test_lidarr.py` новый код Lidarr/Deezer — покрыт только unit, callback-flow нет (HIGH)

Файл: `tests/test_lidarr.py`
Тестируется:
- Lidarr парсинг (artist/album)
- Lidarr add_artist payload
- Deezer trending
- URL masking + SSRF
- SearchService music detection
- AddService music grab (negative cases)

**НЕ тестируется:**
- `bot/handlers/music.py` целиком (0%)
- `process_music_search` → `_handle_confirm_music_add` flow
- Trending artist click from Deezer → artist details
- BUG-27 (music vs search CONFIRM_GRAB conflict)

## TEST-09 (НОВЫЙ) — `tests/__init__.py` пустой, но нужен ли? (INFO)

OK. Удобен для pytest collection.

## TEST-10 (НОВЫЙ) — Нет coverage CI-gate (MED)

`Makefile: test-cov` есть, но нет min-coverage threshold в pyproject или CI. 0% handler coverage не блокирует merge.

**Решение:** `--cov-fail-under=60` или подобное.

## TEST-11 (НОВЫЙ) — `test_qbittorrent.py::TestFormatters.test_format_torrent_action` не проверяет output строго (LOW)

Файл: `tests/test_qbittorrent.py:361-368`
```python
assert "Пауза" in result or "⏸" in result
```
`or` делает тест всегда-true при одном из совпадений. Не плохо, но loose.

## TEST-12 (НОВЫЙ) — Нет теста на `_validate_download_url` с `ipv6` literal (LOW)

Файл: `tests/test_lidarr.py::TestDownloadUrlValidation`
Покрыты IPv4 private literals, но не `::1`, `fe80::`, `fc00::/7`. `ipaddress.ip_address` отработает для v6 в `_is_internal_ip` (`is_link_local`, `is_private`), но тест отсутствует.

## TEST-13 (НОВЫЙ) — Нет теста на `BaseAPIClient._request` retry-логику (MED)

Файл: `bot/clients/base.py:107-182`
Тестируется только init/headers. Реальные retry через `RetryableAPIError` (429/500/502), `TimeoutException`, `ConnectError` — не проверены. Легко сломать при рефакторинге.

## TEST-14 (НОВЫЙ) — Нет теста на timezone формирования (MED)

Файл: `bot/ui/formatters.py::format_calendar` / `_format_date_header`
BUG-11 (timezone mismatch) не обнаружился, т.к. нет теста, который передал бы UTC date близко к полуночи в MSK.

## Итого

HIGH: TEST-01, TEST-02, TEST-08
MED: TEST-04, TEST-05, TEST-06, TEST-10, TEST-13, TEST-14
LOW/INFO: TEST-03, TEST-07, TEST-09, TEST-11, TEST-12

Приоритет:
1. Покрыть `AuthMiddleware` + `RateLimitMiddleware` (критический auth flow)
2. Интеграционный тест на `_execute_grab` для movie/series (главный flow)
3. Тест на BUG-27 (music vs search CONFIRM_GRAB)
4. `respx`-based tests для `BaseAPIClient._request` retry логики
