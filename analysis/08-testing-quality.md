# Testing Quality TG_arr v1.0 (раунд 3)

Дата: 2026-05-08. Pytest 9.0.2, pytest-asyncio 1.3.0 (asyncio_mode=auto), pytest-cov 7.1.0.
Собрано: **261 тест** (`pytest --collect-only -q`), 11 файлов в `tests/`.

> **Главный вывод**. Жалоба пользователя «бот плохо ищет, не различает контент» воспроизводится прямо из тестов: ни одна функция верхнего уровня search-flow не покрыта (`process_search`, `handle_text_search`, `cmd_search`/`cmd_movie`/`cmd_series`, `handle_type_selection`). Логика «фильм vs сериал vs музыка» проверяется только у `detect_content_type` на 4 happy-path кейсах + 2 кейса с сезоном из `test_services.py`. Это и есть основной источник регрессий, в т.ч. жалобы.
>
> Хороший охват — у *парсеров* (Prowlarr `_parse_quality`, scoring 22 теста), у DB (включая corrupt-prefs регрессию BUG-24) и у SSRF/URL-валидации (`test_lidarr.py::TestDownloadUrlValidation`, `test_add_service.py::test_push_release_rejects_*`). Регрессии BUG-15, BUG-27 (косвенно), BUG-32, BUG-23, BUG-11/12 закреплены явно. Респекты — но search-front до сих пор «голый».

## Статистика по файлам

| Файл | Тесты | Покрывает |
|---|---|---|
| `test_clients.py` | ~20 | base init/headers, Prowlarr `_parse_quality`/`_normalize_result`, Radarr/Sonarr `_parse_*`, season-monitor matrix |
| `test_parsing.py` | ~80 (parametrize) | Prowlarr quality/year/season/audio/HDR/codec/season-pack — самый плотный файл |
| `test_qbittorrent.py` | ~40 | qBit login/add/parse, формат-байты, NotificationService subscribe/force_check |
| `test_scoring.py` | 23 | scoring +/-, sort/get_best/filter_by_quality |
| `test_services.py` | 22 | `parse_query`, `detect_content_type` (только S01/S01E05), scoring edge-cases |
| `test_lidarr.py` | ~25 | Lidarr/Deezer parse, music-detection (1 happy + 1 без Lidarr), URL-mask, SSRF |
| `test_add_service.py` | 5 | BUG-32 monitor_type matrix (single ep / season pack), SEC-16 push_release SSRF |
| `test_handlers_downloads.py` | 4 | BUG-15 (callback.answer один раз) |
| `test_db.py` | ~16 | CRUD, sessions, action log, DB-01 миграции, BUG-24 corrupt prefs |
| `test_formatters.py` | 2 | BUG-11 timezone, BUG-12 truncation |
| `conftest.py` | — | autouse env + cache_clear, фикстуры sample_* |

## Критические пробелы (увеличивают риск регрессий)

### TEST-01: Нет тестов для `process_search` / `handle_text_search` / `cmd_*`
- **Файл**: `bot/handlers/search.py:62-270` (`cmd_search`, `cmd_movie`, `cmd_series`, `handle_menu_search`, `handle_text_search`, `process_search`, `handle_type_selection`).
- **Проблема**: главный entry-point поиска от Telegram и есть та поверхность, на которую жалуется юзер. Ни один тест не вызывает `process_search` напрямую и не проверяет:
  - `MAX_QUERY_LENGTH=200` / минимум 2 символа (только в коде, без теста);
  - delegate в `process_music_search` при `content_type == MUSIC` (BUG-27 регрессия);
  - запись `SearchSession(content_type=UNKNOWN)` при «фильм/сериал?» вопросе;
  - что в Prowlarr идёт **очищенный** `parsed["title"]`, а не сырой `query` (LOGIC-09);
  - что при пустом `results` показывается «Ничего не найдено» (а не падение);
  - что при `Exception` пишется только формат-error, а сессия не остаётся «зомби».
- **Связь с жалобой**: «не различает контент» — это именно `content_type == UNKNOWN` + delegate в music + session.save. Когда баг ломает один из этих ифов, тестов, которые упадут, нет.
- **Решение**: 4-6 интеграционных тестов поверх `aiogram.types.Message` mock + AsyncMock для `db`, `db_user`, `search_service`. Покрыть: query слишком короткий, query > 200, MUSIC delegation, UNKNOWN→content_type_selection, MOVIE happy path, exception swallow.
- **Статус**: [ ]

