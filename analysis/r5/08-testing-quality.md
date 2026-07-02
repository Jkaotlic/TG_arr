# Анализ качества тестов TG_arr (раунд 5)

## Результат прогона
- Команда: `python -m pytest tests/ -q --tb=short` (Windows, корень `g:\VSCode\TG_arr`)
- Итог: **399 passed, 0 failed, 0 skipped, 2.56s**
- Прогон быстрый и детерминированный, warnings не выдано. `asyncio_mode = "auto"` в `pyproject.toml:40`.

## Критические

### TEST-01: AddService.add_movie / add_series — реальный код добавления контента не покрыт вообще
- **Файл**: `bot/services/add_service.py:209` и `:267`; тесты `tests/test_r4_C5-handler-perf.py:263`, `tests/test_audit_r4_fixes.py:135`
- **Проблема**: единственные тесты, проходящие через «add» (trending), подменяют `AddService` целиком — тестируют мок. Ветки `add_movie`/`add_series` (дедуп по tmdb/tvdb, `existing.radarr_id`, catch-all → `ActionLog(success=False)`) не исполняются ни одним тестом. Для музыки аналог покрыт (`test_lidarr.py:302`), для кино/сериалов — нет.
- **Риск**: регрессия в ядре бота не будет поймана.
- **Решение**: юнит-тесты с AsyncMock-клиентами: existing → возврат без add; add успешен; `APIError` → `(None, action.success=False, error_message)`.
- **Статус**: [ ] Не исправлено

### TEST-02: детекция типа контента при частично недоступном сервисе (503) не покрыта — реальный прод-кейс
- **Файл**: `bot/services/search_service.py:127-153`; тест только на «все упали»: `tests/test_detect_content_type.py:55`
- **Проблема**: код специально обрабатывает частичный сбой (`failure_count < len(tasks)` → продолжаем по живым), но тестов на это нет. Нет теста «Radarr 503, Sonarr жив и матчится → SERIES». Не покрыта и ветка `lookup_timeout` (строки 118-122).
- **Риск**: именно этот кейс случается в проде; рефакторинг может незаметно превратить частичный сбой в полный UNKNOWN.
- **Решение**: (а) Radarr raises + Sonarr strong match → SERIES; (б) Lidarr raises + movie match → MOVIE; (в) таймаут → UNKNOWN, confidence 0.
- **Статус**: [ ] Не исправлено

### TEST-03: пользовательский grab-flow в handlers/search.py почти не покрыт (force_grab, confirm_grab, выбор релиза, текстовый поиск)
- **Файл**: `bot/handlers/search.py:918` (`handle_force_grab`), `:632` (`handle_confirm_grab`), `:457` (`handle_release_selection`), `:152` (`handle_text_search`), `:349` (`handle_type_selection`)
- **Проблема**: из grab-flow протестированы только `_execute_grab` (series-путь), `handle_grab_best` (double-tap) и `_decide_monitor_type`. `handle_force_grab` — включая ветку «qBittorrent не настроен» и RACE-01-guard — 0 тестов. Диспетчеризация music/movie в `handle_confirm_grab` (фикс BUG-27) — 0 тестов. Movie-путь `_execute_grab` не тестируется.
- **Риск**: BUG-27 уже случался — регресс-защиты нет; force-download — самая «опасная» кнопка бота без единого теста.
- **Решение**: mock callback + session c `selected_result`: `force_download=True` доходит до `_execute_grab`; ветка «qBit не настроен»; dispatch confirm_grab на ArtistInfo → music.
- **Статус**: [ ] Не исправлено

## Средние

### TEST-04: assert-заглушки в тестах очистки БД
- **Файл**: `tests/test_db.py:340` и `:356`
- **Проблема**: `assert deleted >= 0` всегда истинно. `cleanup_old_sessions`/`cleanup_old_searches` фактически не покрыты (в отличие от `cleanup_old_actions` в `test_r4_C6-db-notify.py:48` — правильный образец).
- **Решение**: вставить сессию/поиск со старым `created_at` напрямую, `assert deleted == 1` + свежие остались.
- **Статус**: [ ] Не исправлено

### TEST-05: жизненный цикл notification loop не покрыт
- **Файл**: `bot/services/notification_service.py:61-135, 169-172, 177-198`
- **Проблема**: не покрыты `start()`/`stop()` (двойной start, отмена таски), `_monitor_loop` (error → sleep(10) → продолжение), `_initial_sync`, очистка удалённых торрентов, частичный сбой рассылки в `_notify_completion`.
- **Решение**: тесты на start/stop-идемпотентность, `_initial_sync`, удалённый торрент, sender с ошибкой на первом user_id.
- **Статус**: [ ] Не исправлено

### TEST-06: webhook — error-пути не покрыты
- **Файл**: `bot/webhook.py:82-91, 99-106`; тесты `tests/test_feat_webhook.py`
- **Проблема**: не покрыты: невалидный JSON → 400; `notify` кидает исключение → всё равно 200 (задокументированное поведение «never 500 the *arr side» без теста); `start_webhook_server`.
- **Решение**: `post("/webhook", data="not json")` → 400; notify с `side_effect=Exception` → 200.
- **Статус**: [ ] Не исправлено

