# Observability TG_arr v1.0 (раунд 3)

Дата: 2026-05-08. Reviewer: Claude (Opus 4.7).
Контекст: жалоба юзера «плохо ищет, не понимает контент» — главный вопрос: **можно ли по prod-логам отдиагностировать конкретный инцидент поиска?**

Stack: structlog 25.5.0, Python 3.12, JSON-renderer (INFO+), Docker `json-file` (max-size 10m, max-file 3). Metrics нет, tracing нет, error-sampling нет. Healthcheck — touch-file `/tmp/tgarr-alive` + OS-thread watchdog (`bot/main.py:40-65`).

> Прошлый отчёт (`analysis_round2/07-observability.md`) зафиксировал OBS-01 .. OBS-10. Перепроверка показала: **ни одна из находок не исправлена в коде.** Все статусы ниже это подтверждают и фокусируются на сценарии «плохо ищет».

---

## Главный вывод по жалобе юзера

**Сценарий «плохо ищет, не понимает контент» по текущим логам отлаживается на 30%.**

Что есть в JSON-логе INFO для одного поискового цикла (`process_search`):
1. `LoggingMiddleware: Incoming event` — `event_type=message, user_id, chat_id, text=<query[:50]>` — но **только на DEBUG** (`auth.py:125`), в prod (LOG_LEVEL=INFO) полностью отсутствует.
2. `Parsed query parsed={...}` — `search.py:152`, есть.
3. `detect_content_type` — параллельные lookup'ы Radarr/Sonarr/Lidarr логируются на INFO внутри клиентов (`Looking up movie/series/artist in <X>` + `Lookup completed <type>_count=N`). Но **итоговый winner НЕ логируется** — `search_service.detect_content_type` (`search_service.py:36-109`) возвращает `ContentType` молча. Если detection ошибся (movie вместо series или UNKNOWN), по логам не видно, какие кандидаты были и почему не сматчилось через `_title_matches`.
4. `Searching Prowlarr query=… content_type=…` (`prowlarr.py:63`) + `Search completed result_count=N` (`prowlarr.py:81`).
5. `Search completed result_count=N` второй раз из service (`search_service.py:169`) — после фильтрации по `detected_type`, но **без сохранения "до фильтра"**. Если Prowlarr вернул 50 результатов, а после фильтра 0 — видно «50 → 0», но не видно почему: какие `detected_type` были у отброшенных.

Что **не** логируется и нужно для диагностики:
- Финальный winner `detect_content_type` (movie/series/music/unknown).
- Какие кандидаты Radarr/Sonarr/Lidarr вернули в `detect_content_type` (`movies[:3]`, `series[:3]`, `artists[:3]` — их title/year, прошёл ли `_title_matches`).
- Сколько результатов было до и после фильтра по `detected_type` (`search_service.py:163-164`).
- Top-N результатов с их `calculated_score` после ранжирования (для дебага «почему этот релиз первый»).
- `parsed["title"]` (cleaned) vs `query` — что реально полетело в Prowlarr.

В `parsed=parsed` есть `title`, но это сырой dict — в JSON-логе видно, но без явных bind'ов.

---

## Критические пробелы

### OBS-11 (HIGH) — `detect_content_type` не логирует winner и кандидатов
- **Файл**: `bot/services/search_service.py:36-109`.
- **Проблема**: функция возвращает `ContentType.MOVIE/SERIES/MUSIC/UNKNOWN` без логирования (а) что она решила и (б) почему. Lookup'ы в Radarr/Sonarr/Lidarr логируются, но это «N штук вернули». Нет лога:
  - Какие top-3 кандидата проверялись через `_title_matches`.
  - Какой кандидат сматчился (или ни один).
  - Какой regex-pattern сработал (если series detected by pattern на :64-66).
- **Влияние на дебаг**: главная причина невозможности отладить «не понимает контент». Юзер ввёл «Дюна 2021» — бот ответил «фильм или сериал?». По логам видно `Lookup completed movie_count=20`, но **не видно**, что ни один из 20 не прошёл `_title_matches('дюна 2021', 'Dune', 2021)` из-за кириллица-vs-латиница (это реальный баг, см. LOGIC категорию).
- **Решение**: в начале и в конце `detect_content_type` `log = logger.bind(query=query)`; перед каждым `return` — `log.info("content_type_detected", winner=..., reason=..., movie_titles=[...], series_titles=[...], artist_titles=[...])`; в `_title_matches` опционально DEBUG-лог на каждый сравниваемый кандидат.
- **Статус**: [ ]