### TEST-02: `detect_content_type` — только 4 happy-кейса, нет конфликтов и таймаутов
- **Файл**: `bot/services/search_service.py:36-109`, тесты `test_services.py:86-95`, `test_lidarr.py:269-299`.
- **Проблема**: покрыто только `Show S01`, `Show S01E05`, музыка-найдена, музыка-без-Lidarr. Не тестируется:
  - **конфликт**: имя есть и в Radarr, и в Sonarr (Wednesday — фильм 1974 / сериал 2022), сейчас порядок проверки `movies → series → artists` решает за пользователя;
  - exact-match vs substring (`_title_matches` принимает `"dune" in "dune part two"` → выберет неправильный год);
  - `query_year` mismatch (год указан, но не совпал с `movie.year` ±1);
  - кириллица: «Дюна 2021», «Властелин колец»;
  - mixed: «Wednesday Среда»;
  - `len(query) < 4` short-circuit (только в коде);
  - `Radarr.lookup_movie` бросает Exception → должно быть `[]`, не падение (лог-ветка покрыта только `gather(return_exceptions=True)` без assert).
- **Связь с жалобой**: «плохо ищет, не различает» — ровно этот метод.
- **Решение**: parametrize-таблица: 12-15 кейсов (movie-only, series-only, music-only, conflict-movie-wins, conflict-series-wins, year-match, year-mismatch, кириллица-фильм, кириллица-сериал, latin-movie, mixed, very-short, all-three-fail, lidarr-throws, radarr-throws). Использовать `AsyncMock(side_effect=TimeoutError)` для проверки тайм-аут ветки.
- **Статус**: [ ]

### TEST-03: `parse_query` — нет покрытия комплексных и иноязычных запросов
- **Файл**: `bot/services/search_service.py:202-258`, тесты `test_services.py:28-83`.
- **Проблема**: 7 тестов, все простые. Не покрыто:
  - `"Breaking Bad S03E05 1080p (2010)"` — комбо year+SE+quality (порядок очистки важен);
  - кириллический title с латинским S/E: «Очень странные дела S04»;
  - `4k`, `4К` (кириллическая К), `UHD`;
  - `"Season 5"` (английский), `сезон 5`, `5 сезон`, `сезон5` (без пробела) — последний регекс не ловит;
  - спец-символы: `&`, `'`, `:`, `()`, экранированные кавычки, эмодзи в имени;
  - год вне диапазона 1900–2100 (текущий код игнорирует, но теста нет);
  - 4-значное число, не год: «Top Gun 1986» vs «File 1986 codes» (порядок тиклов).
- **Связь с жалобой**: title очищается → идёт в Prowlarr. Лишний токен в title резко уменьшает релевантность.
- **Решение**: parametrize 15 кейсов с фиксированным `expected = {title, year, season, episode, quality}`.
- **Статус**: [ ]

### TEST-04: Middleware `auth.py` — 0% покрытия
- **Файл**: `bot/middleware/auth.py` (`AuthMiddleware`, `LoggingMiddleware`, `RateLimitMiddleware`).
- **Проблема**: критическая security-граница. Не тестируется:
  - неавторизованный user → отказ;
  - admin/user role inference;
  - **concurrent create**: `IntegrityError` ветка (62-89) — explicit re-fetch;
  - `RateLimitMiddleware`: 30/min, превышение, окно скользит, `_user_requests` cleanup при >10000 ключей;
  - `time.time` flaky-риск (см. TEST-09).
- **Связь с жалобой**: непрямая, но любой регресс в auth = «бот не отвечает», что эквивалентно «плохо ищет».
- **Решение**: 6-8 unit-тестов, мок Database + Message/CallbackQuery, `freezegun` для `time.time`.
- **Статус**: [ ]