### TEST-07: обрезка сообщений 4096 — покрыт только календарь; у списка результатов поиска обрезки нет вовсе
- **Файл**: `bot/ui/formatters.py:586` (`_safe_truncate`, применяется только в `:950`); `:79` (`format_search_results_page` — без обрезки); тест `tests/test_formatters.py:46`
- **Проблема**: ветки `_safe_truncate` («нет `\n` в бюджете», разрез внутри `<tag`, «budget <= 0») не покрыты. `format_search_results_page` не ограничивает длину — потенциальный `MESSAGE_TOO_LONG`.
- **Риск**: реальный 400 от Bot API на «жирных» раздачах RuTracker.
- **Решение**: юнит-тесты `_safe_truncate` (3 ветки); тест `format_search_results_page` с титулами 300+ символов и эмодзи → `len(out) <= 4096` (потребует и фикса кода).
- **Статус**: [ ] Не исправлено

### TEST-08: граница legacy/typed пагинации покрыта только с typed-стороны
- **Файл**: `bot/handlers/music.py:169` (`art_page:`), `bot/handlers/downloads.py:212` (`t_page:`)
- **Проблема**: legacy-строковые пагинации `art_page:N` и `t_page:N` (парсинг `int(...)`, некорректный N) без единого handler-теста; нет регресс-теста «старый префикс `page:` больше нигде не генерируется».
- **Решение**: тест хендлера `art_page:`/`t_page:` (валидная и мусорная страница) + тест на отсутствие `callback_data.startswith("page:")`.
- **Статус**: [ ] Не исправлено

### TEST-09: concurrency-тесты на реальном asyncio.sleep — риск флака
- **Файл**: `tests/test_r4_C2-qbit.py:96,112` (sleep 0.02), `tests/test_r4_C5-handler-perf.py:60,177-190` (sleep 0.05), `tests/test_audit_r4_fixes.py:242` (sleep 0.05)
- **Проблема**: утверждения «все три стартовали до того, как первый закончился» опираются на реальные таймеры — на загруженном раннере/Pi возможен флак.
- **Решение**: `asyncio.Event`/`Barrier` вместо sleep-гонок.
- **Статус**: [ ] Не исправлено

### TEST-10: RateLimitMiddleware и fail-closed ветка авторизации без тестов
- **Файл**: `bot/middleware/auth.py:154-206` (rate limit — 0 тестов), `:32-35` (`_is_authorized` при исключении БД → False), `:56-57` (событие без from_user)
- **Проблема**: security-значимый rate-limiter не покрыт (в т.ч. cleanup на `:201-204`, который никогда ничего не удаляет — см. PERF-09); fail-closed при ошибке БД не запинен.
- **Риск**: рефакторинг может превратить fail-closed в fail-open.
- **Решение**: `db.is_allowed_in_db = AsyncMock(side_effect=Exception)` → False; 31-й запрос за окно → handler не вызван; событие без from_user → None.
- **Статус**: [ ] Не исправлено

## Низкие

### TEST-11: мёртвые фикстуры в conftest
- **Файл**: `tests/conftest.py:38-41, 44-86, 140-158`
- **Проблема**: `mock_env`, `sample_prowlarr_response`, `sample_quality_profiles`, `sample_root_folders` не используются нигде.
- **Решение**: удалить 4 фикстуры.
- **Статус**: [ ] Не исправлено

### TEST-12: дублирование тестов и хелперов
- **Файл**: `tests/test_clients.py:50-165` vs `tests/test_parsing.py`; `tests/test_lidarr.py:228-263` vs `tests/test_ssrf_trusted_hosts.py`; хелперы-копипаста: `_mock_http_with_cookie` (×2), `_callback_with_status` (×2), `_build_add_service` (×2), `_make_callback` (×3)
- **Решение**: хелперы → `conftest.py`; из test_clients убрать парсинг-тесты, оставив уникальную retry-логику (`:167-204`).
- **Статус**: [ ] Не исправлено

### TEST-13: имена регрессионных файлов раунда 4 непонятны без контекста аудита
- **Файл**: `tests/test_audit_r4_fixes.py`, `test_audit_r4_phase2.py`, `test_r4_C1-deploy-docs.py` … `test_r4_C8-coverage.py`, `test_r4_race04.py`
- **Проблема**: имена привязаны к номерам кластеров аудита, а не к поведению; дефисы делают модули неимпортируемыми.
- **Решение**: переименовать по смыслу с сохранением ID в docstring; дефисы → подчёркивания.
- **Статус**: [ ] Не исправлено

### TEST-14: тест, проверяющий присваивание в конструкторе (тест мока)
- **Файл**: `tests/test_r4_C6-db-notify.py:121-126`
- **Проблема**: `assert service.qbittorrent is sentinel_client` — ценность ~0; RACE-05 реально не защищает.
- **Решение**: удалить или заменить тестом wiring'а.
- **Статус**: [ ] Не исправлено