### OBS-12 (HIGH) — `search_releases` теряет данные о фильтрации по detected_type
- **Файл**: `bot/services/search_service.py:163-170`.
- **Проблема**: 
  ```python
  if content_type != ContentType.UNKNOWN:
      results = [r for r in results if r.detected_type == content_type or r.detected_type == ContentType.UNKNOWN]
  ```
  Молча отбрасывает результаты. Финальный лог `Search completed result_count=N` (`:169`) показывает только число **после** фильтра. Если Prowlarr вернул 50, а фильтр оставил 5 — невозможно понять почему без отдельного логирования.
- **Влияние на дебаг**: при жалобе «мало результатов» нужно знать, отбросил ли фильтр (тогда баг в `_normalize_result.detected_type`) или Prowlarr реально мало вернул.
- **Решение**: добавить `log.info("filtered_by_type", before=N_raw, after=N_filtered, dropped_types=Counter([r.detected_type for r in raw if r not in results]))`.
- **Статус**: [ ]

### OBS-13 (HIGH) — Top-результаты с score не логируются перед показом юзеру
- **Файл**: `bot/handlers/search.py:201-256`.
- **Проблема**: `results = await search_service.search_releases(...)` → `scoring.sort_results` (внутри service) → выводится юзеру. Промежуточно нет лога вида `top_3_results=[{title, score, indexer, seeders}, ...]`. При жалобе «опять плохой релиз первый» — невозможно проверить ранжирование.
- **Влияние на дебаг**: scoring (`bot/services/scoring.py`) — 30+ правил. Без top-N в логе оператор не может решить, баг ли это в `_parse_quality` (определил `source=CAM` неправильно) или в weights.
- **Решение**: после `search_releases` — `log.info("top_results", top=[{"title": r.title[:80], "score": r.calculated_score, "indexer": r.indexer, "seeders": r.seeders, "size_gb": r.get_size_gb(), "detected_type": r.detected_type.value} for r in results[:5]])`.
- **Статус**: [ ]

### OBS-14 (HIGH) — `process_search` не логирует ключевые ветки
- **Файл**: `bot/handlers/search.py:123-269`.
- **Проблема**: 
  - На входе нет `log.info("search_started", query=...)` — есть только `Parsed query parsed=...`.
  - Ветка «question_suffix» (`:172-190`) — show_music decision, partial session save — **ничего не логирует**. Юзер сообщает «бот спросил фильм/сериал», по логам не понять, что это была эта ветка.
  - Ветка `if not results` (`:203-208`) — «Ничего не найдено» — тоже без INFO-лога. У SearchService логируется `No results found`, но без user_id/query_clean.
  - Музыка handoff (`:165-170`) — `process_music_search` вызывается, но фактический «тип определён MUSIC» не логируется.
- **Влияние на дебаг**: невозможно по логам прочитать «состояние» поиска у конкретного юзера. Нет ни start-event, ни branch-event.
- **Решение**: `log.info("search_branch", branch="auto_detect_unknown" / "question_user" / "search_releases" / "music_handoff" / "no_results", ...)` на каждом `return`.
- **Статус**: [ ]

### OBS-01 (HIGH, сохраняется с раунда 2) — handlers вызывают module-level `logger` без bind'ов
- **Файлы**: 60+ строк (см. `Grep log\.error|logger\.error` выше).
- **Проблема**: `LoggingMiddleware` (`auth.py:99-133`) делает `log = logger.bind(user_id=...)`, но это **локальная** переменная — handler'ы обращаются к module-level `logger = structlog.get_logger()` и пишут без user_id/chat_id.
  - Примеры: `downloads.py:77,80,104,107,206,248,319,352,...`; `trending.py:107,158,189,212,266,306,367,476`; `calendar.py:48,54,61`; `settings.py:68,102,...`; `emby.py:75,87,134,138,...`; `history.py:42`; `music.py:265,296`; `search.py:268,468,678`.
  - Из ~60 error-логов в handlers только ~5 (search.py:152, 268; music.py:121; добавлены в раунде 2) имеют bind с user_id/query.
