# Performance Audit — TG_arr

Дата: 2026-04-18.

## PERF-01 — Blocking I/O в async hot paths — проверено (OK)

- `socket.gethostbyname` — **не используется** (закрыто SEC-11). Используется `asyncio.to_thread(socket.getaddrinfo, ...)` в `_validate_download_url`. Это thread-pool wrap, корректно.
- `Path().mkdir` в `db.connect()` вызывается один раз при старте — приемлемо.
- `json.loads/json.dumps` в session save/load — CPU-bound но <1ms на сессию из 500 SearchResult'ов.

**Вердикт:** критического блокирующего I/O в event-loop нет.

## PERF-02 — N+1 при pagination торрентов (HIGH)

Файл: `bot/handlers/downloads.py:223, 563`
`handle_page` и `handle_filter_select` на **каждый callback** делают `qbt.get_torrents()` (полный список, без server-side pagination). Если у пользователя 500 торрентов, каждая кнопка «следующая страница» = full fetch + local slice. qBit API поддерживает `limit` + `offset` — мы передаём их только в `get_torrents(limit=..., offset=...)`, но handler не использует.
**Решение:** использовать `limit=TORRENTS_PER_PAGE, offset=page*TORRENTS_PER_PAGE`; для `total_pages` отдельный lightweight запрос `transfer/info` → `total_torrents`.

## PERF-03 — Unbounded in-memory dicts (MED)

Файлы:
- `bot/handlers/music.py:35-36` — `_artist_candidates`, `_trending_artists_cache` — БЕЗ защиты размера.
- `bot/handlers/trending.py:27-28` — `_trending_movies_cache`, `_trending_series_cache` — защита `_MAX_CACHE_SIZE=200` есть.
- `bot/handlers/calendar.py:23` — `_user_period` — защита `_MAX_USER_PERIOD_ENTRIES=100` есть.
- `bot/middleware/auth.py:142` — `_user_requests` — защита 10000, но только очистка stale; fresh entries копятся.

В prod (10-20 whitelist пользователей) утечек не видно, но при 1000+ рестартов без cleanup music dict'ов RSS растёт.
**Решение:** TTL-based dict (`cachetools.TTLCache`, 1h) или migrate в `sessions` table.

## PERF-04 — ScoringService() создаётся при каждом get_services() (LOW)

Файлы: `bot/handlers/search.py:50`, `bot/handlers/music.py:50`
```python
scoring = ScoringService()
```
Новый экземпляр на каждый callback. `ScoringService.__init__` только `self.weights = weights or ScoringWeights()` — дёшево. Но принципиально стоит сделать singleton.
**Решение:** module-level `_SCORING = ScoringService()` или lru_cache.

## PERF-05 — Regex компиляция внутри циклов (MED)

Файл: `bot/services/scoring.py:227-231`
```python
for keyword, penalty in self.weights.bad_keywords.items():
    pattern = rf"\b{re.escape(keyword)}\b"
    if re.search(pattern, title_lower):
```
18 keywords × N результатов = 18N compiles. `re.search` кэширует до 512 паттернов в `re._cache`, но кастомный escape каждый раз. Дешевле один раз скомпилировать в `__post_init__`.

Также `bot/services/search_service.py:49-57`:

```python
series_patterns = [r"s\d{1,2}", r"s\d{1,2}e\d{1,3}", ...]
for pattern in series_patterns:
    if re.search(pattern, query_lower):
```
7 паттернов, вызывается 1× на detect — ок, но precompile всё равно стоит.

## PERF-06 — `detect_content_type`: 3 параллельных lookup даже на очень короткие запросы (MED)

Файл: `bot/services/search_service.py:64-69`
Для запроса типа `"d"` (2 символа) запускается 3 lookup'а. Prowlarr API может быть медленным, Lidarr/MusicBrainz — тоже. Heuristic: если `len(query) < 4`, сделать только prowlarr.search без detect.
См. также LOGIC-10.

## PERF-07 — `format_*` линейные O(N) без batching (LOW)

Файл: `bot/ui/formatters.py:*`
Formatters линейные, для 500 SearchResult это `join` 500 строк — миллисекунды. Не hot path.

## PERF-08 — `get_torrents()` на каждый callback handle (HIGH)

