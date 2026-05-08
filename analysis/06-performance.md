# Performance TG_arr v1.0 (раунд 3)

Дата: 2026-05-08. Контекст: бот на **Raspberry Pi 4** (ARM64, 2-4GB RAM, 256M/0.5cpu лимит, медленная Wi-Fi). Все *arr — на отдельном VPS, RTT 50-200ms. Жалоба пользователя: «плохо ищет».

Сравнение с раундом 2 (`analysis_round2/06-performance.md`): часть прошлых находок зафиксирована (PERF-04 singleton scoring, PERF-05 precompiled regex, PERF-06 ранний выход для коротких запросов). Остальное либо deferred, либо переоткрывается с новой остротой на rpie4.

---

## Критические (заметно для пользователя — могут объяснять «плохо ищет»)

### PERF-01: detect_content_type без таймаута на параллельных lookup-ах
- **Файл**: `bot/services/search_service.py:69-76`
- **Проблема**: `asyncio.gather(*tasks, return_exceptions=True)` без `asyncio.wait_for(...)` или `asyncio.timeout(...)`. Запускаются 3 lookup'а: Radarr, Sonarr и (если включён) Lidarr. На VPS через Wi-Fi с RTT 200ms каждый lookup — 1-3s, любой висящий запрос тянет всю detect-фазу. `BaseAPIClient._request` имеет `wait_exponential(min=1,max=5)` + `stop_after_attempt(2)` → один зависший lookup может удерживать gather до **30s + retry**. До этого момента user видит «🔍 Определяю тип контента…» и думает, что бот сломан.
- **Влияние**: 1-30 секунд задержки **до** реального поиска при медленной сети или подвисшем VPS. Это первая фаза любого «плохого поиска».
- **Решение**: обернуть весь `gather` в `asyncio.wait_for(..., timeout=8.0)`; при срабатывании — `ContentType.UNKNOWN` и идти дальше с явным fallback («не смог определить тип, ищу как фильм/сериал»).
- **Статус**: [ ]

### PERF-02: Prowlarr search timeout 60s — пользователь висит до минуты
- **Файл**: `bot/clients/prowlarr.py:66`
- **Проблема**: `timeout=60.0` на `/api/v1/search`. Plus tenacity `stop_after_attempt(2)` + `wait_exponential(min=1,max=5)` → в худшем случае 60+5+60 = **125s** ожидания. Telegram callback window ~15s, message edit будет «зависшим». Пользователь нажимает что угодно ещё → дубль запросов (no idempotency lock).
- **Влияние**: типичный сценарий «плохо ищет» = Prowlarr долго ходит по индексерам, пользователь не понимает, что происходит, повторяет запрос.
- **Решение**: 
  1. снизить hard-timeout до **20-25s**;
  2. показывать промежуточный keepalive («Ищу в N индексеров, это может занять до 25 секунд»);
  3. отключить retry на search (idempotent, но дорогой);
  4. рассмотреть `params["interactiveSearch"]=False` если поддерживается;
  5. cancel-button в UI с сохранением частичных результатов.
- **Статус**: [ ]

### PERF-03: detect_content_type делает `lookup_movie` + `lookup_series` на КАЖДЫЙ свободный запрос, даже не music-related
- **Файл**: `bot/services/search_service.py:69-91`, `bot/handlers/search.py:114-120`
- **Проблема**: `handle_text_search` ловит **любой текст** не из меню, не команда — сразу `process_search` с `ContentType.UNKNOWN`. Это означает, что **каждое** случайное сообщение пользователя в чате (если он не использует меню) → Radarr lookup + Sonarr lookup + (опц.) Lidarr lookup. Три HTTP-запроса на VPS. Пользователь набирает «привет» (5 символов > 4) → 3 cross-VPS запроса.
- **Влияние**: при каждой опечатке/уточнении/чат-сообщении бот делает 3 круга через медленную сеть. На rpie4 это заметно, особенно если индексер на VPS под нагрузкой.
- **Решение**: 
  1. фронт-фильтр: если строка `<` 5 символов или `>` 80 символов или содержит много пунктуации — спросить «вы хотели поискать?» вместо немедленного lookup;
  2. либо требовать `/` команду или меню-кнопку для всех поисков и убрать автохэндлер.
- **Статус**: [ ]