- **Влияние на дебаг**: при инциденте «X не работает у юзера Y» в логе видно `Failed to refresh error="..."` без user_id. Невозможно ответить «затронуло одного юзера или всех».
- **Решение**: добавить `structlog.contextvars.bind_contextvars(user_id=..., chat_id=..., request_id=uuid4().hex[:8])` в `LoggingMiddleware.__call__` (perosessor `merge_contextvars` уже подключён в `main.py:80`); `clear_contextvars()` в finally. Тогда **любой** `logger.error` подхватит контекст автоматически.
- **Статус**: [ ] (зафиксирован в раунде 2 как fix-plan, но не выполнен)

### OBS-06 (HIGH, сохраняется) — невозможно отладить grab-инцидент
- **Файлы**: `bot/services/add_service.py:278-426 (movie)`, `:428-584 (series)`, `:640-772 (music)`.
- **Проблема**: при «не добавился фильм X» в ActionLog (`bot/models.py`) сохраняется `release_title`, `error_message`, но **нет**: 
  - rejections-list от Radarr/Sonarr/Lidarr (есть в `log.warning("Release was not approved", reason=...)` — но это в stdout-log, а не в БД).
  - Какой fallback использовался (push → grab → qBit → search). Логи есть в коде (`add_service.py:350,373,393,415`), но скоррелировать их с конкретной user-action из БД нельзя без request_id.
  - download_url masked-вариант для post-mortem.
- **Влияние на дебаг**: оператор смотрит ActionLog, видит `success=false, error_message="Релиз отклонён: Quality not wanted"` — но не видит, что было до этого: попыток push было N, какой именно release был выбран, был ли fallback к qBit.
- **Решение**: расширить `ActionLog` полем `details: dict` (TEXT JSON в SQLite). Туда складывать `{"rejections": [...], "fallback_chain": ["push", "grab", "qbit"], "download_url_masked": "...", "indexer": "..."}`. Уже описано в раунде 2 — не сделано.
- **Статус**: [ ]

### OBS-15 (HIGH, NEW) — нет latency-логирования по этапам поиска
- **Файлы**: `bot/handlers/search.py:123-269`, `bot/services/search_service.py:36-170`.
- **Проблема**: BaseAPIClient логирует `elapsed_ms` per HTTP call, но **только на DEBUG** (`base.py:137-141`). На INFO (prod) — **ноль timing-данных**. Невозможно понять, какой этап тормозит:
  - parse_query (CPU, обычно <1ms — ок)
  - detect_content_type (3 параллельных API call — может быть 500ms-30s)
  - search_releases → prowlarr.search (timeout=60s — самое медленное)
  - scoring.sort_results (CPU, 100 results × 30 правил)
  - DB save_search/save_session/log_action
  - Telegram edit_text
- **Влияние на дебаг**: жалоба «бот тупит» — не отличить «Prowlarr 30s» от «scoring 5s» от «Telegram timeout». User-visible latency только в watchdog-stale event (но это уже catastrophe).
- **Решение**: на верхнем уровне `process_search` обернуть каждый этап `t0 = time.monotonic(); ...; log.info("stage_done", stage="detect_content_type", elapsed_ms=...)`. Либо `log.bind` накапливающий dict timings + один итоговый event.
- **Статус**: [ ]

---

## Средние

### OBS-02 (MED, сохраняется) — exc_info=True используется только в middleware
- **Файл**: только `bot/middleware/auth.py:132`.
- **Проблема**: 60+ `logger.error("...", error=str(e))` без `exc_info=True`. JSON-renderer теряет traceback. При production-инциденте оператор видит `error="'NoneType' object has no attribute 'tg_id'"` без линии и call-stack.
- **Решение**: для всех `except Exception as e: logger.error(...)` добавить `exc_info=True`. Либо сделать ProcessorL `structlog.processors.format_exc_info` + автоподхват из `event_dict["exc_info"]`.
- **Статус**: [ ]

### OBS-03 (MED, сохраняется) — нет метрик
- **Файлы**: проект в целом.
- **Проблема**: ни prometheus-client, ни statsd. Нельзя получить:
  - QPS по handler'у
  - p50/p95/p99 latency для Prowlarr/Radarr/Sonarr
  - error rate
  - search no_results rate (важно для жалобы юзера — если процент >20%, query-detection не работает)