Файл: `bot/handlers/downloads.py:188, 223, 563`
Handler refresh, page, filter, torrent details — каждый делает полный `get_torrents()`. Для 100 торрентов × 2KB JSON = 200KB на каждый клик. При частом refreshing sub-секундные клики → 10+ MB/min.
**Решение:** TTL cache (5 сек) на уровне handler; или move в state-shared `QBittorrentCache` сервис.

## PERF-09 — `get_torrent_by_short_hash` всегда fetch all (HIGH)

Файл: `bot/clients/qbittorrent.py:292-298`
```python
async def get_torrent_by_short_hash(self, short_hash: str):
    torrents = await self.get_torrents()
    for t in torrents:
        if t.hash.lower().startswith(short_hash.lower()):
            return t
```
Для 500 торрентов — full fetch при каждом click на `t:abc123` callback. qBit API: `/api/v2/torrents/info?hashes=<full_hash>` даёт single result, но нам дан только `short_hash` (первые 16 символов).
**Решение:** локальный короткоживущий кэш hash-map (см. PERF-08).

## PERF-10 (НОВЫЙ) — Session save/load: JSON round-trip для каждого pagination click (HIGH)

Файл: `bot/db.py:235-289`, `bot/handlers/search.py:337-338`
При каждом `page:`, `rel:`, `back` handler делает:
1. `get_session` (SQL SELECT + json.loads + SearchSession.model_validate 500 items)
2. `save_session` (model_dump_json + SQL UPSERT)

500 SearchResult × pydantic validate = ~50ms. Не катастрофа, но на slow disk (SD-card на rpi4) видно.
**Решение:** session-cache в памяти с TTL; или урезать session.results до MINIMUM (guid + title + score), остальное по request.

## PERF-11 (НОВЫЙ) — Trending cache key по `tmdb_id` но overwrite при каждом refresh (MED)

Файл: `bot/handlers/trending.py:93-97, 143-147`
`_trending_movies_cache.update({movie.tmdb_id: movie for movie in movies})` — **replaces** прошлые entries. Если пользователь A увидел top-10 в час X, user B в час X+1 (другой top-10), cache будет содержать union с устаревшими entries. Защита от размера есть (200), но логика стирания "всё" при overflow груба.
**Решение:** LRU по user_id+tmdb_id.

## PERF-12 (НОВЫЙ) — `NotificationService._check_for_completions` делает full fetch каждые 60 сек (MED)

Файл: `bot/services/notification_service.py:136-175`
Каждые `notify_check_interval=60` сек fetch всех torrents. qBit поддерживает `sync/maindata` с `rid`-diff protocol — lighter, но сложнее.
**Решение:** оставить как есть (60s не hot-path), либо mov to `/sync/maindata`.

## PERF-13 (НОВЫЙ) — `TMDbClient` использует `httpx.AsyncClient(proxy=...)` только при `_get_client()` без re-check (LOW)

Файл: `bot/clients/tmdb.py:29-39`
`self._client` создаётся 1 раз. Если `tmdb_proxy_url` меняется (hot-reload settings через rare admin command) — нет перезахода. В prod рестартом контейнера — ок.

## PERF-14 (НОВЫЙ) — `SearchResult.model_copy(update={"calculated_score": score})` — full copy (LOW)

Файл: `bot/services/scoring.py:252-254`
В `sort_results` для каждого result делается `model_copy` — создаётся новый pydantic object. Для 500 items — 500 copies. Быстрее мутировать `r.calculated_score = score` напрямую (mutable field in pydantic v2 требует `model_config = ConfigDict(...)` или просто setattr).

## PERF-15 (НОВЫЙ) — Prowlarr search с `limit=100` по всем indexer'ам параллельно — зависит от Prowlarr (MED)

Файл: `bot/clients/prowlarr.py:32, 66`
`timeout=60s` на search. Это блокирует event-loop (в смысле пользователь ждёт 60s). Слишком много.
**Решение:** уменьшить до 30s + явный user-message «долго ищу».

## Итого

HIGH: PERF-02, PERF-08, PERF-09, PERF-10
MED: PERF-03, PERF-05, PERF-06, PERF-11, PERF-12, PERF-15
LOW: PERF-04, PERF-07, PERF-13, PERF-14