### PERF-04: Sequential awaits в `process_search` — сначала detect, потом search (нельзя распараллелить, но можно делать prowlarr.search спекулятивно)
- **Файл**: `bot/handlers/search.py:155-201`
- **Проблема**: явная серия `detect_content_type → search_releases`. detect_content_type сам делает 2-3 параллельных lookup, ждём ВСЕХ → прогрессим к prowlarr.search. На медленной сети это **detect (3-8s) + prowlarr.search (10-25s) = 13-33s до первого результата**.
- **Влияние**: главная причина субъективного «бот тормозит». User ждёт ~20+s для нового запроса.
- **Решение**: 
  1. **спекулятивный prowlarr.search**: запускать `prowlarr.search(query, content_type=UNKNOWN)` в `asyncio.create_task` параллельно с `detect_content_type`; когда тип определён — фильтровать уже полученные результаты в памяти. Это сократит wall-time с T_detect+T_search до max(T_detect, T_search);
  2. либо если detect выдал UNKNOWN — НЕ делать второй проход, а сразу показать prowlarr-результаты без фильтра, отметив тип каждого по `detected_type`;
  3. cache по query+content_type на 60s в памяти, чтобы повторный запрос за минуту был мгновенным.
- **Статус**: [ ]

### PERF-05: Session save/load на каждый callback (модельная валидация 500 SearchResult'ов на slow SD-card)
- **Файл**: `bot/db.py:321-381`, `bot/handlers/search.py:316-382, 385-475, 683-739`
- **Проблема**: каждый `page:` / `rel:` / `back` callback:
  1. `get_session` → SQL SELECT + json.loads + `SearchSession.model_validate` (pydantic round-trip 500 объектов с вложенным `QualityInfo`),
  2. `save_session` → `model_dump_json` + UPSERT.
  
  На rpie4 SD-card random-write ~5-15ms, json+pydantic для 500 results = 30-80ms. **Каждый** клик пагинации делает это туда-обратно.
- **Влияние**: на rpie4 каждый клик пагинации = ~100-150ms ouverture (+ Telegram round-trip). При активной навигации заметно тормозит.
- **Решение**: 
  1. in-process LRU кэш `dict[user_id, (mtime, SearchSession)]` с TTL 30 мин — проверяем mtime в SQL и переиспользуем dict (cheap), либо вообще держим в памяти (мало пользователей);
  2. в session.results хранить **минимум** (guid + title + indexer_id + score + protocol + download_url) — остальное выбрасывать или хранить отдельно с lazy fetch;
  3. в pydantic v2 `SearchSession` использовать `ConfigDict(frozen=False)` и мутировать `current_page` без полного re-validate (тут load всё равно нужен, но dump можно сократить в `save_session_partial`).
- **Статус**: [ ]