- **Решение**: для 10-20-юзерного бота prometheus избыточен, но **минимум** — структурный JSON c `event="search_metric", outcome="no_results"|"results"|"music_handoff"|"question"` + offline-парсер `jq` или `goaccess`. Альтернатива: prometheus exporter на :9090 (image +5 MB).
- **Статус**: [ ] (deferred в раунде 2, OK)

### OBS-07 (MED, сохраняется) — нет request_id
- **Файл**: `bot/middleware/auth.py:99-133`.
- **Проблема**: один user-event = handler → service → 2-5 client'ов → DB. В логах десятки строк без корреляции. При двух одновременных запросах одного юзера логи перемешиваются.
- **Решение**: `bind_contextvars(request_id=uuid4().hex[:8])` в LoggingMiddleware (см. OBS-01).
- **Статус**: [ ]

### OBS-04 (MED, сохраняется) — qBittorrent client использует только `error`, нет WARNING для retry
- **Файл**: `bot/clients/qbittorrent.py:255-256, 419, 479`.
- **Проблема**: `BaseAPIClient` различает WARNING (retry) vs ERROR (final) — `base.py:157,177,181`. qBittorrent client (отдельная иерархия) кладёт всё в `error`, даже временные ошибки. В реальности retry tenacity работает (line 99-104), но логи не отличают «временно» от «финально».
- **Решение**: в `qbittorrent.py:_request` после exception — `log.warning(..., will_retry=True)` если попытка не последняя; `log.error(...)` только на финальном fail.
- **Статус**: [ ]

### OBS-16 (MED, NEW) — `BaseAPIClient.elapsed_ms` живёт только на DEBUG
- **Файл**: `bot/clients/base.py:137-141`.
- **Проблема**: timing per HTTP call залогирован, но `log.debug` — в prod (INFO) не виден. При этом API-latency — ключевая метрика для observability arr-стека (Radarr/Sonarr/Lidarr/Prowlarr медленные при тормозящем диске).
- **Влияние**: невозможно построить latency-distribution без переключения LOG_LEVEL=DEBUG (что небезопасно — SEC-03 token-leak уже один раз был).
- **Решение**: вынести в INFO с порогом: `if elapsed > 1000: log.warning("slow_api_call", elapsed_ms=...)` или дедуплицированный INFO ровно для health-summary каждые 60s.
- **Статус**: [ ]

### OBS-17 (MED, NEW) — `parse_query` не логирует, что было «съедено» из запроса
- **Файл**: `bot/services/search_service.py:202-258`.
- **Проблема**: parse_query экстрактит year/season/quality и оставляет cleaned title. `process_search:152` логирует `parsed=<dict>`, но **дальше используется** `parsed.get("title")` для Prowlarr (`search.py:200`). Если parser «съел» лишнее (например, число `2021` интерпретирует как год даже из «Top 2021 list»), реальный поисковый term будет другим. В логе видно `parsed.title="Top  list"` — но `query="Top 2021 list"` уже не сохранён в одном bind'е с этим title.
- **Решение**: `log.info("search_term", original=query, cleaned=parsed["title"], extracted={"year": ..., "season": ..., "quality": ...})`.
- **Статус**: [ ]

### OBS-18 (MED, NEW) — Жертва маскировки: `_mask_url` обрезает URL до 100 символов и теряет path
- **Файл**: `bot/services/add_service.py:38-55`.
- **Проблема**: `_mask_url(url, max_len=100)` обрезает до 100 байт **после** замены sensitive query params. Длинные magnet-URL и URL с tracker-path `https://tracker.example.org/path/with/many/segments?passkey=***&...` обрезаются — в логе видно начало, но **не tracker-path** (важен для определения «какой private tracker сглючил»).
- **Решение**: увеличить max_len до 200, или хранить в БД (ActionLog.details) полный masked URL без обрезания.
- **Статус**: [ ]

---

## Низкие

### OBS-05 (LOW, сохраняется) — нет error sampling/rate-limit
- **Файл**: `bot/middleware/auth.py:132`.
- **Проблема**: spam: при Prowlarr down каждый search → `exc_info=True` в logging-middleware → стек на каждый запрос. 5-10 юзеров × 30 req/min × full traceback = переполнение `max-size: 10m` за минуты, потеря старых событий.
- **Решение**: `structlog`-processor дедупликации по hash(event+error_type) с TTL 60s; для критичных оставить как есть.
- **Статус**: [ ]

