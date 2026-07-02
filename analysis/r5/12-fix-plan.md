# Fix-план аудита р5 TG_arr

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (волны параллельных агентов с дизъюнктным владением файлами). Каждый пункт ссылается на ID находки — **полное описание, точные file:line и конкретное решение лежат в соответствующем отчёте** `analysis/r5/NN-*.md`; исполнитель обязан прочитать свои секции отчётов перед правкой. Модель исполнителей: **sonnet5**. TDD обязателен для пунктов, помеченных `*` (RED→GREEN→REFACTOR).

**Goal:** Закрыть все находки аудита р5 (кроме отложенного архитектурного рефакторинга), не сломав прод на rpie4.

**Architecture:** Исполнение волнами: Wave 0 — гигиена репо (оркестратор); Wave 1 — 7 параллельных агентов, каждый владеет непересекающимся множеством файлов; Wave 2 — cross-cutting фиксы, требующие файлов из разных кластеров Wave 1; Wave 3 — бандл-апгрейд зависимостей отдельным коммитом. Полный прогон тестов — только между волнами (во время волны каждый агент гоняет лишь свои тесты — соседние файлы в этот момент могут быть в промежуточном состоянии).

**Tech Stack:** Python 3.12, aiogram 3.x, httpx, structlog, aiosqlite, pytest (asyncio_mode=auto), ruff.

## Global Constraints

- Прод — Raspberry Pi 4 (arm64, 256MiB лимит); ничего не добавлять тяжёлого в рантайм.
- Каждый агент правит ТОЛЬКО файлы своего кластера (см. «Files» задачи). Новые тестовые файлы — можно.
- Во время волны агент запускает только СВОИ тесты (`pytest tests/test_<свои>.py`), не весь сьют.
- Коммиты делает оркестратор после верификации волны, не агенты.
- Стиль: смотреть на соседний код; structlog kv-события snake_case для новых логов; html.escape для любых подстановок в HTML.
- Каждый агент в СВОИХ файлах добавляет `exc_info=True` к `log.error(...)` внутри `except Exception:`-catch-all (OBS-04) — это глобальный пункт, размазанный по кластерам.

---

## Wave 0 — гигиена репо (оркестратор, без агентов)

- [ ] DEAD-01: заархивировать диффы 8 worktrees в `analysis/r5/worktree-patches/wf-{1..8}.patch`, затем `git worktree remove --force` каждого + `git branch -D worktree-wf_*`
- [ ] DEAD-17b: перенести 13 отчётов р4 из `analysis/*.md` в `analysis/r4/`

## Wave 1 — 7 параллельных агентов (sonnet5)

### Task A: *arr-клиенты + AddService + models
**Files:** Modify: `bot/clients/base.py`, `bot/clients/radarr.py`, `bot/clients/sonarr.py`, `bot/clients/lidarr.py`, `bot/clients/tmdb.py`, `bot/services/add_service.py`, `bot/models.py`; Tests: `tests/test_add_service.py`, `tests/test_clients.py`, `tests/test_lidarr.py`, `tests/test_r4_C4-services.py`, `tests/test_ssrf_trusted_hosts.py`, новые.
**Interfaces (Produces):** сигнатуры `push_release`/`get_quality_profiles`/`get_root_folders` не меняются (только поведение/кэш); `grab_music_release` удаляется — никто вне кластера его не вызывает (проверено DEAD-04).

