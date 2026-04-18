# Fix Plan — TG_arr (Round 2)

Дата: 2026-04-18. Формат: `superpowers:writing-plans`.

Критерий deferred: diff ≥4 файла с moves/renames и не фиксит конкретный баг. Всё остальное — в Fixes.

---

## Fixes (this cycle)

### Phase 1 — Quick wins (параллелизуемо)

Независимые правки, которые можно делать без координации.

#### 1.1 Sanitize user-facing error messages (SEC-02, SEC-13)

- `bot/handlers/trending.py:109-111, 158-160, 188-189, 302-304, 360-361` — заменить `f"❌ Ошибка: {html.escape(str(e))}"` на `Formatters.format_error("Не удалось загрузить популярные фильмы/сериалы")`.
- `logger.error(...)` уже есть — оставляем.
- Verify: unit-тест на handler (respx mock exception) — сообщение пользователю не содержит `str(e)`.

#### 1.2 SQLite PRAGMA улучшения (DB-02, DB-04)

- `bot/db.py:35-50 connect()` — после `aiosqlite.connect(...)`:

  ```python
  await self._connection.execute("PRAGMA journal_mode=WAL")
  await self._connection.execute("PRAGMA foreign_keys=ON")
  await self._connection.execute("PRAGMA synchronous=NORMAL")
  ```

- Verify: existing `test_db.py` должны пройти. Добавить тест `test_pragma_wal_enabled`.

#### 1.3 Dependency bumps (DEP-02, DEP-05, DEP-06, DEP-07)

- `requirements.txt`: aiogram 3.26.0 → 3.27.0, pydantic 2.12.5 → 2.13.2.
- `pyproject.toml`: `structlog>=24.4,<26`, `aiosqlite>=0.20,<2`.
- Verify: `make test` зелёный.

#### 1.4 Dead code cleanup (DEAD-04/31, DEAD-07, DEAD-08, DEAD-09..15, DEAD-19, DEAD-29)

- Удалить `Keyboards.series_list` в `bot/ui/keyboards.py:235-280` (использует несуществующий `CallbackData.SERIES` — crash-if-called).
- Применить `ScoringService.filter_by_quality(preferred_resolution=...)` в `SearchService.search_releases` до `sort_results` (DEAD-07 — fix broken feature).
- Удалить неиспользуемые: `SearchService.get_artist_by_mbid/get_movie_by_tmdb/get_series_by_tvdb/lookup_album`, `DeezerClient.search_artist`, `LidarrClient.get_all_artists`, `EmbyClient.get_scheduled_tasks`, `Formatters.format_torrent_compact`, `NotificationService.force_check`.
- Verify: `ruff check bot/` + `pytest tests/` зелёные.

#### 1.5 Dockerfile slim + multi-stage (DEPLOY-02, DEPLOY-03, DEAD-27)

- Переписать Dockerfile с builder-stage без `gcc` в финальном runtime:

  ```dockerfile
  FROM python:3.12-slim AS builder
  WORKDIR /build
  COPY requirements.txt .
  RUN pip install --user --no-cache-dir -r requirements.txt

  FROM python:3.12-slim
  COPY --from=builder /root/.local /home/botuser/.local
  ENV PATH=/home/botuser/.local/bin:$PATH
  ...
  ```

- Verify: `docker build` успешен, image size -100..-150MB.

#### 1.6 Docker compose hardening (DEPLOY-05, DEPLOY-12)

- Переименовать `docker-compose.override.yml` → `docker-compose.dev.yml` (не автоподхватывается).
- Добавить в `docker-compose.yml`:

  ```yaml
  logging:
    driver: "json-file"
    options:
      max-size: "10m"
      max-file: "3"
  stop_grace_period: 30s
  ```

- Update Makefile `docker-up` → `docker compose -f docker-compose.yml up -d`; добавить `docker-up-dev` с `-f docker-compose.dev.yml`.
- Verify: deploy на staging, override не применяется автоматом.

#### 1.7 Telegram token masking в logs (SEC-03)

- `bot/main.py:setup_logging` — добавить structlog processor, который маскирует `/bot[0-9]+:[A-Za-z0-9_-]+/` в любом строковом поле события.
- Verify: запустить с `LOG_LEVEL=DEBUG`, проверить что токен не появляется.

#### 1.8 Constants extraction (LOGIC-08)