### OBS-08 (LOW, сохраняется) — `notification_service._monitor_loop` без heartbeat
- **Файл**: `bot/services/notification_service.py:92-111`.
- **Проблема**: 60s loop. Если `qbt.get_torrents()` зависнет на 50s, watchdog-thread (`main.py:40`) отстрелит процесс через 120s — но в логе будет тишина 50s. heartbeat `tick {iter, duration_ms}` помог бы post-mortem.
- **Решение**: `log.debug("monitor_tick", ...)` каждые N итераций или `log.info("monitor_summary", checks=..., notifications_sent=..., elapsed_ms=...)` каждые 5 минут.
- **Статус**: [ ]

### OBS-09 (LOW, сохраняется) — `AuthMiddleware` создание-конфликт на DEBUG
- **Файл**: `bot/middleware/auth.py:84-89`.
- **Проблема**: race-condition при первом /start от двух concurrent updates. Conflict логируется DEBUG → не виден в prod. Если конфликт частит (>1/мин), это симптом DB-locking.
- **Решение**: WARNING + counter.
- **Статус**: [ ]

### OBS-10 (LOW, сохраняется) — `TelegramBadRequest "message is not modified"` глотается без счётчика
- **Файл**: `bot/handlers/downloads.py:202,243,272,293,578,619,660`.
- **Проблема**: 7+ повторений `if "message is not modified" not in str(e): raise`. Никаких counter/log. Если новый тип BadRequest появится (`message too long`, `entity not found`), будет crash без диагностики, что именно произошло.
- **Решение**: helper `_safe_edit(callback, ...)` с единым `log.warning("telegram_bad_request_handled", reason=str(e))` для не-fatal случаев.
- **Статус**: [ ]

### OBS-19 (LOW, NEW) — `LoggingMiddleware: Incoming event` на DEBUG, в prod невидимо
- **Файл**: `bot/middleware/auth.py:125`.
- **Проблема**: `log.debug("Incoming event")` — на INFO (prod) **полностью отсутствует** факт прихода update'а. Если кто-то сделал /search и **ничего не произошло** (handler-routing промахнулся), по логам этого не видно. Только если handler сам что-то логирует.
- **Решение**: повысить до INFO для message с command/text + callback_query (~10 событий/мин в whitelisted-боте — норм). Либо INFO для command+text events, DEBUG для всего остального.
- **Статус**: [ ]

### OBS-20 (LOW, NEW) — нет slow-query log для SQLite
- **Файлы**: `bot/db.py` (полный модуль).
- **Проблема**: SQLite в Docker volume на rpie4 (microSD/external HDD). При фрагментации/I/O-stall запросы к `searches`, `search_results`, `actions_log` могут идти 1-5s. Никакого timing.
- **Решение**: декоратор `@_timed_query` для критичных методов (`get_session`, `save_session`, `log_action`) → WARNING если >500ms.
- **Статус**: [ ]

### OBS-21 (LOW, NEW) — `prowlarr._normalize_result` молча отбрасывает items
- **Файл**: `bot/clients/prowlarr.py:88-99`.
- **Проблема**: 
  ```python
  guid = item.get("guid") or item.get("downloadUrl") or item.get("infoUrl") or ""
  if not guid: return None
  title = item.get("title") or item.get("fileName") or ""
  if not title: return None
  ```
  Если indexer вернул items без guid (бывает у некоторых private-trackers), они **молча** теряются. `Search completed result_count=N` — после фильтрации, без счётчика отброшенных.
- **Влияние**: жалоба «мало результатов» — может быть из-за этого, но в логе невидимо.
- **Решение**: счётчик `dropped_no_guid`, `dropped_no_title` → один summary-log на конце поиска.
- **Статус**: [ ]

### OBS-22 (LOW, NEW) — Telegram-команды не пишутся в БД ActionLog (только SEARCH/GRAB/ADD)
- **Файлы**: `bot/handlers/*.py`, `bot/models.py:ActionLog`.
- **Проблема**: ActionLog содержит SEARCH/GRAB/ADD типы. Действия `pause/resume/delete torrent`, `force_grab`, `cancel`, `back`, `change_settings` — **не логируются в БД**. В случае инцидента «у меня всё пропало» (юзер случайно сделал delete-with-files) restoration-аудит невозможен.
- **Решение**: расширить `ActionType` enum + `db.log_action` вызовы в `downloads.py:t_delete:, t_delf:, t_pause_all`. Достаточно `user_id, action_type, target=hash[:8]`.
- **Статус**: [ ]