- [ ] * BUG-01: push_release в 3 клиентах разворачивает list-ответ (`result[0] if list`); RED-тест на list с `approved: true` на уровне клиента (см. 02-bugs.md)
- [ ] * BUG-05: убрать fallback `*arr.grab_release(prowlarr_guid)` из add_service (3 места) — путь всегда 404; авто-поиск остаётся, но сообщение честно «Запущен автопоиск (выбранный релиз не удалось передать)» (см. 02-bugs.md)
- [ ] * SEC-01: `_trusted_service_hosts()` → множество `(hostname, port)` с учётом дефолтных портов схем; тесты «тот же хост, чужой порт → блок» (см. 01-security.md)
- [ ] * SEC-03: `_mask_url` — маскировать `link`/`file`/`r`/`rss` + пасскей-подобные сегменты пути; логировать только scheme://netloc + путь с маской (см. 01-security.md)
- [ ] SEC-08: комментарий «принятый риск DNS-rebinding» у `_validate_download_url`
- [ ] OBS-05: единое INFO-событие `grab_completed` (success, path=push|qbit|auto_search|rejected|failed, force_download, content_type) в конце grab_movie/series (см. 07-observability.md)
- [ ] OBS-14: tenacity before_sleep-лог с attempt + итоговый WARNING `request_retries_exhausted` в base.py
- [ ] PERF-07-cache: TTL-кэш (10 мин) для get_quality_profiles/get_root_folders в base или клиентах (см. 06-performance.md PERF-07)
- [ ] BUG-13: tmdb.py — если ключ не начинается с `eyJ` → слать query-параметром `api_key`, иначе Bearer; поправить описание в добавляемом комментарии (config.py не трогать — чужой кластер)
- [ ] DEAD-04: удалить `grab_music_release` (~135 строк) + его тесты в test_add_service/test_lidarr/test_r4_C4
- [ ] DEAD-09: удалить `LidarrClient._parse_album`, `AlbumInfo` из models и union `ContentInfo` + тесты
- [ ] DEAD-11: удалить `BaseAPIClient.delete`
- [ ] DEAD-12: удалить `SearchResult.info_url`/`category_names` (уменьшает session-JSON); остальные write-only поля не трогать
- [ ] LOGIC-10c: удалить `lidarr.check_connection` (дубль базового) и мёртвый `self.prowlarr` в AddService
- [ ] LOGIC-22: `QBIT_INFINITE_ETA = 8640000` именованной константой в models.py:385
- [ ] * TEST-01: тесты AddService.add_movie/add_series (existing→без add; успех; APIError→ActionLog(success=False))
- [ ] OBS-04: exc_info=True в catch-all своих файлов

### Task B: торренты (downloads + qbittorrent + keyboards + callbacks)
**Files:** Modify: `bot/handlers/downloads.py`, `bot/clients/qbittorrent.py`, `bot/ui/keyboards.py`, `bot/ui/callbacks.py`; Tests: `tests/test_qbittorrent.py`, `tests/test_handlers_downloads.py`, `tests/test_feat_callbackdata.py`, новые.
**Interfaces (Produces):** новый `TorrentPageCB(CallbackData, prefix="tpg")` с полями `page:int`, `filter:str` в callbacks.py; `check_qbt_enabled` теперь возвращает `QBittorrentClient | None`.

- [ ] * LOGIC-01: фильтр переживает пагинацию/refresh/back — `TorrentPageCB(page, filter)`, `_render_torrent_list(..., filter_type)`; кнопки фильтров тоже несут состояние (см. 05-logic-issues.md)
- [ ] * BUG-04a/LOGIC-02: pause_all/resume_all/speed_set → рендер-хелперы без повторного `callback.answer` (образец — `_render_torrent_list`); ровно один answer на callback (см. 02-bugs.md BUG-04)
- [ ] * LOGIC-03: `Keyboards.speed_limits_menu(status.download_limit, status.upload_limit)` — маркер текущего пресета честный; 4 копии построения рядов → helper
- [ ] * BUG-09: `asyncio.Lock` вокруг `_get_client` и `_ensure_authenticated` в qbittorrent.py (образец — base.py `_client_lock`)
- [ ] * BUG-12a: `html.escape(args)` в «Торрент не найден: {args}» (downloads.py:136,164)
- [ ] * BUG-14/DEAD-03: подключить confirm-флоу для `t_delf` (удаление С файлами): новый формат `t_delfc:<hash>` для подтверждения, починить парсинг; для `t_delete` (без файлов) оставить как есть
- [ ] PERF-05: полный 40-hex хэш в callback_data действий с торрентом (48 байт < 64); short-hash — fallback для старых сообщений
- [ ] PERF-06a: `httpx.Limits(max_keepalive_connections=4, max_connections=10, keepalive_expiry=300.0)` в qbittorrent.py
- [ ] SEC-06: `is_admin`-гейт на pause_all/resume_all/speed_set/recheck/prio (+ /pause,/resume команды всех торрентов; точечные pause/resume одного торрента оставить всем)
- [ ] DEAD-02: удалить handle_recheck/handle_priority + методы recheck/set_priority_top/set_priority_bottom (кнопок нет)
- [ ] DEAD-13: удалить мёртвую ветку `filter_value == "menu"`
- [ ] LOGIC-16: `check_qbt_enabled` возвращает клиент или None (убрать двойную проверку в 4 хендлерах)
- [ ] LOGIC-12: фильтры колбэков через константы CallbackData вместо строковых литералов (в рамках миграции t_* выше)
- [ ] LOGIC-22: qbit docstring «first 8 chars»→16; убрать неиспользуемый `current_filter`/`page_torrents = torrents` в keyboards (поглощается LOGIC-01)
- [ ] OBS-12b: DEBUG-лог `qbit_command_failed` в cmd_pause/cmd_resume; `refetch_failed` в `_refetch_one`
- [ ] * TEST-08a: тесты `t_page`/TorrentPageCB (валидная, мусорная страница, сохранение фильтра)
- [ ] OBS-04: exc_info=True в catch-all своих файлов