- Создать `bot/constants.py` с `MAX_MESSAGE_LENGTH=4096`, `SAFE_MESSAGE_LENGTH=3800`, `TORRENTS_PER_PAGE=5`, `MAX_QUERY_LENGTH=200`, `PROWLARR_SEARCH_LIMIT=100`, `TRENDING_LIMIT=10`, `SESSION_RESULTS_MAX=500`, `SESSION_TTL_HOURS=24`, `SEARCH_HISTORY_DAYS=7`, `QBT_ETA_INFINITY=8640000`.
- Impact: ~12 файлов, но каждое изменение — 1 строка.
- Verify: tests pass.

### Phase 2 — Behaviour changes (TDD)

Пишем тест first, потом фикс.

#### 2.1 Fix music vs search CONFIRM_GRAB router conflict (BUG-27) — CRITICAL

- Test first: `tests/test_handlers_grab.py::test_confirm_grab_movie_flow_after_music_router_registered` — проверить что при `selected_content=MovieInfo` вызывается movie-grab, не music.
- Refactor: вместо двух `@router.callback_query(F.data == CallbackData.CONFIRM_GRAB)` — **один** handler, который dispatch'ит по `session.selected_content` type.
- Move `_handle_confirm_music_add` в общий handler'е `handle_confirm_grab` в новом файле `bot/handlers/_grab_dispatch.py`.
- Verify: music path, movie path, series path — все работают.

#### 2.2 Fix recursive callback in downloads (BUG-15)

- Test first: `test_pause_torrent_no_double_answer` — `callback.answer` вызван ровно один раз.
- Refactor: вынести общий `_render_torrent_details(callback, torrent, answer=False)` helper, который **не** вызывает `callback.answer`. `handle_pause_torrent` после действия вызывает его напрямую.
- Apply аналогично к `handle_resume_torrent`, `handle_delete_torrent`, `handle_delete_with_files`.
- Verify: manual test через Telegram — pause/resume не дают `query is too old`.

#### 2.3 SSRF валидация для push_release (SEC-16) — HIGH

- Test first: `test_push_release_rejects_private_url` — `grab_movie_release` с `download_url=http://192.168.1.1/x` возвращает `(False, action, "Небезопасный URL")`.
- Fix: в `add_service.grab_movie_release/grab_series_release/grab_music_release` — вызвать `await _validate_download_url(release.download_url)` **до** `radarr.push_release` (и перед direct grab). Если не валидно — пропустить push_release, сразу fallback.
- Verify: tests pass, add IPv6 test case (SSRF-IPv6 edge).

#### 2.4 Liveness healthcheck (SEC-14 / DEPLOY-04) — HIGH

- Test first (manual scenario): kill polling task, контейнер должен стать unhealthy за 2 мин.
- Fix: в `bot/main.py` добавить asyncio task:

  ```python
  async def _liveness_touch():
      while True:
          Path("/tmp/alive").touch()
          await asyncio.sleep(30)
  asyncio.create_task(_liveness_touch())
  ```

- Dockerfile:

  ```dockerfile
  HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
      CMD ["sh", "-c", "find /tmp/alive -mmin -2 | grep -q alive"]
  ```

- Verify: SIGSTOP бота → healthcheck failing за 2 мин.

#### 2.5 Monitor type fix for single episode (BUG-32)

- Test first: `test_grab_single_episode_sets_monitor_existing` — grab S01E05 → Sonarr add с `monitor_type="existing"`, а не `"none"`.
- Fix: `bot/handlers/search.py:617-622`:

  ```python
  if force_download:
      monitor_type = "all"
  elif result.is_season_pack:
      monitor_type = "all"
  elif result.detected_season is not None:
      monitor_type = "existing"  # single ep — monitor future eps in same season
  else:
      monitor_type = "all"
  ```

- Verify: test pass.

#### 2.6 Structlog contextvars в middleware (OBS-01, OBS-07)

- Test first: `test_logging_middleware_binds_user_id_to_contextvars`.
- Fix: `bot/middleware/auth.py:LoggingMiddleware.__call__` — `structlog.contextvars.bind_contextvars(user_id=..., chat_id=..., request_id=uuid.uuid4().hex[:8])`; `clear_contextvars()` в finally.
- Удалить ручной `log.bind` в handler'ах — уже из contextvars.
- Verify: JSON log `bot/handlers/downloads.py:error` содержит `user_id`, `request_id`.

#### 2.7 Migration framework (DB-01)