### TEST-05: `_execute_grab` — покрыты только monitor_type (BUG-32) + SSRF, нет sad-paths
- **Файл**: `bot/handlers/search.py:567-680`, тесты `test_add_service.py:46-141`.
- **Проблема**: не покрыты ветки:
  - `not result` → format_error("Релиз не выбран");
  - `not isinstance(movie, MovieInfo)` → fallback к `lookup_movie`, fallback вернул `[]`;
  - `not profiles or not folders` → format_error;
  - `_resolve_folder` с `preferred_id=None`, с несуществующим id (fallback на `folders[0]`), с пустым списком (`ValueError`);
  - финальная `Exception` ветка (677-680) с `db.delete_session`;
  - **MUSIC** ветка `grab_release` — отсутствует целиком (в `_execute_grab` её нет — это значит music идёт через `handle_confirm_music_add`, но тестов на этот путь нет совсем).
- **Связь с жалобой**: после успешного поиска grab — финальный шаг, если он сломан — пользователь решит, что «бот не работает».
- **Решение**: 6 тестов на ветки + 1 на `_resolve_folder`.
- **Статус**: [ ]

### TEST-06: `BaseAPIClient._request` retry-логика — 0%
- **Файл**: `bot/clients/base.py:107-182` + `_safe_request:184-198`.
- **Проблема**: tenacity retry на `TimeoutException`/`ConnectError`/`RetryableAPIError`, маппинг 401→AuthenticationError, 404→NotFoundError, 429/500/502/503/504→RetryableAPIError → ретрай 2 попытки. Нет ни одного теста: рефакторинг ретраев пройдёт незамеченным, регрессия повторных POST (двойное добавление к Radarr) не обнаружится.
- **Связь с жалобой**: «иногда ищет, иногда нет» при флапах сети.
- **Решение**: использовать **respx** (для httpx) или `httpx.MockTransport`, 5-7 тестов: timeout → retry → success; 502 → retry; 401 → `AuthenticationError` без retry; ConnectError → 2 попытки → `ServiceConnectionError`. Один тест должен утверждать `mock_transport.calls[0] == calls[1]` (нет двойного `POST` на не-идемпотентных эндпоинтах — потенциальный bug).
- **Статус**: [ ]

### TEST-07: `notification_service` — покрыт только subscribe/unsubscribe/force_check
- **Файл**: `bot/services/notification_service.py`, тесты `test_qbittorrent.py:488-549`.
- **Проблема**: не покрыта `_monitor_loop`, `start/stop` (race condition двойного start), `notify_download_complete=False` short-circuit, send_notification бросает Exception.
- **Решение**: 3-4 теста с asyncio loop + cancellation.
- **Статус**: [ ]

## Средние

### TEST-08: Mocking границ — клиенты мокаются на уровне `.get()/.post()`, контракт не проверяется
- **Файл**: `tests/test_lidarr.py:78-129`, `tests/test_clients.py` целиком.
- **Проблема**: `patch.object(lidarr, "get", ...)` ставит мок поверх `BaseAPIClient.get`, что значит URL-формирование, headers, query-encoding, retry — **не проверены**. Если кто-то заменит `/api/v1/artist/lookup?term=X` на `/api/v1/artistlookup`, тесты пройдут.
- **Решение**: для критических клиентов (Prowlarr.search, Lidarr.lookup_artist, Radarr.lookup_movie) — два HTTP-уровневых теста через `respx` или `httpx.MockTransport`, проверяющих URL+headers.
- **Статус**: [ ]

### TEST-09: Flaky-риск — `time.time()` в `RateLimitMiddleware`, нет freezegun
- **Файл**: `bot/middleware/auth.py:162-180`.
- **Проблема**: окно (`now - 60`) и накопление timestamps. Если когда-то будет тест — без `freezegun` или `monkeypatch.setattr(time, "time", ...)` он будет flaky на медленных CI.
- **Решение**: при добавлении тестов TEST-04 — fix `time.time` через monkeypatch.
- **Статус**: [ ]