### Task C: поиск (search + search_service + scoring + formatters)
**Files:** Modify: `bot/handlers/search.py`, `bot/services/search_service.py`, `bot/services/scoring.py`, `bot/ui/formatters.py`; Tests: `tests/test_detect_content_type.py`, `tests/test_formatters.py`, `tests/test_scoring.py`, `tests/test_r4_C7-search-models.py` (docstring), новые.
**Interfaces (Consumes):** ничего из других кластеров. **Produces:** поведение `detect_with_confidence` (семафор/кэш) — сигнатура не меняется.

- [ ] * PERF-01: (1) глобальный `asyncio.Semaphore(2)` вокруг detection-lookup'ов; (2) TTL-кэш detection-результатов 5 мин по нормализованному запросу; (3) circuit-breaker 30с на сервис после 503 (см. 06-performance.md — точный план из 4 пунктов, ступенчатую стратегию (3) НЕ делать — меняет UX)
- [ ] * BUG-02: legacy-хендлер `F.data.startswith("page:")` → `callback.answer("Кнопка устарела — повторите поиск", show_alert=True)`
- [ ] * BUG-03: try/except TelegramBadRequest («message is not modified») вокруг edit_text в handle_pagination/handle_back + answer всегда
- [ ] * BUG-06: `today` в календарном форматтере — в `ZoneInfo(settings.timezone)`; хелпер `to_local(dt)` для publish_date/added/completed/уведомлений/истории (см. 02-bugs.md)
- [ ] * BUG-07: `callback.answer()` сразу после валидации сессии в handle_release_selection; финальный ack убрать
- [ ] * BUG-11/TEST-07: `_safe_truncate(page_text, 3800)` в format_search_results_page + обрезка title до 150; юнит-тесты 3 веток _safe_truncate + тест «длинные названия ≤ 4096»
- [ ] * DEAD-06: подключить `preferred_resolution` как бонус в `calculate_score` (+15 за совпадение разрешения); тест «1080p-предпочтение поднимает 1080p-релиз»; удалить `filter_by_quality`/`get_best_result` если не используются после этого
- [ ] DB-03 (search-side): убрать вызов `save_search` из process_search (db-методы правит Task F)
- [ ] LOGIC-04: `_render_results_page(...)` — дедуп трёх копий рендера страницы
- [ ] LOGIC-23: в except редактировать status_msg («Ищу релизы...») вместо нового сообщения; после выбора типа — edit исходного сообщения с кнопками
- [ ] PERF-07b: `asyncio.gather(profiles, root_folders)` в _execute_grab-пути (search.py:758,796)
- [ ] PERF-08: `r.calculated_score = score` in-place вместо model_copy в sort_results
- [ ] DEAD-14: `except Exception:` вместо `(ZoneInfoNotFoundError, Exception)`; ZoneInfo-объект кэшировать на модульном уровне
- [ ] DEAD-17a: поправить docstring test_r4_C7 (monitor_type возвращён фичей #2)
- [ ] LOGIC-22: get_services() не возвращает scoring (подправить вызовы в своём файле; `_SCORING_SERVICE` оставить — его импортирует music.py); history: эмодзи для music-действий в formatters:364
- [ ] OBS-12a: DEBUG `emby_note_skipped` в `_emby_library_note`
- [ ] * TEST-02: тесты детекции при частичном 503 (Radarr упал + Sonarr матчится → SERIES; таймаут → UNKNOWN)
- [ ] * TEST-03: тесты handle_force_grab (force_download=True доходит; «qBit не настроен»), dispatch confirm_grab movie vs music, movie-путь _execute_grab
- [ ] OBS-04: exc_info=True в catch-all своих файлов

### Task D: music + trending + calendar + emby + status
**Files:** Modify: `bot/handlers/music.py`, `bot/handlers/trending.py`, `bot/handlers/calendar.py`, `bot/handlers/emby.py`, `bot/handlers/status.py`, `bot/handlers/history.py`, `bot/clients/emby.py`; Tests: новые + связанные существующие.

- [ ] * SEC-04: html.escape для version/service/path в `_format_health` (status.py:46-54)
- [ ] * BUG-10/PERF-12: LRU-вытеснение старейших вместо `cache.clear()` в music/trending кэшах + TTL-метка 6ч для trending
- [ ] * BUG-12b: html.escape(error_msg) в trending.py:390,504
- [ ] BUG-04c: emby-хендлеры — один answer на callback, рендер без повторного ack (образец BUG-04a)
- [ ] BUG-17a: try/except «message is not modified» в календаре (повтор активного периода)
- [ ] PERF-06b: httpx.Limits в emby.py (как PERF-06a)
- [ ] PERF-07a: gather для последовательных вызовов Lidarr (music.py:308-310) и trending add-флоу
- [ ] OBS-08: `health_check_failed`/`calendar_fetch_failed` со `service=` kv вместо f-string имён событий
- [ ] LOGIC-14a: `_render_artist_list()` — дедуп трёх копий в music.py (per_page/keyboards не трогать — Wave 2)
- [ ] LOGIC-17: `_collect_statuses(include_deezer)` — дедуп cmd_status/cmd_health
- [ ] LOGIC-20: show_emby_status → `_render_status_text()` + два тонких вызывающих
- [ ] LOGIC-22: убрать повторный `await get_sonarr()` (trending.py:460)
- [ ] * TEST-08b: тесты хендлера `art_page:` (валидная/мусорная страница)
- [ ] OBS-04: exc_info=True в catch-all своих файлов

### Task E: инфраструктура рантайма (main + notification + webhook + config + users)
**Files:** Modify: `bot/main.py`, `bot/services/notification_service.py`, `bot/webhook.py`, `bot/config.py`, `bot/handlers/users.py`; Tests: `tests/test_feat_webhook.py`, `tests/test_feat_users.py`, новый `tests/test_notification_lifecycle.py`, новый `tests/test_logging_setup.py`.
**Interfaces (Consumes):** `Database.run_maintenance(backup: bool = False) -> dict[str, int]` из Task F (вызвать в `_periodic_cleanup` вместо трёх отдельных cleanup_*; раз в сутки — с backup=True). `NotificationService.get_stats()` уже существует.
**Produces:** `Settings.webhook_token: str | None = None` (env `WEBHOOK_TOKEN`); `Settings.log_format: Literal["json","console"] = "json"` (env `LOG_FORMAT`).

- [ ] * OBS-01: stdlib-логгеры через `structlog.stdlib.ProcessorFormatter` (JSON + `_mask_tokens`); httpx/httpcore/aiogram.event/aiohttp.access → WARNING; тест: запись через logging.getLogger("httpx") даёт JSON с маской (см. 07-observability.md)
- [ ] * OBS-03: send_notification-обёртка возвращает bool; «Sent completion notification» только при успехе; удалить мёртвую ERROR-ветку в _notify_completion
- [ ] * SEC-02/BUG-08: `webhook_token` в конфиге; маршрут `/webhook/{token}` (+ отклонение без/с неверным токеном 403); `webhook_bind` default → `127.0.0.1`; тесты (см. 01-security.md)
- [ ] * OBS-02: события `webhook_received` (INFO), `webhook_invalid_json` (WARNING, remote IP), `webhook_notified` (INFO)
- [ ] * DB-04/LOGIC-08/BUG-15: runtime-allowlist в уведомлениях — on_startup подписывает `db.list_allowed_users()`; `/adduser` → subscribe_user, `/deluser` → unsubscribe_user; `_webhook_notify` объединяет env+DB списки; тесты
- [ ] * PERF-02 (минимальный): notification loop — `get_torrents(filter=downloading)` + завершение по исчезновению из downloading с проверкой через `get_torrent(hash)`; адаптивный интервал (нет активных → 300с, есть → 60с); тесты (полный sync/maindata — отложен, см. Refactoring)
- [ ] * LOGIC-09: `@model_validator(mode="after")` в Settings — warning при «URL без ключа» (lidarr/emby/qbit), «notify без qBit», «webhook_enabled без token»; тесты
- [ ] OBS-06: `bind_contextvars(component="notification_service")` в таске (аналогично cleanup/warmup); backoff с логом повторных ошибок раз в 10 циклов + INFO о восстановлении
- [ ] OBS-07 (часть): убрать дублирующий «Notification service started» из on_startup
- [ ] OBS-09: раз в 6ч INFO `notification_stats` из get_stats() (в цикле _periodic_cleanup)
- [ ] OBS-10: INFO `background_task_started` (task=liveness/cleanup/watchdog/webhook)
- [ ] OBS-13: `LOG_FORMAT=json|console` отдельно от LOG_LEVEL
- [ ] SEC-05: рекурсивный маскировщик + паттерн `//user:pass@` в _mask_tokens
- [ ] LOGIC-18b: webhook: диапазон эпизодов «S01E01-E10» вместо `episodes[0]`
- [ ] LOGIC-22: убрать `or []` у admin_tg_ids (main.py:117,319)
- [ ] * TEST-05: lifecycle-тесты notification (двойной start, stop, _initial_sync, исчезнувший торрент, częściowy сбой рассылки)
- [ ] * TEST-06: webhook-тесты: bad JSON→400, notify-исключение→200
- [ ] OBS-04: exc_info=True в catch-all своих файлов

### Task F: БД + middleware + settings
**Files:** Modify: `bot/db.py`, `bot/middleware/auth.py`, `bot/handlers/settings.py`; Tests: `tests/test_db.py`, `tests/test_r4_C6-db-notify.py`, новые.
**Interfaces (Produces):** `Database.run_maintenance(backup: bool = False) -> dict[str, int]` — вызывает cleanup_old_sessions/searches/actions + `PRAGMA optimize`; при backup=True делает `VACUUM INTO data/backup/bot-YYYYMMDD.db` (ротация: хранить 3 последних). Task E вызывает её из _periodic_cleanup.

- [ ] * DB-01/DB-08: `run_maintenance()` с PRAGMA optimize + VACUUM INTO-бэкапом и ротацией (3 шт.); тест: бэкап-файл создаётся, старые ротируются (примечание в README: копирование backup/ вне SD — хостовым cron'ом, вне скоупа бота)
- [ ] * DB-05: `update_user_preference(user_id, key, value)` через SQLite `json_set` — точечный UPDATE; settings-хендлеры переведены с «мутируй весь preferences» на точечный; тест конкурентных изменений двух ключей — оба выживают
- [ ] DB-03 (db-side): удалить `get_search_results`; `save_search` — писать только метаданные (query, content_type, result_count) без results_json; обновить test_db
- [ ] DB-06: миграция v3 — `idx_actions_user_created ON actions(user_id, created_at DESC)`, дроп `idx_actions_user`
- [ ] DB-09: `remove_allowed_user` дополнительно удаляет сессию юзера
- [ ] * PERF-09/BUG-17b: rate-limit cleanup — чистить юзеров по `reqs[-1] < window_start`, порог 1000; тест
- [ ] * TEST-10: тесты auth: fail-closed при исключении БД → deny; событие без from_user → None; 31-й запрос за окно → отказ
- [ ] * TEST-04: переписать заглушки `assert deleted >= 0` — вставить старую сессию/поиск, `assert deleted == 1`, свежие остались
- [ ] BUG-04b: settings set-хендлеры — один answer на callback (рендер-хелпер, образец BUG-04a)
- [ ] LOGIC-05: табличная диспетчеризация 6 пар menu/set хендлеров settings (один generic-хендлер + таблица `{prefix: (pref_key, getter)}`) — локальный рефакторинг одного модуля; TTL-кэш профилей приезжает из Task A автоматически
- [ ] PERF-07c: gather 4 вызовов в handle_settings_back / cmd_settings
- [ ] OBS-04: exc_info=True в catch-all своих файлов

### Task G: деплой + зависимости + конфиги dev-инструментов
**Files:** Modify: `Dockerfile`, `docker-compose.yml`, `docker-compose.dev.yml`, `Makefile`, `.dockerignore`, `.env.example`, `requirements.txt`, `requirements-dev.txt`, `pyproject.toml`, `README.md`; Tests: `tests/conftest.py`, `tests/test_r4_C1-deploy-docs.py`.

- [ ] DEP-01: `aiohttp==3.13.4` в requirements.txt + `"aiohttp>=3.9,<4"` в pyproject
- [ ] DEP-07/DEAD-16: `PyYAML>=6,<7` в requirements-dev.txt и [dev] extra (guard-тесты compose перестанут скипаться — проверить, что они проходят!)
- [ ] DEP-04: `[tool.ruff]` — `target-version="py312"`, `line-length=100` (по факту кода), `lint.select=["E4","E7","E9","F"]` (текущие дефолты, зафиксированные явно; расширение набора — Wave 2)
- [ ] DEP-03: `make typecheck` (mypy bot/) + минимальный `[tool.mypy]` (python_version=3.12, ignore_missing_imports=true); НЕ добавлять в make lint (ошибки чинить вне скоупа)
- [ ] DEP-06: `make dev` → `pip install -e . -r requirements-dev.txt`
- [ ] DEP-08: цель `make check-base-image` (imagetools inspect) + строка в README о ежемесячном bump
- [ ] * DEPLOY-01/OBS-11/DB-10: в compose `environment:` добавить `WEBHOOK_ENABLED/WEBHOOK_PORT/WEBHOOK_BIND/WEBHOOK_TOKEN`, `PROWLARR_SEARCH_TIMEOUT/RETRIES`, `DATABASE_PATH`, `LOG_FORMAT` (все `${VAR:-default}`); закомментированный блок `ports:` для webhook; guard-тест на полноту allowlist vs config.py
- [ ] DEPLOY-02: цели `deploy` (build → tag prev → up -d → ps) и `rollback` (retag prev → up -d); `docker-restart` в .PHONY
- [ ] DEPLOY-03: честные комментарии в docker-compose.dev.yml (без claim'а автозагрузки и standalone)
- [ ] DEPLOY-04: `**/__pycache__`, `**/*.pyc`, `**/.pytest_cache` в .dockerignore
- [ ] DEPLOY-05/SEC-07/PERF-10: compose — `read_only: true`, `tmpfs: [/tmp]`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`; Dockerfile — COPY bot/ без --chown (root-owned код)
- [ ] DEPLOY-06: `# PROWLARR_SEARCH_TIMEOUT=25.0`, `# PROWLARR_SEARCH_RETRIES=1`, `# WEBHOOK_TOKEN=` в .env.example
- [ ] DEPLOY-07: поправить комментарий про tzdata/UTC-таймстемпы в Dockerfile
- [ ] TEST-11/DEAD-15: удалить 4 мёртвые фикстуры из conftest.py
- [ ] TEST-15: убрать литерал «3.27.0» из guard-теста (сравнение README ↔ requirements.txt остаётся)

## Wave 2 — cross-cutting (после мержа Wave 1, 2 параллельных агента)

### Task H: межкластерные фиксы
- [ ] * DB-02: per-user `asyncio.Lock` на цикл get_session→мутация→save_session (реестр локов в db.py, использование в search.py/music.py hot-path'ах)
- [ ] LOGIC-06: сохранить lookup-кандидатов detection в SearchSession → убрать повторные lookup'ы в handle_release_selection/_execute_grab (grab_best-путь)
- [ ] LOGIC-07/DEAD-10: `bot/ui/menu.py` — единый источник текстов кнопок; MENU_BUTTONS = frozenset; main_menu и все 8 модулей импортируют оттуда; удалить мёртвые MENU_* из start.py
- [ ] LOGIC-13: общий `_strip_command` (режет `@botname`) в `bot/handlers/common.py`; использовать в search/downloads/music
- [ ] LOGIC-15: `safe_edit(message, text, **kw)` там же; заменить ~10 копий try/except «not modified»
- [ ] LOGIC-14b: per_page музыкальной пагинации из settings.results_per_page (music.py + keyboards.py)
- [ ] BUG-16: `season_back` → перерисовка карточки релиза вместо сброса выбора
- [ ] DEAD-07: удалить `detect_content_type`-обёртку; тесты на `detect_with_confidence(...).content_type`
- [ ] DEAD-08: удалить `force_check` (get_stats/unsubscribe_user теперь используются E)
- [ ] OBS-07 (финал): унификация имён событий snake_case (`health_check_failed` с service=, `search_completed` vs prowlarr — по списку из 07-observability.md)
- [ ] PERF-04 (транспарентный): in-process write-through кэш активных сессий внутри Database.get_session/save_session (cap 50, инвалидация в delete/update)

### Task I: тест-гигиена
- [ ] * TEST-09: sleep-гонки → asyncio.Event/Barrier (3 файла)
- [ ] TEST-12: хелперы-дубли → conftest.py; убрать дублирующие парсинг-тесты из test_clients.py
- [ ] TEST-13: переименовать r4-файлы по смыслу (дефисы → подчёркивания), ID в docstring
- [ ] TEST-14: заменить тест-присваивание wiring-тестом
- [ ] TEST-16: autouse-фикстура очистки trending-кэшей
- [ ] * TEST-17: parametrize-кейсы эмодзи/300-символьных названий; тест пустого ответа Prowlarr

## Wave 3 — зависимости (отдельный коммит)

- [ ] DEP-05: aiogram 3.27.0→3.29.1, pydantic 2.12.5→2.13.4 (поднять cap в pyproject до <2.14), pydantic-settings→2.14.2, structlog 25.5→26.1 (cap <27), pytest 9.0.2→9.1.1, pytest-asyncio→1.4.0, ruff→0.15.20 (диапазон уже покрывает); обновить README-упоминания; полный прогон тестов; при любом падении — откат пина виновника

## Верификация (verification-before-completion)

- [ ] Полный `python -m pytest tests/ -q` — 0 failed (ожидаемо >430 тестов)
- [ ] `make lint` (ruff) — чисто
- [ ] Все чекбоксы этого плана отмечены; статусы в отчётах 01-10 обновлены
- [ ] grep по удалённым символам (grab_music_release, get_search_results, force_check, _parse_album, AlbumInfo, t_recheck, detect_content_type) — 0 вхождений
- [ ] Коммиты по волнам; деплой на rpie4 (`git pull && docker compose build && up -d`), проверка `docker logs` на старте — нет ошибок, healthcheck healthy
- [ ] Смоук на проде: /status, поиск, /downloads

## Refactoring (deferred — отдельный PR, НЕ в этом цикле)

⚠️ Не выполняется автоматически.

- [ ] LOGIC-11: перенос `_resolve_folder`/`_resolve_profile`/TVDB-резолва в AddService (4 копии в 3 хендлерах)
- [ ] LOGIC-18a: инверсия services→ui (Formatters из notification_service — через колбэк)
- [ ] LOGIC-19: сигнатуры 6+ аргументов (format_emby_status(EmbyServerInfo) и др.)
- [ ] LOGIC-21: единый bounded-dict util для module-level кэшей
- [ ] Полная миграция CallbackData: `settings:*`, `emby_*`, `trend_*`, `cal_*`, `rel:`, `type:`, `season_*`, `noop` (карта — в 05-logic-issues.md)
- [ ] Промежуточный шаг к ArrBaseClient: generic push_release/get_profiles/get_root_folders в BaseAPIClient через `_api_prefix`
- [ ] Унификация grab_movie/series_release шаблонным методом (~150×2 строк)
- [ ] God-file split: search.py (1009), formatters.py (1005), keyboards.py (925)
- [ ] PERF-02 full: qBittorrent `sync/maindata` delta-протокол
- [ ] PERF-04 full: результаты по search_id вместо дублирования в сессии
- [ ] DEP-02: lock-файл (uv/pip-tools) + `--require-hashes` в Dockerfile — требует решения об инструменте
- [ ] DEP-05b: mypy 2.x — осознанное решение об апгрейде мажора