- Test first: `test_migration_adds_column_to_v2`.
- Fix: в `_create_tables` — `PRAGMA user_version`, `if v < 1: create tables; set user_version=1`. Future migrations в `_migrate_v1_to_v2()`.
- Документация в `bot/db.py` docstring.
- Verify: existing БД с v0 апгрейдит до v1 без ошибок.

#### 2.8 Session corruption recovery (BUG-24, SEC-19, BUG-35)

- Test first: `test_get_user_falls_back_on_corrupt_preferences`; `test_get_session_truncates_when_results_exceed_max`.
- Fix:
  - `db.py:_row_to_user` — `try: UserPreferences(**prefs_data); except Exception: log warn + default`.
  - `db.py:save_session` — truncate `session.results[:500]` перед сохранением.
  - `SearchSession` добавить `schema_version: int = 1`.
- Verify: tests pass.

#### 2.9 Rejected release: honest feedback (BUG-14)

- Test first: `test_grab_reports_fallback_search_not_grabbed`.
- Fix: `add_service.grab_*` — возвращать `(True, action, "Выбранный релиз не был принят. Запущен автопоиск")` явно, не "Запущен автопоиск" без объяснения.
- Verify: manual UX check.

#### 2.10 Timezone-aware календарь (BUG-11)

- Test first: `test_calendar_displays_next_day_for_late_utc_episodes` — episode air at `22:00 UTC` (= 01:00 MSK next day) отображается как next-day в MSK.
- Fix: в `formatters._extract_date_key` — convert to `settings.timezone` via `zoneinfo.ZoneInfo(settings.timezone)` до `strftime`.
- Verify: test pass.

#### 2.11 Safe message truncation (BUG-12)

- Test first: `test_long_calendar_truncation_does_not_break_html`.
- Fix: helper `bot/ui/formatters.py::_safe_truncate_html(text, max_len)` — корректно закрывает открытые теги.
- Apply в `format_calendar`, `format_torrent_list`, `format_action_log`.
- Verify: Telegram `can't parse entities` не кидается.

#### 2.12 qBit v5 state mapping (BUG-29)

- Test first: `test_parse_torrent_v5_stopped_state`.
- Fix: расширить `STATE_MAP` в `qbittorrent.py` — `stoppedDL → PAUSED`, `stoppedUP → PAUSED`, `running → DOWNLOADING`.
- Verify: test pass.

#### 2.13 Performance: torrent pagination (PERF-02, PERF-08, PERF-09, PERF-10)

- Test first: `test_pagination_does_not_fetch_all_torrents` — при page=2 делается запрос с `offset=5, limit=5`.
- Fix:
  - `bot/handlers/downloads.py:handle_page` — использовать `qbt.get_torrents(limit=5, offset=page*5)`, отдельный запрос `get_status()` для `total_torrents`.
  - TTL-кэш в handler-слое: `QBT_CACHE_TTL=3s` (dict с timestamp).
- Verify: unit + manual (большой qBit).

#### 2.14 Lidarr monitorNewItems fix (SEC-17)

- Test first: `test_add_artist_respects_monitor_none`.
- Fix: `bot/clients/lidarr.py:107-119` — удалить `"monitorNewItems": "all"` или брать из `monitor` параметра.
- Verify: test pass.

#### 2.15 Search query mismatch (LOGIC-09)

- Test first: `test_search_uses_parsed_title_without_year`.
- Fix: `bot/handlers/search.py:193` — передавать `parsed["title"]` в `search_releases`, не оригинал.
- Verify: Prowlarr запросы без `2021` в query-param.

### Phase 3 — Behaviour-preserving cleanup inside one module

Без изменения API/поведения.

#### 3.1 Status handler: консолидация check_service (BUG-31, DEAD, LOGIC-19)

- Файл: `bot/handlers/status.py`. Удалить `check_qbittorrent` (duplicate of `check_service`). Использовать `check_service(qbittorrent, "qBittorrent")`.

#### 3.2 Precompile regex в scoring (PERF-05)

- Файл: `bot/services/scoring.py`. В `ScoringWeights.__post_init__` предкомпилировать `_bad_keyword_patterns: list[tuple[re.Pattern, int]]`. Использовать в `calculate_score`.

#### 3.3 Short-circuit detect_content_type (PERF-06, LOGIC-10)

- Файл: `bot/services/search_service.py:detect_content_type`. Добавить heuristic: `if len(query) < 4: return ContentType.UNKNOWN`.