### OBS-23 (LOW, NEW) — `_mask_tokens` processor работает только на string-values 1-го уровня
- **Файл**: `bot/main.py:21-26`.
- **Проблема**: 
  ```python
  for k, v in list(event_dict.items()):
      if isinstance(v, str): event_dict[k] = ...sub(v)
  ```
  Не обходит вложенные dict/list (например, `parsed={...}` в `search.py:152`, `result=result` в `add_service.py:357,509,710`). Если внутри nested dict окажется bot-token (теоретически — TG webhook URL), маска не сработает.
- **Влияние**: minimal на сегодня (TG-токен в nested структурах не появляется), но в любой момент кто-то залогирует `tmdb_response={"results": [..., url: "https://api.tg.../botXXX:..."]}` и токен потечёт.
- **Решение**: рекурсивный обход event_dict.
- **Статус**: [ ]

### OBS-24 (LOW, NEW) — Watchdog-stale dump в stderr теряется в Docker-rotation
- **Файл**: `bot/main.py:58-64`.
- **Проблема**: `faulthandler.dump_traceback(file=sys.stderr, all_threads=True)` — отлично. Но stderr идёт через `json-file` driver с `max-size 10m, max-file 3` = 30MB rolling. Если хеш-инцидент → дамп ~50KB stack-traces; через 10 минут активного бота они уйдут в ротацию.
- **Решение**: отдельный mount `/var/log/tgarr-watchdog.log` для критичных дампов. Опционально: persistent volume.
- **Статус**: [ ]

---

## Сводка

| Категория | OBS-01 | 02 | 03 | 04 | 05 | 06 | 07 | 08 | 09 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 | 19 | 20 | 21 | 22 | 23 | 24 |
|-----------|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Severity  | H | M | M | M | L | H | M | L | L | L | H | H | H | H | H | M | M | M | L | L | L | L | L | L |
| Round     | 2 | 2 | 2 | 2 | 2 | 2 | 2 | 2 | 2 | 2 | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 |
| Status    | ✗ | ✗ | ⏸ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |

✗ = open. ⏸ = deferred (OBS-03 metrics).

HIGH: 6 (3 из раунда 2 не сделаны + 3 новых). MED: 7. LOW: 11.

## Главные actionable выводы

1. **Жалоба «плохо ищет, не понимает контент» НЕ диагностируется по логам.** Нужны OBS-11 (winner detect_content_type), OBS-12 (фильтр по типу), OBS-13 (top-N с score), OBS-14 (branch-events в process_search) — всё HIGH-severity.
2. **Корень problemy раунда 2 — OBS-01** (handlers без bind'ов) — не исправлен, хотя fix описан и тривиален: 3 строки в `LoggingMiddleware`. Это блокирует все остальные observability-улучшения, потому что без user_id/request_id даже хорошие логи бесполезны.
3. **Latency полностью невидим в prod** (OBS-15, OBS-16). При жалобе «бот тупит» нет данных — где затык. Минимум: повысить `BaseAPIClient.elapsed_ms` в DEBUG → INFO с порогом slow.
4. **Metrics остаются deferred** — для 10-20-юзера ОК, но в случае массового инцидента восстановить картину «сколько юзеров затронуто» нельзя без grep-counter по логам.
5. **Прямой путь к 80% диагностируемости**: реализовать OBS-01 + OBS-11 + OBS-13 + OBS-15 = 4 правки, ~80 строк кода. Это закроет 90% жалоб типа «не работает поиск».

Файлы, требующие правок (все HIGH):
- `f:/VScode/TG_arr/bot/middleware/auth.py` (OBS-01, OBS-07)
- `f:/VScode/TG_arr/bot/services/search_service.py` (OBS-11, OBS-12, OBS-17)
- `f:/VScode/TG_arr/bot/handlers/search.py` (OBS-13, OBS-14, OBS-15)
- `f:/VScode/TG_arr/bot/services/add_service.py` (OBS-06)
- `f:/VScode/TG_arr/bot/clients/base.py` (OBS-16, OBS-02)