### TEST-15: сверхжёсткие пины в guard-тестах деплоя
- **Файл**: `tests/test_r4_C1-deploy-docs.py:60` (`pinned == "3.27.0"`), `:125-129`
- **Проблема**: плановое обновление aiogram роняет тест — версия захардкожена дважды.
- **Решение**: убрать литерал — достаточно проверки «README упоминает ту же версию, что requirements.txt».
- **Статус**: [ ] Не исправлено

### TEST-16: утечка module-state между тестами (trending-кэши)
- **Файл**: `tests/test_audit_r4_fixes.py:150-159, 315-328`
- **Проблема**: `_trending_movies_cache[123]` заполняются, но не очищаются после теста — латентная порядковая зависимость.
- **Решение**: `try/finally` с `.clear()` или autouse-фикстура очистки.
- **Статус**: [ ] Не исправлено

### TEST-17: непокрытые edge cases данных
- **Проблема**: нет тестов на эмодзи/юникод в названиях релизов; экстремально длинное название (кроме календаря); пустой ответ Prowlarr (`search_service.py:285-287`).
- **Решение**: parametrize-кейсы с эмодзи и 300-символьным названием; тест пустого ответа Prowlarr.
- **Статус**: [ ] Не исправлено

## Карта непокрытых критических путей

| Путь | Файл | Покрыт? | Приоритет |
|---|---|---|---|
| Auth: allow/deny, авто-создание, env+DB allowlist | `bot/middleware/auth.py` | ✅ Да | — |
| Auth: fail-closed при ошибке БД, без from_user | `auth.py:32,56` | ❌ Нет | Средний |
| RateLimitMiddleware (30 req/min) | `auth.py:154` | ❌ Нет | Средний |
| AddService.grab_*_release (push/reject/SSRF/qBit-fallback) | `add_service.py:328+` | ✅ Да | — |
| AddService.add_movie / add_series | `add_service.py:209,267` | ❌ Нет (замокан целиком) | **Высокий** |
| handle_force_grab / handle_confirm_grab / handle_release_selection | `search.py:918,632,457` | ❌ Нет | **Высокий** |
| _execute_grab: movie-путь | `search.py:727` | ❌ Нет (только series) | Средний |
| Notification: _check_for_completions / force_check | `notification_service.py:136,200` | ✅ Да | — |
| Notification: _monitor_loop, start/stop, _initial_sync | `notification_service.py:61-135` | ❌ Нет | Средний |
| Webhook: парсер + happy POST | `webhook.py:32,77` | ✅ Да | — |
| Webhook: bad JSON→400, notify-fail→200 | `webhook.py:82-91,99` | ❌ Нет | Средний |
| Пагинация typed PageCB (search) | `callbacks.py`, `search.py:388` | ✅ Да | — |
| Legacy пагинация `art_page:` / `t_page:` | `music.py:169`, `downloads.py:212` | ❌ Нет | Средний |
| Обрезка 4096: календарь | `formatters.py:950` | ✅ Да | — |
| Обрезка 4096: результаты поиска | `formatters.py:79` | ❌ Нет (и обрезки нет) | **Высокий** |
| Детекция: все lookups упали → UNKNOWN | `search_service.py:144` | ✅ Да | — |
| Детекция: один сервис 503, остальные живы | `search_service.py:127-153` | ❌ Нет | **Высокий** |
| Детекция: lookup_timeout (8s cap) | `search_service.py:118-122` | ❌ Нет | Низкий |
| BaseAPIClient._request (retry, маппинг ошибок) | `bot/clients/base.py` | ⚠️ Частично | Средний |
| DB: cleanup_old_sessions / cleanup_old_searches | `bot/db.py` | ⚠️ Заглушки | Средний |

## Проверено — проблем нет
- **Прогон**: 399/399 за 2.56s, ноль скипов и ворнингов; сеть не нужна.
- **Изоляция окружения**: autouse `_default_env` с `get_settings.cache_clear()` до и после каждого теста.
- **TZ/локаль**: tz-зависимый тест монкипатчит TIMEZONE, фиксированная дата, skipif на tzdata; прод-кейс Europe/Moscow покрыт.
- **Качество моков в целом**: граница мокается на уровне методов клиента — стабильно; «тестов мока», кроме TEST-01/TEST-14, нет. `_post_no_retry` детерминирует время через фейковый `time.monotonic`.
- **Регрессии RACE-01/02/04**: тесты реально гоняют конкурентность.
- **SSRF-guard**: покрыт с обеих сторон (trusted-host пропускается; приватные блокируются; push_release не вызывается).
- **HTML-escaping**: везде проверяется и наличие эскейпа, и отсутствие сырой строки.
- **qBittorrent 5.2**: оба контракта логина, фолбэк на legacy endpoints, 403→re-auth→retry.
- **Битые данные в БД**: corrupt preferences → дефолты; corrupt session → авто-удаление + None.
