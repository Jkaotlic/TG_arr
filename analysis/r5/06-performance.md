# Анализ производительности TG_arr (раунд 5)

Прочитаны все 40 файлов `bot/` (12 861 строк), docker-compose; сопоставлено с прод-контекстом (Pi 4, 256MiB, одновременные 503 Radarr+Lidarr).

## Критические

### PERF-01: Detection-burst — 3 параллельных lookup'а в Radarr+Sonarr+Lidarr на КАЖДОЕ свободное текстовое сообщение, без семафора и кэша
- **Файл**: `bot/services/search_service.py:106-117` (создание task'ов + gather), триггер — `bot/handlers/search.py:151-157` (`handle_text_search` ловит любой текст)
- **Проблема**: при любом тексте без явного типа бот одномоментно бьёт `radarr.lookup_movie` + `sonarr.lookup_series` + `lidarr.lookup_artist`. Эндпоинты `*/lookup` — самые тяжёлые в *arr: каждый ходит во внешний metadata-proxy (api.radarr.video / api.lidarr.audio / Skyhook). Это **единственный код-путь, где Radarr и Lidarr нагружаются в один и тот же момент** — точное совпадение с наблюдаемым «Radarr и Lidarr одновременно 503». Нет: (а) глобального семафора, (б) кэша повторных запросов (повтор после таймаута = тот же тройной burst), (в) cooldown'а. Сразу после detection идёт Prowlarr search с fan-out на все индексеры — пиковая CPU-конкуренция на Pi.
- **Риск**: 503/деградация Radarr/Lidarr; каждый пользовательский текст = 3 concurrent тяжёлых запроса + 1 Prowlarr-fanout.
- **Решение**: (1) глобальный `asyncio.Semaphore(2)` вокруг detection-lookup'ов; (2) LRU-кэш detection-результатов с TTL 5–10 мин по нормализованному запросу; (3) ступенчатая стратегия: сначала Radarr+Sonarr, Lidarr — только если оба score < порога; (4) при 503 — короткий circuit-breaker (30–60с не ходить в этот сервис из detection).
- **Статус**: [ ] Не исправлено

## Средние

### PERF-02: Notification loop — полный опрос всех торрентов каждые 60с без delta-протокола и адаптивного интервала
- **Файл**: `bot/services/notification_service.py:136-175` (`_check_for_completions` → `get_torrents()` без фильтра, строка 139), цикл — `:92-111`
- **Проблема**: каждые 60с тянется **весь** список торрентов (включая сотни сидируемых) и каждый парсится в pydantic `TorrentInfo` (`bot/clients/qbittorrent.py:306-318`). qBittorrent имеет `sync/maindata` с `rid` (delta-протокол), но он не используется. Интервал не адаптируется.
- **Риск/стоимость**: 1440 запросов/сутки почти все впустую при простое; при T торрентов — T×~1КБ JSON + T pydantic-объектов в минуту = постоянный CPU/GC-шум на ARM.
- **Решение**: перейти на `/api/v2/sync/maindata?rid=N`; либо минимально — `get_torrents(filter_type=TorrentFilter.DOWNLOADING)` + отслеживание исчезновения из downloading; адаптивный интервал (нет активных → 300с).
- **Статус**: [ ] Не исправлено

### PERF-03: Таблица `search_results` — мёртвая запись ~50–150 КБ на каждый поиск (никогда не читается)
- **Файл**: `bot/db.py:358-365` (INSERT), `bot/db.py:373-382` (`get_search_results` — не вызывается ни из одного хендлера)
- **Проблема**: `save_search` (`bot/handlers/search.py:281`) сериализует все ~100 результатов в JSON и пишет в `search_results`, но тот же JSON пишется второй раз в `sessions` строкой ниже (`search.py:290`). Двойная запись одного блоба на SD-карту. (= DB-03.)
- **Риск/стоимость**: ~100 КБ лишней записи на каждый поиск + рост WAL; износ SD; лишний CPU на второй `json.dumps`.
- **Решение**: убрать вставку в `search_results` (оставить строку в `searches` для истории).
- **Статус**: [ ] Не исправлено

### PERF-04: Сессия — полный цикл JSON parse/serialize (~100 КБ, ~100 pydantic-моделей) на каждый клик в UI
- **Файл**: `bot/db.py:440-472` (`get_session`), `bot/db.py:385-412` (`save_session`); горячие пути — `bot/handlers/search.py:398` (пагинация), `:465`, `:483-484`, `:511/534`
- **Проблема**: каждый клик пагинации/выбора десериализует ~100 `SearchResult` из SQLite и (при изменении) сериализует обратно — хотя нужны 5 результатов страницы. На Pi4 это ~50–150 мс CPU на клик + запись на SD. Один поиск→выбор→grab = 4–5 полных сериализаций.
- **Риск**: заметная латентность UI на Pi, лишний I/O на SD.
- **Решение**: in-memory кэш активной сессии (cap ~50 юзеров, TTL 24ч) с write-through в SQLite; либо хранить результаты один раз по `search_id`, в сессии — только `search_id/page/selected_idx`.
- **Статус**: [ ] Не исправлено

### PERF-05: Downloads — каждое действие с торрентом тянет полный список для поиска по 8-символьному префиксу хэша
- **Файл**: `bot/clients/qbittorrent.py:320-326` (`get_torrent_by_short_hash` → `get_torrents()` целиком); вызовы: `bot/handlers/downloads.py:331, 357, 393, 429, 463, 493, 523`
- **Проблема**: клик по любой кнопке торрента = полная выборка и парсинг всех торрентов ради матчинга префикса. Целевой `get_torrent(full_hash)` существует (`qbittorrent.py:328`), но используется только для re-fetch.
- **Риск/стоимость**: при 200+ торрентах — сотни КБ JSON + 200 pydantic-объектов на каждый клик.
- **Решение**: класть **полный** хэш в callback_data: `"t_pause:" (8) + 40 hex = 48 байт` — влезает в лимит 64 байта; short-hash оставить как fallback.
- **Статус**: [ ] Не исправлено

### PERF-06: qBittorrent/Emby httpx-клиенты без `limits` — keepalive умирает через 5с (дефолт httpx)
- **Файл**: `bot/clients/qbittorrent.py:77-86`, `bot/clients/emby.py:63-71`
- **Проблема**: в отличие от `BaseAPIClient` (base.py:81-90, `keepalive_expiry=300`), эти два клиента создаются без `httpx.Limits` → дефолт `keepalive_expiry=5.0`. Notification loop опрашивает qBit раз в 60с — соединение всегда мертво → новый TCP-handshake на каждый poll (1440/сутки).
- **Риск**: небольшой (LAN), но бесплатно чинится.
- **Решение**: те же `httpx.Limits(max_keepalive_connections=4, max_connections=10, keepalive_expiry=300.0)`.
- **Статус**: [ ] Не исправлено

### PERF-07: Последовательные независимые API-вызовы там, где нужен gather
- **Файл**: `bot/handlers/settings.py:48-51`, `:81-84` (4 последовательных вызова); `bot/handlers/search.py:758-759`, `:796-797` (profiles + root_folders в горячем пути grab'а); `bot/handlers/music.py:308-310`; `bot/handlers/trending.py:356-357`, `:443-444`
- **Проблема**: 2–4 последовательных RTT вместо одного wall-clock RTT. На Pi это +0.3–1с к каждому grab/настройкам.
- **Риск**: латентность UX.
- **Решение**: `asyncio.gather(get_profiles(), get_root_folders())` (2 запроса — безопасно, в отличие от PERF-01). Опционально: кэш profiles/root_folders с TTL 10 мин.
- **Статус**: [ ] Не исправлено

## Низкие

### PERF-08: `sort_results` — `model_copy` для каждого из ~100 результатов
- **Файл**: `bot/services/scoring.py:269-273`
- **Проблема**: на каждый поиск ~100 копий 25-полевых pydantic-моделей ради записи `calculated_score`. Модели не frozen.
- **Решение**: `r.calculated_score = score` in-place + `results.sort(...)`.
- **Статус**: [ ] Не исправлено

### PERF-09: RateLimitMiddleware — cleanup-ветка мёртвая
- **Файл**: `bot/middleware/auth.py:200-204`
- **Проблема**: чистятся только юзеры с пустыми списками, но списки никогда не пустеют (фильтруются лишь при следующем запросе того же юзера) — условие `if not reqs` не сработает. Утечка теоретическая (<10 юзеров).
- **Решение**: чистить по `reqs[-1] < window_start`, порог снизить с 10000 до 1000.
- **Статус**: [ ] Не исправлено

### PERF-10: liveness-файл `/tmp/tgarr-alive` — запись на overlayfs (SD) каждые 30с
- **Файл**: `bot/main.py:294-301` (touch), `:40-65` (watchdog stat); `docker-compose.yml:48-49` — tmpfs для `/tmp` не смонтирован
- **Проблема**: `/tmp` внутри контейнера на writable-слое (SD) → metadata-запись каждые 30с, 2880/сутки.
- **Решение**: в compose `tmpfs: [/tmp]` — код менять не нужно. (Синергия с DEPLOY-05.)
- **Статус**: [ ] Не исправлено

### PERF-11: Нет VACUUM/auto_vacuum — файл БД держит high-water mark
- **Файл**: `bot/db.py:54-65`, `bot/main.py:161-181`
- **Проблема**: файл вырастает до пика недельной активности и не сжимается; WAL checkpoint TRUNCATE только на shutdown.
- **Решение**: `PRAGMA optimize` после cleanup; устранение PERF-03 делает почти неактуальным. (= DB-08.)
- **Статус**: [ ] Не исправлено

### PERF-12: Trending-кэши без TTL (данные стареют; clear() сбрасывает всех юзеров)
- **Файл**: `bot/handlers/trending.py:27-34` (cap 200, clear-on-overflow), `bot/handlers/music.py:47-48`
- **Проблема**: память не течёт (cap есть), но записи живут до рестарта — клик по «вчерашнему» трендовому фильму работает со стейл-данными. «clear всё при переполнении» сбрасывает активные списки других юзеров («Выбор истёк»).
- **Решение**: TTL-метка (added_at, 1–6ч); вытеснять по-старшинству, а не clear() целиком.
- **Статус**: [ ] Не исправлено

### PERF-13: Индекс `actions(user_id)` без `created_at` для ORDER BY DESC
- **Файл**: `bot/db.py:158`, запрос `:516-528`
- **Проблема**: SQLite использует `idx_actions_user`, затем сортирует в памяти. Объём мал. (= DB-06.)
- **Решение**: составной `idx_actions_user_created ON actions(user_id, created_at DESC)` миграцией v3.
- **Статус**: [ ] Не исправлено

## Проверено — проблем нет

- **Блокирующие вызовы**: `time.sleep` только в watchdog-**треде** (намеренно); `requests`/синхронного `sqlite3` нет; DNS в SSRF-валидаторе — в `asyncio.to_thread` (`add_service.py:143`). Регэкспы прекомпилированы; `SequenceMatcher` — на коротких строках.
- **HTTP-клиенты *arr/TMDb/Deezer**: синглтоны через registry с double-check под lock; `keepalive_expiry=300с`, пул ограничен (base.py:81-90).
- **Retry-политика не усугубляет 503**: 5xx **не** ретраится (base.py:184-189), ретраи только на сетевые ошибки и 429; grab/push через `_post_no_retry`.
- **Память**: все in-memory-структуры ограничены (caps 100–500); `_tracked_torrents` зеркалит текущий список qBit; `_grab_in_progress` чистится в finally. 117 MiB за 19ч — baseline, запас к 256 MiB достаточный.
- **Ранний срез больших ответов**: Prowlarr `limit=100` на сервере, сессия capped 500, TMDb 1 страница, Deezer limit=10, Emby Limit:10.
- **Параллелизм с ограниченным fan-out**: warmup — 4, `/status` — ≤7 лёгких, календарь — 3, qBit-статус — 4.
- **Холодный старт**: warmup в `wait_for(5s)` параллельно; polling стартует через ≤5с.
- **БД-настройки**: WAL + NORMAL + busy_timeout + autocheckpoint 200 + mmap 32МБ; индексы покрывают cleanup.
- **Notification loop без N+1**: один `get_torrents` на цикл.

**Приоритет под Pi**: PERF-01 (503-инцидент) → PERF-02 (86 тыс. запросов/2 мес впустую) → PERF-03+PERF-04 (SD-wear и латентность кликов) → остальное.