### TEST-10: `conftest._default_env` — autouse, но НЕ сбрасывает singleton'ы клиентов
- **Файл**: `tests/conftest.py:12-35` (как и в раунде 2).
- **Проблема**: `bot.clients.registry._prowlarr/_radarr/_sonarr/_lidarr/_qbittorrent/_emby/_tmdb/_deezer` — модульные синглтоны. Тесты, которые случайно вызовут `get_prowlarr()`, оставят клиент с замоканным state на следующий тест. Сейчас этого нет, потому что handlers/* не тестируются. Как только TEST-01/04 будут реализованы — flakeness гарантирована.
- **Решение**:
  ```python
  @pytest.fixture(autouse=True)
  async def _reset_registry():
      yield
      from bot.clients import registry
      await registry.close_all()
      for attr in ("_prowlarr","_radarr","_sonarr","_lidarr","_qbittorrent","_emby","_tmdb","_deezer"):
          if hasattr(registry, attr):
              setattr(registry, attr, None)
  ```
- **Статус**: [ ]

### TEST-11: Нет coverage gate — 0% в handlers/middleware не блокирует merge
- **Файл**: `pyproject.toml`, `Makefile`, нет CI файла с порогом.
- **Проблема**: `--cov-fail-under` не задан. План раунда 2 (`12-fix-plan.md:311-313`) — sprint 2/3/4 поэтапно поднять до 60% — частично выполнен (сейчас оценочно ~38-42% из-за добавленных тестов BUG-15/24/32), но gate отсутствует.
- **Решение**: добавить в `pyproject.toml`:
  ```toml
  [tool.coverage.report]
  fail_under = 50
  ```
  и `pytest --cov=bot --cov-fail-under=50` в Makefile/CI.
- **Статус**: [ ]

### TEST-12: Pydantic v2 моки — потеря валидации
- **Файл**: `tests/test_add_service.py:77-78` (`MagicMock(id=1, name="HD-1080p")`).
- **Проблема**: `add_service.get_sonarr_profiles` возвращает `MagicMock(id=1, ...)` вместо настоящей `QualityProfile`-модели. Если код добавит валидацию (например, `profiles[0].id` стало бы `int`), тест пройдёт с любым типом.
- **Решение**: использовать настоящие модели (`QualityProfile`, `RootFolder` уже есть). Минимальная правка.
- **Статус**: [ ]

### TEST-13: Тесты на исключения внешних сервисов в detect — отсутствуют
- **Файл**: `bot/services/search_service.py:76-91`, тестов 0.
- **Проблема**: `gather(return_exceptions=True)` ловит, но **не assert**. Если кто-то уберёт `return_exceptions=True` или поменяет логирование — поломается поведение, тесты не упадут.
- **Решение**: 3 теста — Radarr timeout, Sonarr connect-error, Lidarr 5xx — всё через side_effect, ассерт что `detect_content_type` возвращает `UNKNOWN`/`MOVIE`/`SERIES` корректно.
- **Статус**: [ ]

### TEST-14: Music callback flow от start до confirm — 0%
- **Файл**: `bot/handlers/music.py` (целиком), `tests/` — нет.
- **Проблема**: `process_music_search`, `handle_confirm_music_add`, `_handle_music_browse_albums`, trending-flow. BUG-27 (music vs search CONFIRM_GRAB) на дисбатчер в `handle_confirm_grab` сейчас покрыт только косвенно (никакой тест явно не проверяет, что `isinstance(session.selected_content, ArtistInfo)` ветка вызывает music-хендлер).
- **Решение**: 1 тест на BUG-27 (session с `ArtistInfo` → `handle_confirm_music_add` зовётся, `grab_release` НЕ зовётся). +2-3 теста на music flow.
- **Статус**: [ ]

## Низкие / INFO

### TEST-15: `test_format_torrent_action` — слабые assert (`or`)
- **Файл**: `tests/test_qbittorrent.py:359-368` (раунда 2 TEST-11 не закрыт).
- **Решение**: рассыпать на 3 теста с одним точным assert каждый.
- **Статус**: [ ]

### TEST-16: Нет IPv6 SSRF теста (раунд 2 TEST-12 не закрыт)
- **Файл**: `tests/test_lidarr.py::TestDownloadUrlValidation`.
- **Решение**: добавить `::1`, `fe80::1`, `fc00::1` в parametrize.
- **Статус**: [ ]

### TEST-17: `pytest --collect-only` отчёт — DeprecationWarning?
Запуск был чистый, 261 тест за 1.84s, ошибок коллекции нет. ✅

### TEST-18: `test_handlers_downloads.py` — паттерн «handler-impotency» отлично, но **только** для downloads
- **Положительная заметка**: проверка `cb.answer.call_count == 1` — образцовый regression-тест для BUG-15. Применить тот же паттерн к `handle_release_selection`, `handle_grab_best`, `handle_force_grab` (они тоже ведут двойной answer-риск).
- **Статус**: [ ]

### TEST-19: `tests/test_formatters.py` — только 2 теста при ~700 строк `formatters.py`
- **Файл**: `bot/ui/formatters.py`. Покрыто только calendar tz/truncation. `format_search_results_page`, `format_release_details`, `format_movie_info`, `format_series_info`, `format_artist_info` — 0%.
- **Решение**: snapshot-тесты (через `syrupy` или просто string-equality на стабильные образцы).
- **Статус**: [ ]

### TEST-20: Async fixtures корректность — OK
`asyncio_mode=auto` + `asyncio_default_fixture_loop_scope="function"` (`pyproject.toml:34-37`) корректно. Ни в одном тесте нет голого `asyncio.run()` или `loop.run_until_complete()`. Проверено: `grep -r "asyncio.run\|run_until_complete" tests/` — пусто. ✅

## Регрессионные тесты на существующие баги — статус

| Bug ID | Тест | Файл |
|---|---|---|
| BUG-04 (5xx retry) | ❌ нет | — |
| BUG-11 (timezone calendar) | ✅ | `test_formatters.py:18-43` |
| BUG-12 (calendar truncation) | ✅ | `test_formatters.py:46-66` |
| BUG-15 (double answer in downloads) | ✅ | `test_handlers_downloads.py` (4 теста) |
| BUG-19 (retry backoff stop=2) | ❌ нет (см. TEST-06) | — |
| BUG-23 (env cache_clear) | ✅ implicit | `conftest.py:33,35` |
| BUG-24 (corrupt prefs JSON) | ✅ | `test_db.py:296-318` |
| BUG-27 (music vs search CONFIRM_GRAB) | ❌ только косвенно | см. TEST-14 |
| BUG-32 (monitor_type=existing) | ✅ | `test_add_service.py:46-141` |
| LOGIC-09 (clean title to indexer) | ❌ нет | см. TEST-01 |
| SEC-01/11 (SSRF download URL) | ✅ | `test_lidarr.py:228-263` |
| SEC-04 (URL masking) | ✅ | `test_lidarr.py:195-225` |
| SEC-16 (push_release SSRF) | ✅ | `test_add_service.py:175-289` |
| SEC-19 (corrupt prefs) | ✅ | `test_db.py:296-318` |
| DB-01 (migrations idempotent) | ✅ | `test_db.py:266-293` |

## Итог

**HIGH**: TEST-01, TEST-02, TEST-04, TEST-05, TEST-06.
**MED**: TEST-03, TEST-07, TEST-08, TEST-09, TEST-10, TEST-11, TEST-12, TEST-13, TEST-14.
**LOW/INFO**: TEST-15, TEST-16, TEST-17, TEST-18, TEST-19, TEST-20.

### Прямой ответ на «достаточно ли покрыты пути жалобы пользователя»

**Нет.** Жалоба «плохо ищет, не различает контент» = front-of-funnel `handle_text_search → process_search → detect_content_type → parse_query → search_releases`. Из этой цепочки:
- `handle_text_search`/`process_search`/`cmd_*` — 0%;
- `detect_content_type` — 4 happy + 2 series, 0 conflict-кейсов;
- `parse_query` — 8 простых, 0 complex;
- `search_releases` — 1 happy в `test_services.py:97-120` (фильтрация по типу не проверена).

Любая регрессия в этих местах **молча проедет в прод**. Приоритет фиксов: TEST-01 → TEST-02 → TEST-03 → TEST-13 → TEST-04. Это закрывает весь search-funnel за ~1 день работы.

### Приоритет (Sprint)
1. **TEST-02 + TEST-03 + TEST-13** (parse_query/detect_content_type/исключения) — 1 файл, ~30 тестов, прямо адресует жалобу.
2. **TEST-01** (process_search/handle_text_search) — интеграционный, ~6 тестов.
3. **TEST-04** (auth/rate-limit middleware) — security.
4. **TEST-06 + TEST-08** (`respx` для BaseAPIClient + контракт URLs).
5. **TEST-10 + TEST-11** (registry-reset autouse + coverage gate ≥50%).
6. **TEST-14** (music BUG-27 explicit).