### PERF-06: get_torrents() на каждый callback (всё ещё HIGH из раунда 2)
- **Файлы**: `bot/handlers/downloads.py:188, 223, 309, 335, 367, 403, 437, 467, 497, 606`
- **Проблема**: refresh, page, filter, torrent details (`t:abc`), pause, resume, delete, recheck, set_priority — **каждый** делает полный `qbt.get_torrents()`. Для 100 торрентов ~200KB JSON. Pause/resume callbacks делают это **дважды** (один раз для lookup'а hash, второй раз — refresh после действия).
- **Влияние**: серия кликов в Downloads UI создаёт нагрузку на qBit + сеть; на rpie4 при 500+ торрентах список рендерится 1-3s каждый клик.
- **Решение**: TTL-cache (3-5s) на handler-уровне или в `QBittorrentClient`, инвалидация при write-операциях.
- **Статус**: [ ]

---

## Высокие

### PERF-07: НЕТ HTTP connection pool tuning — для каждого *arr используется default `httpx.Limits` (max_keepalive=20, max_connections=100)
- **Файлы**: все клиенты (`base.py:78-82`, `tmdb.py:33-38`, `emby.py:66-70`, `qbittorrent.py:79-83`)
- **Проблема**: `httpx.AsyncClient(...)` создаётся без `limits=...`. Это и хорошо (singleton по клиенту, keep-alive есть по умолчанию), и плохо: **default `Limits(max_keepalive_connections=20)`** избыточно для одиночного сервера. На ARM с 256M это пустые сокет-структуры. Нет явного `http2=True` (httpx может ускорить mux). Нет `keepalive_expiry`.
- **Влияние**: умеренно, но при холодном старте после простоя у каждого клиента лежит TCP-handshake (~100-200ms RTT). Если бы держали keep-alive **дольше** — холодных handshakes было бы меньше.
- **Решение**:
  ```python
  httpx.AsyncClient(
      base_url=...,
      limits=httpx.Limits(
          max_keepalive_connections=4,
          max_connections=10,
          keepalive_expiry=300,  # 5 min idle
      ),
      http2=False,  # *arr не поддерживают
      timeout=...,
  )
  ```
  Для TMDb можно `http2=True` (Cloudflare поддерживает).
- **Статус**: [ ]

### PERF-08: Cold start — все клиенты создаются лениво, первый запрос платит за TCP+TLS handshake
- **Файлы**: `bot/clients/registry.py` (все `get_*`), `bot/main.py:179-263`
- **Проблема**: singletons создаются ПРИ ПЕРВОМ запросе. Когда пользователь делает первый поиск после рестарта/idle, бот платит:
  - DNS lookup VPS (50-100ms),
  - TCP handshake (1-2 RTT = 100-400ms),
  - TLS handshake (2-3 RTT = 200-600ms),
  - в случае Prowlarr — health check на VPS внутри Prowlarr (он тоже не в горячем кэше).
  Итого **первый поиск после простоя = +500-1500ms**.
- **Влияние**: совокупно с PERF-04 первый запрос за час может быть >30s. Без warm-up.
- **Решение**: в `on_startup` (после `bot.get_me()`) запускать «прогрев» — `asyncio.gather` с `prowlarr.check_connection()`, `radarr.check_connection()`, `sonarr.check_connection()` параллельно с timeout 5s. Это и DNS прогревает, и singleton создаёт, и keep-alive разворачивает.
- **Статус**: [ ]

### PERF-09: tenacity retry на все 5xx — но с медленной сети 502/504 могут быть нормой, retry усугубляет hang
- **Файл**: `bot/clients/base.py:101-106, 156-161`
- **Проблема**: `RetryableAPIError` бросается на 429/500/502/503/504. `wait_exponential(min=1, max=5)` + `stop_after_attempt(2)` → между попытками ~1-5s паузы. На *arr серверах перегрузка → 504 → бот ждёт +5s → второй запрос → ещё +5s. Прибавляется к 60s prowlarr-таймауту.
- **Влияние**: у пользователя «бот на минуту замолк, потом сказал что неудача» — типичная история на rpie4 при перегруженном VPS.
- **Решение**:
  1. retry **только** на сетевых ошибках (TimeoutException, ConnectError) и 429 (с честным `Retry-After`);
  2. на 5xx — fail fast (бот может предложить «попробовать ещё раз»);
  3. для prowlarr search — отключить retry полностью (поиск не идемпотентен в смысле «лучше один раз чем долго ждать»).
- **Статус**: [ ]

### PERF-10: get_torrent_by_short_hash линейный поиск по полному списку (HIGH из раунда 2 — не исправлено)
- **Файл**: `bot/clients/qbittorrent.py:296-302`
- **Проблема**: `torrents = await self.get_torrents()` (full fetch) + Python-цикл с `t.hash.lower().startswith(...)`. Для 500 торрентов = full fetch + 500 sтрингов. И вызывается из 7 мест в downloads.py.
- **Влияние**: см. PERF-06 — особенно болезненно для callback `t:abc` после клика на любой торрент.
- **Решение**: `qBit /api/v2/torrents/info?hashes=full_hash` — но short_hash идёт от UI. Решение: при первом render списка строить in-memory map `short_hash → full_hash` с TTL.
- **Статус**: [ ]

### PERF-11: notification_service полный fetch каждые 60s — на rpie4 это 100-200KB парсинга
- **Файл**: `bot/services/notification_service.py:136-175`
- **Проблема**: `_check_for_completions` делает `qbt.get_torrents()` (full list) каждые 60s. Парсинг 500 torrents в pydantic ~30-50ms на rpie4. Плюс трафик с qBit. Memory growth: `_tracked_torrents` dict монотонно растёт (cleanup только по removed_hashes).
- **Влияние**: фоновая нагрузка ~1% CPU + steady network traffic.
- **Решение**: `/api/v2/sync/maindata` с `rid` — incremental updates. Сложнее, но на ARM с 256M ощутимо.
- **Статус**: [ ]

### PERF-12: SQLite synchronous=NORMAL OK, но `_connection.execute("BEGIN")` без явного COMMIT pragma WAL — нет периодической `wal_checkpoint`
- **Файл**: `bot/db.py:46-55`
- **Проблема**: `journal_mode=WAL` без `PRAGMA wal_autocheckpoint=N` (default 1000 страниц = 4MB). При активной session-save-каждый-клик WAL растёт. На SD-card это ещё и износ.
- **Влияние**: средний — при долгой сессии файл растёт, периодически TRUNCATE.
- **Решение**:
  - `PRAGMA wal_autocheckpoint=200` (≈800KB),
  - `PRAGMA temp_store=MEMORY` — temp tables в RAM,
  - `PRAGMA mmap_size=33554432` — 32MB mmap для read (чтобы json чтение шло без syscall),
  - `PRAGMA cache_size=-2000` — 2MB страничный кэш (default 2MB но в строках страниц 4KB).
- **Статус**: [ ]

---

## Средние

### PERF-13: TMDb — отдельный httpx-клиент (через прокси), но в TMDbClient переопределён `_get_client` без `_client_lock` использования — race возможен
- **Файл**: `bot/clients/tmdb.py:29-39`
- **Проблема**: `super().__init__` инициализирует `self._client_lock`, и `_get_client` использует `async with self._client_lock`, но **только один раз при создании**. Settings берутся из `self._settings`, который НЕ инициализируется в `TMDbClient.__init__` (наследует lazy через `_get_http_timeout`, но `_get_client` идёт через `self._settings.http_timeout` НАПРЯМУЮ — будет AttributeError при первом вызове, если `_get_http_timeout` не был вызван заранее).
- **Влияние**: latent bug в TMDb клиенте при холодном вызове, может объяснять fallback в trending.
- **Решение**: вызвать `self._get_http_timeout()` или защитить `if self._settings is None: self._settings = get_settings()`.
- **Статус**: [ ]

### PERF-14: Pydantic model_copy в sort_results — N polymorphic copies (не исправлено из раунда 2)
- **Файл**: `bot/services/scoring.py:256-260`
- **Проблема**: для 500 results = 500 model_copy. Pydantic v2 model_copy дешевле v1, но всё равно создаёт новый Python-объект и dict. На ARM это ~5-10ms.
- **Решение**: `r.calculated_score = score` напрямую (pydantic v2 поддерживает mutable fields по умолчанию для not-frozen моделей). Удалить `update={...}`.
- **Статус**: [ ]

### PERF-15: Регулярки в Prowlarr `_parse_quality` / `_extract_*` re-evaluate каждый раз для каждого результата
- **Файл**: `bot/clients/prowlarr.py:201-373`
- **Проблема**: ~15 `re.search` per result. Python кэширует до 512 паттернов в `re._cache`, но на 500 результатов × 15 паттернов = 7500 lookups. Python re-cache хорошо обрабатывает, но на ARM ощутимо.
- **Влияние**: ~100-200ms на парсинг 500 результатов от Prowlarr.
- **Решение**: вынести все паттерны в module-level `_PAT_RES_2160 = re.compile(...)`, `_PAT_RES_1080 = ...`, etc. (precompile). Для resolution/source/codec — вообще `set` lookup без regex.
- **Статус**: [ ]

### PERF-16: ProwlarrClient.search читает 100 results с `limit=100` — но на rpie4 100 × pydantic-объект ≈ 1-2MB RSS на каждый поиск
- **Файл**: `bot/clients/prowlarr.py:32` (default `limit=100`), `bot/db.py:328-329` (cap 500 при сохранении)
- **Проблема**: 256M memory limit — RSS бота ~80-120M idle + 1.5-2M на каждый параллельный поиск. На пиках OOM-kill.
- **Влияние**: при 3 одновременных поисках от разных юзеров = 5-6M temp + сессии. С учётом httpx buffer'ов + pydantic-валидации — близко к лимиту.
- **Решение**:
  - снизить default `limit` до 50 (UI всё равно показывает 5 на страницу × 10 страниц);
  - в save_session — хранить только top-50 (cap 500 избыточен);
  - после save_search не держать `results` в памяти handler'а.
- **Статус**: [ ]

### PERF-17: Trending cache замена-на-замену (не исправлено из раунда 2)
- **Файл**: `bot/handlers/trending.py:92-96, 142-147`
- **Проблема**: `if len() > MAX: cache = {}` — если кэш чуть-чуть переполнен, чистится **полностью**, теряются недавние entries. Random thrashing.
- **Решение**: использовать `cachetools.LRUCache(maxsize=200)` или OrderedDict с move_to_end.
- **Статус**: [ ]

### PERF-18: `_artist_candidates`, `_trending_artists_cache` — глобальные dict[user_id, ...], cleanup только при overflow всех ключей
- **Файл**: `bot/handlers/music.py:39-48`
- **Проблема**: cleanup= clear() при overflow. См. PERF-17 (то же что и trending).
- **Решение**: TTL-кэш (1h). Перенести в `cachetools` или просто привязать к `SearchSession` — там есть TTL cleanup в db.
- **Статус**: [ ]

### PERF-19: Telegram polling без `request_timeout`/`polling_timeout` — long-polling используется, но default
- **Файл**: `bot/main.py:267-271`
- **Проблема**: aiogram 3 default `polling_timeout=30`. На rpie4 с медленной сетью к api.telegram.org (через VPS-proxy?) разрыв TCP может незаметно повисеть. 
- **Влияние**: малое (aiogram сам реконнектит), но в логах могут быть GET timeouts.
- **Решение**: явно `polling_timeout=20`, `request_timeout=15`. Никакого throttling — нет flood control middleware (только rate-limit пользователей).
- **Статус**: [ ]

### PERF-20: Logging level — `LOG_LEVEL=INFO` default, но `log.debug(...)` в hot-paths остаются (`base.py:137`, `db.py:347, 364`)
- **Файлы**: `bot/clients/base.py:137-141`, `bot/db.py:347, 364-369`
- **Проблема**: structlog `make_filtering_bound_logger(INFO)` отсекает на bound-уровне (хорошо), но **аргументы вычисляются** до этого (например `len(session.results)` дёшево, но `elapsed_ms=round(...)` всегда вычисляется). На rpie4 каждые 100мс — ничтожно, но сумма по сотням callback'ов заметна.
- **Решение**: проверить, не строится ли крупный dict в DEBUG-логе; для structlog можно использовать lazy через `**lazy_kwargs`.
- **Статус**: [ ]

---

## Низкие

### PERF-21: Импорты handlers выполняются на холодном старте (~3-5MB модулей)
- **Файл**: `bot/handlers/__init__.py`, импорты structlog, pydantic, aiogram, httpx, tenacity, aiosqlite — итого ~8-12MB RSS до запуска main.
- **Влияние**: cold start +1-2s на rpie4. Не критично, но при OOM-restart долго.
- **Решение**: ничего, Python такой.
- **Статус**: [ ]

### PERF-22: `re.compile` на каждый запрос в search_service.detect_content_type — 7 паттернов каждый раз
- **Файл**: `bot/services/search_service.py:54-65`
- **Проблема**: каждый detect — 7 `re.search` (Python re-cache спасает, но не идеально). 
- **Решение**: module-level `_SERIES_PATTERNS = [re.compile(p) for p in [...]]`.
- **Статус**: [ ]

### PERF-23: ScoringWeights `bad_keywords` mutable default — `Optional[dict] = None` ОК, но `_bad_keyword_patterns` пересоздаётся при каждом ScoringWeights() (через __post_init__)
- **Файл**: `bot/services/scoring.py:70-97`
- **Проблема**: `_SCORING_SERVICE = ScoringService()` singleton — `__post_init__` вызывается ОДИН раз при импорте. ОК.
- **Влияние**: нет.
- **Статус**: N/A

### PERF-24: SQLite cleanup_old_sessions/searches на startup — синхронный, но быстрый
- **Файл**: `bot/db.py:476-509`, `main.py:104-111`
- **Проблема**: на startup — DELETE WHERE updated_at<cutoff. На SD-card 50-200ms.
- **Влияние**: только на startup, ОК.
- **Статус**: N/A

### PERF-25: orjson был убран в раунде 2 — почему?
- **Контекст**: пользователь спросил «но раунд 2 убрал orjson — почему?»
- **Поиск**: `requirements.txt` имеет только `aiogram, httpx, tenacity, pydantic, pydantic-settings, aiosqlite, structlog`. orjson отсутствует.
- **Гипотеза**: pydantic v2 имеет встроенную rust-impl сериализацию (`model_dump_json`), которая использует pydantic-core (Rust). По производительности эквивалентна orjson для pydantic-моделей. Для SearchSession с 500 SearchResult — pydantic-core быстрее orjson(json.dumps(model.model_dump())) потому что нет double-pass.
- **Вердикт**: правильно убрали. msgspec был бы быстрее, но переход — большая работа (модели надо переписать на msgspec.Struct), на rpie4 экономия 10-20ms на сессию не оправдывает риск.
- **Статус**: N/A (информационно)

### PERF-26: DNS resolving на каждый запрос?
- **Контекст**: вопрос пользователя.
- **Анализ**: `httpx.AsyncClient` с keep-alive держит TCP-сокет открытым → DNS не перерезолвится пока сокет жив (default keepalive_expiry=5s в httpx). При idle >5s — да, перерезолвится. На rpie4 systemd-resolved кэширует.
- **Решение**: см. PERF-07 — увеличить `keepalive_expiry=300`.
- **Статус**: дублирует PERF-07.

### PERF-27: Telegram throttling / flood control — отсутствует
- **Файлы**: `bot/middleware/auth.py:136-188` — RateLimitMiddleware (per-user 30/min), но НЕТ глобального rate-limit для **исходящих** сообщений.
- **Проблема**: aiogram 3 имеет встроенный flood-control (через `ChatActionSender`, retry на 429), но если бот шлёт нотификации через `notification_service` бурстом (10 пользователей, 5 событий) → Telegram 429.
- **Решение**: подключить `aiogram_throttle` или собственный semaphore (1 msg per 30ms = 30msg/s, под лимитом Telegram 30/sec для bots).
- **Статус**: [ ]

### PERF-28: 256M memory limit — реально для нагрузки?
- **Анализ**: idle RSS ~80-120MB (Python 3.12 + aiogram + httpx + structlog + pydantic + aiosqlite). Под пиком (3 одновр. поиска × 1-2MB + sessions) — ~140-180MB. Ноге 256M = ~30% запас. На ARM Linux Docker overhead ~30-50MB.
- **Вывод**: впритык, но достаточно. **При увеличении limit'а Prowlarr (см. PERF-16) — close to OOM**. С 50 results — ОК.
- **Решение**: оставить 256M, но снизить `prowlarr.search.limit` до 50 (PERF-16).
- **Статус**: [ ]

### PERF-29: 0.5 CPU limit — реально для ARM
- **Анализ**: rpie4 = 4× Cortex-A72 @ 1.5GHz. 0.5 = пол ядра. JSON-парсинг 500 SearchResult ~50ms на полное ядро → 100ms на 0.5. 
- **Вывод**: при единичном поиске — норм; при concurrent (notification_service + поиск + другой пользователь) — троттлинг.
- **Решение**: поднять до 1.0 CPU (всё равно у rpie4 есть запас, бот не самый тяжёлый процесс).
- **Статус**: [ ]

### PERF-30: HEALTHCHECK `find /tmp/tgarr-alive -mmin -2` — каждые 60s
- **Файл**: `Dockerfile:19-20`
- **Проблема**: `find` запускается каждые 60s. На rpie4 это fork+exec ~10-30ms каждый.
- **Влияние**: nano.
- **Статус**: [ ]

---

## Сводка по категориям

| Severity | IDs | Total |
|---|---|---|
| **Критические** | PERF-01, 02, 03, 04, 05, 06 | 6 |
| **Высокие** | PERF-07, 08, 09, 10, 11, 12 | 6 |
| **Средние** | PERF-13, 14, 15, 16, 17, 18, 19, 20 | 8 |
| **Низкие** | PERF-21, 22, 24, 27, 28, 29, 30 | 7 |
| **N/A / Info** | PERF-23, 25, 26 | 3 |

## Что объясняет «плохо ищет» (top-3 по симптому)

1. **PERF-04 + PERF-02 + PERF-01** — sequential awaits detect→search, **каждый** до минуты на retry'ах. **Главное**: зависающий detect_content_type без timeout удерживает event-loop задачу даже когда уже понятно, что Lidarr недоступен.
2. **PERF-08 (cold start) + PERF-09 (retry на 5xx)** — первый поиск после простоя стабильно медленнее на 0.5-1.5s.
3. **PERF-03 (lookup на любой текст)** — пользователь набирает что угодно, бот делает 3 запроса на VPS. Кажется «бот реагирует медленно».

## Что обязательно фиксить в раунде 3

1. **PERF-01**: `asyncio.wait_for(gather, timeout=8.0)` в detect_content_type. **2 строки кода, drastically улучшит UX**.
2. **PERF-02**: prowlarr search timeout 60→25s + отключить retry. **3 строки кода**.
3. **PERF-04**: спекулятивный prowlarr.search параллельно с detect. **~10 строк**.
4. **PERF-08**: warm-up в on_startup. **~5 строк**.
5. **PERF-07**: `httpx.Limits(keepalive_expiry=300)` всем клиентам. **~20 строк**.

После этих 5 фиксов суммарный wall-time на типичный поиск должен упасть с 15-30s до 5-12s.