#### 3.4 Scoring singleton (PERF-04)

- Файл: `bot/handlers/search.py` + `bot/handlers/music.py`. `_SCORING = ScoringService()` на module level. В `get_services` использовать.

#### 3.5 Mutate calculated_score без model_copy (PERF-14)

- Файл: `bot/services/scoring.py:sort_results`. Вместо `model_copy(update=...)` — `r.calculated_score = score` (убедиться что `SearchResult` mutable).

#### 3.6 Safe edit_text helper (OBS-10)

- Файл: новый `bot/ui/telegram_helpers.py::safe_edit_text(message, text, **kwargs)`. Логирует non-"not modified" `TelegramBadRequest`. Используется во всех handler'ах где сейчас `try: edit_text; except TelegramBadRequest: ...`.
- Это ~6 мест, все в одном module-pattern → phase 3, без моих behaviour change.

#### 3.7 Unbounded music caches (PERF-03, SEC-08)

- Файл: `bot/handlers/music.py`. Добавить `_MAX_CANDIDATES=100` + cleanup при overflow (как в `trending.py`).

#### 3.8 Remove `data/.gitkeep` COPY (DEPLOY-10)

- Файл: `Dockerfile`. Убрать `COPY data/.gitkeep ./data/` — `bot-data` volume всё равно перекрывает.

#### 3.9 structlog WARNING vs ERROR разделение (OBS-04)

- Файл: `bot/clients/qbittorrent.py`. Заменить retry-attempt `log.error` на `log.warning`.

#### 3.10 Audit-log расширение (OBS-06)

- Файл: `bot/models.py` + `bot/db.py`. Добавить `ActionLog.details: Optional[str] = None` (JSON). `log_action` сохраняет `rejections`, `fallback_used`. Migration DB-01 нужен.

---

## Refactoring (deferred — отдельный PR)

Архитектурные рефакторинги, которые трогают ≥4 файлов и не фиксят конкретный баг. Требуют отдельного review + полного регресс-теста.

### R1 — Split `bot/ui/formatters.py` god-file (LOGIC-01)

- `bot/ui/formatters/` package: `common.py`, `search.py`, `torrent.py`, `media.py`, `calendar.py`, `music.py`, `emby.py`.
- Re-export из `__init__.py` для backward-compat (`Formatters` aggregate-class).

### R2 — Split `bot/ui/keyboards.py` god-file (LOGIC-02)

- Аналогично: `bot/ui/keyboards/` package.

### R3 — Unified GrabOrchestrator (LOGIC-03, LOGIC-05)

- Выделить `bot/services/grab_orchestrator.py` с dispatch по `content_type`.
- `grab_movie_release` / `grab_series_release` / `grab_music_release` — становятся тонкими wrapper'ами.
- `search.py._execute_grab` + `music.py._handle_confirm_music_add` сливаются.

### R4 — ArrBaseClient (LOGIC-04)

- Выделить `bot/clients/_arr_base.py` с `_parse_images`, `_parse_ratings`, `check_connection` template-methods.
- `RadarrClient`, `SonarrClient`, `LidarrClient` наследуются.

### R5 — Split handlers if needed (LOGIC-03 extension)

- После R3 некоторые handler'ы можно разбить по sub-flows: `handlers/search/` directory (entry, pagination, grab).
- Может быть отдельным PR после R3.

---

## Порядок выполнения

1. **Sprint 1**: Phase 1 (parallelizable quick wins) — 1-2 дня. Результат: SEC-02/03, deps, dead code, Dockerfile multi-stage, DB pragma, compose hardening.
2. **Sprint 2**: Phase 2 (TDD, критические баги) — 3-5 дней. Сначала BUG-27, BUG-15 (blocking bugs), потом SEC-16, SEC-14 (security), потом migration (DB-01), потом остальные.
3. **Sprint 3**: Phase 3 (cleanup) — 1-2 дня.
4. **Sprint 4** (отдельный PR): Refactoring R1..R5 — 3-5 дней, только после того как Phase 2 стабилизировался.

## Тесты как exit-criteria

- После Sprint 2: `pytest tests/ --cov=bot --cov-fail-under=50` (сейчас ~35%).
- После Sprint 3: `pytest tests/ --cov=bot --cov-fail-under=55`.
- После Sprint 4: `pytest tests/ --cov=bot --cov-fail-under=60`.
