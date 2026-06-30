# План исправлений TG_arr — раунд 4 (2026-06-30)

Формат — `superpowers:writing-plans`: фазы, чекбоксы, пометка TDD для поведенческих фиксов.
Правило `default-full-fix`: чинится всё, кроме архитектурного рефакторинга (вынесен в отложенный раздел).
> ✅ **Раунд 4 — применено (TDD):** SEC-01, SEC-02, SEC-03, BUG-01, RACE-01, RACE-02/DB-01. Тесты: 291 passed, ruff clean.


Дедупликация корней:
- **RACE-02 ≡ DB-01** — одна правка (write-lock на соединение БД) закрывает обе.
- **LOGIC-05 ≡ PERF-03** — одна правка (параллельный календарь) закрывает обе.

---

## Fixes (этот цикл)

### Phase 1 — Быстрые независимые правки (параллелятся, без поведенческих рисков)

Безопасность (escape — поведение «не падать», но без логики):
- [x] **SEC-01**: `html.escape(torrent.name)` в `cmd_pause`/`cmd_resume` ([downloads.py:132](../bot/handlers/downloads.py#L132), :160). Добавить `import html`. *(TDD: тест с именем `A & B`)*
- [x] **SEC-02**: `html.escape(...)` для тайтлов в `handle_add_*_from_trending` ([trending.py:358](../bot/handlers/trending.py#L358), :467). Добавить `import html`. *(TDD)*
- [x] **SEC-03**: убрать `result=` из `log.info("Push release result", ...)` ([add_service.py:357](../bot/services/add_service.py#L357), :509, :710) — логировать только `approved`/`rejections`; опц. сделать `_mask_tokens` рекурсивным + маскировать query-параметры URL.

Мёртвый код (удаление, без TDD — суит ловит регрессии):
- [ ] **DEAD-01**: либо удалить `bot/constants.py`, либо сделать его единственным источником и убрать дубли (`TORRENTS_PER_PAGE` в downloads.py:19, `MAX_QUERY_LENGTH` в search.py:138 и music.py:99, литерал `8640000` в models.py:385).
- [ ] **DEAD-02**: удалить `ProwlarrClient.grab_release` ([prowlarr.py:447](../bot/clients/prowlarr.py#L447)).
- [ ] **DEAD-03/04**: удалить `LidarrClient.lookup_album`/`search_album` + `Formatters.format_album_info` (решить судьбу `_parse_album`/`AlbumInfo` — оставить для календаря или дропнуть).
- [ ] **DEAD-05**: удалить `SonarrClient.lookup_series_by_tvdb`.
- [ ] **DEAD-06**: удалить `DeezerClient.get_trending_albums`.
- [ ] **DEAD-07**: удалить `Database.get_search_results` (или начать использовать таблицу — см. DB-03).
- [ ] **DEAD-08/09**: пометить test-only методы (`ScoringService.get_best_result`/`filter_by_quality`, `NotificationService.force_check`/`get_stats`/`unsubscribe_user`) — удалить или покрыть.
- [ ] **DEAD-10**: удалить `UserPreferences.language` (нигде не читается).
- [ ] **DEAD-11**: удалить/начать читать `SearchSession.monitor_type` (пишется, не читается — реальная логика monitor_type считается в search.py).
- [ ] **DEAD-12**: удалить wrapper `detect_content_type` (test-only) либо использовать в тестах через `detect_with_confidence`.
- [ ] **DEAD-13**: убрать мёртвый `hasattr(r, "get_size_gb")` на типизированном `SearchResult` ([search_service.py:301](../bot/services/search_service.py#L301)).

Зависимости / документация:
- [ ] **DEP-01**: `make dev` должен ставить dev-зависимости (`pip install -e ".[dev]"` или `-r requirements-dev.txt`).
- [ ] **DEP-02**: убрать `orjson` из README (удалён, не используется).
- [ ] **DEP-03**: синхронизировать версию aiogram в README (3.26.0 → 3.27.0).
- [ ] **DEP-04**: согласовать dev-зависимости между `pyproject [dev]` и `requirements-dev.txt`.

Deploy / IaC:
- [ ] **DEPLOY-02**: добавить `LIDARR_URL`/`LIDARR_API_KEY`/`DEEZER_ENABLED` в `docker-compose.yml`.
- [ ] **DEPLOY-03**: запинить базовый образ по digest (`python:3.12-slim@sha256:...`).
- [ ] **DEPLOY-04**: исправить вводящий в заблуждение комментарий про Swarm в compose (лимиты ресурсов активны в Compose V2).
- [ ] **DEPLOY-05**: починить `docker-compose.dev.yml` (volume `bot-data` объявлен только в prod-файле → dev `up` падает).

Observability (только логи, визуальная проверка):
- [ ] **OBS-01**: логировать неудачу login qBittorrent ([qbittorrent.py:122](../bot/clients/qbittorrent.py#L122)).
- [ ] **OBS-02**: тайминг/slow-call инструментацию в `_post_no_retry` ([base.py:256](../bot/clients/base.py#L256)).
- [ ] **OBS-03**: персистить rejection-детали в `ActionLog.details`.
- [ ] **OBS-04**: логировать fallback версии API в pause/resume (404→старый эндпоинт).
- [ ] **OBS-05**: логировать re-auth по 403 в `_request`.

### Phase 2 — Поведенческие правки (TDD: RED→GREEN→REFACTOR, последовательно где зависят)

- [x] **RACE-01** (critical): защита от двойного grab. Per-user in-progress guard ИЛИ атомарный claim сессии (delete в начале хендлера). Применить к `handle_confirm_grab`/`handle_grab_best`/`handle_force_grab`/`handle_confirm_music_add`. *(TDD: два конкурентных вызова → один grab)*
- [x] **RACE-02 / DB-01** (high): `asyncio.Lock` на запись в `Database`; обернуть `save_search` и `cleanup_old_searches` (и желательно все write-блоки). *(TDD: конкурентные `save_search` + `cleanup` не падают, ничего не теряется)*
- [x] **BUG-01** (high): отдельный `CallbackData.TRENDING_BACK` + хендлер в trending.py, ИЛИ `handle_back` пропускает (return без answer) при пустой сессии. *(TDD)*
- [ ] **RACE-04** (med): атомарность мутаций сессии — version-токен или UPDATE-only-if-exists, чтобы slow-callback не воскрешал удалённую сессию. *(TDD; зависит от RACE-02 lock)*
- [ ] **BUG-04** (low): одиночный сезон — `monitor_type="none"` + monitored только на целевом сезоне ([search.py:726](../bot/handlers/search.py#L726)). *(TDD)*
- [ ] **BUG-05 / TEST-05** (low, PLAUSIBLE): `add_torrent_url` — считать любой 2xx без `Fails.` успехом ([qbittorrent.py:422](../bot/clients/qbittorrent.py#L422)). *(TDD: пустое тело 200 → True)*
- [ ] **BUG-02** (low): leechers=0 не должен превращаться в None ([prowlarr.py:183](../bot/clients/prowlarr.py#L183)) — явная проверка `is not None`. *(TDD)*
- [ ] **LOGIC-01** (med): REMUX-бонус (+30) недостижим без source-токена — учитывать `is_remux` независимо от `quality.source` ([scoring.py:138](../bot/services/scoring.py#L138)). *(TDD)*
- [ ] **LOGIC-03** (low): язык-пенальти (`ita`/`french`/…) ложно срабатывает на легитимных тайтлах — ограничить контекстом релиз-тегов. *(TDD)*

### Phase 3 — Производительность и БД (rpie4)

- [ ] **PERF-01**: pause/resume/delete тянут полный список торрентов 2–3×/клик — переиспользовать один fetch / точечный запрос ([qbittorrent.py:303](../bot/clients/qbittorrent.py#L303)).
- [ ] **LOGIC-05 / PERF-03**: календарь Sonarr+Radarr+Lidarr через `asyncio.gather` ([calendar.py:48](../bot/handlers/calendar.py#L48)).
- [ ] **PERF-04**: Emby-статус — параллелить 3 round-trip ([emby.py:35](../bot/handlers/emby.py#L35)).
- [ ] **PERF-05**: `qBittorrent.get_status()` — 4 последовательных вызова → gather ([qbittorrent.py:218](../bot/clients/qbittorrent.py#L218)).
- [ ] **PERF-02**: poller уведомлений — тянуть только нужные поля (`filter`/projection) каждые 60s.
- [ ] **PERF-06**: пагинация/back пере-сериализуют всю сессию в SQLite на каждый тап — писать только при изменении (или кэш в памяти). Связано с износом SD-карты (DB).
- [ ] **PERF-07**: trending detail/add повторно делает полный lookup — кэшировать выбранный объект в сессии.
- [ ] **DB-02**: добавить cleanup таблицы `actions` (растёт без ограничений) в `_periodic_cleanup`.
- [ ] **DB-03**: либо начать читать `search_results`, либо перестать писать (write-only blob на SD-карту).
- [ ] **DB-04** (PLAUSIBLE): `wal_checkpoint(TRUNCATE)` в `close()`.
- [ ] **DB-07** (PLAUSIBLE): `log_action` тип `-> int`, но `lastrowid` может быть `None` — поправить аннотацию/контракт.
- [ ] **RACE-05** (med): инжектить registry-singleton qBittorrent в `NotificationService` (а не второй клиент); подписывать новых юзеров из AuthMiddleware. *(вторую часть можно отнести к Phase 2)*

### Phase 4 — Тесты (the fix IS the test)

- [ ] **TEST-01**: тесты Auth/RateLimit middleware + `is_user_allowed`/`is_admin`.
- [ ] **TEST-02**: re-auth по 403 в `_request` (state machine qBittorrent).
- [ ] **TEST-03**: poller уведомлений (`_monitor_loop`/`_initial_sync`/`_check_for_completions`).
- [ ] **TEST-04**: grab happy-path (push-approved / direct grab / qBit fallback).
- [ ] **TEST-06** (PLAUSIBLE): tie-break и music-demotion в `detect_with_confidence`.
- [ ] **TEST-07**: авто-удаление повреждённой сессии в `get_session`.

---

## Refactoring (отложено — отдельный PR, отдельная сессия)

⚠️ НЕ выполняется в этом цикле. Запуск: `project-audit fix-refactor` или явный запрос.

- [ ] **LOGIC-06**: унифицировать `grab_*_release` (movie/series/music дублируют push→grab→qBit-fallback 3×) в один параметризованный flow.
- [ ] Разбить god-файлы: `ui/formatters.py` (1038), `ui/keyboards.py` (867), `handlers/search.py` (865), `services/add_service.py` (773).
- [ ] Извлечь `ArrBaseClient` для Radarr/Sonarr/Lidarr (дублирование lookup/add/grab/profiles/root-folders).
- [ ] Решение по album-фиче Lidarr: удалить целиком (модель+клиент+UI) или довести до рабочего флоу (связано с DEAD-03/04).
- [ ] Мигрировать состояние сессии на aiogram FSM (вместо своей таблицы `sessions`) — упростит RACE-01/04.
