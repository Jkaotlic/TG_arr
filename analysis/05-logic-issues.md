# Logic / Architecture Issues — TG_arr (Round 2)

Дата: 2026-04-18.

## LOGIC-01 — God-file `bot/ui/formatters.py` (HIGH, deferred)

Файл: `bot/ui/formatters.py` — ~1000 строк, один класс `Formatters` со статиками для torrent/movie/series/artist/album/calendar/emby/action_log/qbittorrent/trending.
Проблема: изменения одной подсистемы требуют чтения всего файла, git-blame бесполезен, конфликты merge неизбежны.
**Решение (deferred):** разбить на `bot/ui/formatters/`: `common.py` (progress_bar, _e, format_bytes refs), `search.py`, `torrent.py`, `media.py`, `calendar.py`, `music.py`, `emby.py`.

## LOGIC-02 — God-file `bot/ui/keyboards.py` (HIGH, deferred)

Файл: `bot/ui/keyboards.py` — ~900 строк. То же самое.
**Решение (deferred):** разбить параллельно с formatters.

## LOGIC-03 — God-file `bot/handlers/search.py` + music.py + trending.py дублируют grab-логику (HIGH, deferred)

Файлы: `bot/handlers/search.py` (755), `bot/handlers/music.py` (331), `bot/handlers/trending.py` (468).
- trending.py инлайнит `add_service = AddService(...)` и не использует `get_services()` — дублирует shape.
- music.py отдельно делает `_get_music_services()` (свой variant) — близко, но не идентично.
- `_execute_grab` в search.py обрабатывает ТОЛЬКО MOVIE/SERIES, music-grab-flow в music.py идёт через отдельный path.

**Решение (deferred):** единый `GrabOrchestrator` с dispatch по `content_type`.

## LOGIC-04 — Дублирование `RadarrClient._parse_movie` / `SonarrClient._parse_series` / `LidarrClient._parse_artist` (MED, deferred)

Файлы: `bot/clients/radarr.py:265-311`, `bot/clients/sonarr.py:306-366`, `bot/clients/lidarr.py:245-292`
Один и тот же pattern: images (poster/fanart), ratings, parse fields. Можно вынести в `bot/clients/_arr_common.py` (ArrBaseClient с `_parse_images`, `_parse_ratings`).
**Решение (deferred):** ArrBaseClient.

## LOGIC-05 — `grab_movie_release` / `grab_series_release` / `grab_music_release` почти идентичны (MED, deferred)

Файл: `bot/services/add_service.py:278-568, 624-748`
Различия: клиент (radarr/sonarr/lidarr), category в qBit ("radarr"/"tv-sonarr"/"music"), content_type в ActionLog, tvdb/tmdb/mb_id, monitor logic для series.
**Решение (deferred):** unified `_grab(release, target_client, category, action_type, ensure_content_fn)`.

## LOGIC-06 — `parse_query` возвращает dict, не dataclass (LOW)

Файл: `bot/services/search_service.py:233-289`
`dict` слабо типизирован. Потребитель не проверяется mypy.
**Решение:** `ParsedQuery` dataclass.

## LOGIC-08 — Magic numbers разбросаны (MED)

Примеры:
- `MAX_MSG_LEN = 3800` (formatters.py:951) — hard-coded
- `TORRENTS_PER_PAGE = 5` (downloads.py:19)
- `MAX_REQUESTS_PER_MINUTE = 30` (auth.py:15)
- `MAX_QUERY_LENGTH = 200` (search.py:128, music.py:87)
- `limit=100` в Prowlarr.search (prowlarr.py:32)
- `limit=10` в trending (trending.py:100)
- `max_length=500` в SearchSession (models.py:276)
- `hours=24`, `days=7` в cleanup (main.py)
- `_MAX_CACHE_SIZE=200`, `_MAX_USER_PERIOD_ENTRIES=100`
- `8640000` (eta constant в models.py:384) — безымянный sentinel для qBit "∞"

**Решение:** вынести в `bot/constants.py`; где уместно — в Settings с env-override.

## LOGIC-09 — `process_search` не использует `parsed["title"]` (MED)

Файл: `bot/handlers/search.py:147-158, 193`
`parsed = search_service.parse_query(query)` извлекает title/year/season, но в `search_service.detect_content_type(parsed["title"])` — используется title (ок), а в `search_service.search_releases(query, content_type)` (line 193) передаётся **оригинальный `query`** с годом/season внутри. Это mismatch: detection делается по cleaned title, а search — по полному query. Prowlarr получит lower-quality match для мультиязычных пользователей (русский title + год), т.к. indexer думает что `2024` — часть названия.

## LOGIC-10 — `SearchService.detect_content_type` делает 2-3 параллельных lookup даже на короткие уточнённые запросы (MED)

Файл: `bot/services/search_service.py:36-104`
Для запроса `Дюна 2021` (clearly movie by context — есть год) делается lookup в Radarr **и** Sonarr **и** Lidarr параллельно. На медленных сетях это 3 HTTP-запроса × 5s timeout. Нет heuristic-shortcut для year-only или artist-like.

## LOGIC-11 — `detect_content_type` сравнивает topN=3 только — может пропустить match (LOW)

Файл: `bot/services/search_service.py:90-102`
`artists[:3]`, `movies[:3]`, `series[:3]` — если искомый артист на 4-й позиции (Lidarr relevance иногда плохой), он не выявится. Приоритеты: music > movie > series — артист побеждает, если match в top-3.
**Решение:** top-5 или использовать similarity score.

## LOGIC-12 (НОВЫЙ) — Inconsistency в `from bot.models` import usage (LOW)

Файлы: `bot/ui/formatters.py:557` — `from bot.models import format_bytes` внутри функции (deferred import), в то время как вверху уже импортированы многие models. Непоследовательно, но не критично.

## LOGIC-13 (НОВЫЙ) — `TorrentInfo.eta_formatted` возвращает `"∞"` для `eta == 8640000` — magic (LOW)

Файл: `bot/models.py:384`
Magic sentinel от qBittorrent. Нет комментария.
**Решение:** `QBT_ETA_INFINITY = 8640000` константа с docstring.

## LOGIC-14 (НОВЫЙ) — `Formatters.format_download_complete_notification` использует `torrent.save_path` из qBit (сырой путь) — может запутать пользователя (LOW)

Файл: `bot/ui/formatters.py:617-630`
Если qBit раскладывает в `/downloads/incomplete/.../`, пользователь получит путь внутри Docker-контейнера, не host-путь. Минорно.

## LOGIC-15 (НОВЫЙ) — `bot/handlers/trending.py: handle_add_series_from_trending` делает второй `add_service = AddService(...)` с нуля (MED)

Файл: `bot/handlers/trending.py:319, 403`
Каждый callback создаёт новый экземпляр AddService. В `search.py` `get_services()` тоже создаёт (через singleton-клиенты, но service-wrapper нов каждый раз). ScoringService создаётся при каждом get_services().
**Решение:** закешировать `AddService`/`SearchService` как lru-singleton (или factory).

## LOGIC-16 (НОВЫЙ) — `_row_to_user` fallback на `UserRole.USER` при bad role (OK), но не логирует (LOW)

Файл: `bot/db.py:168-174`
`except ValueError: role = UserRole.USER` — без `logger.warning`. При corrupt-data не будет сигнала в метриках.

## LOGIC-17 (НОВЫЙ) — `process_music_search` не обрабатывает status_msg при ошибке перед `edit_text` (LOW)

Файл: `bot/handlers/music.py:106-111`
```python
status_msg = await message.answer("🔍 Ищу артистов в Lidarr...")
try:
    artists = await search_service.lookup_artist(query)
except Exception as e:
    ...
    await status_msg.edit_text(Formatters.format_error(...))
    return
```
OK. Но если `message.answer` упадёт, `status_msg` undefined — UnboundLocalError. Крайний случай.

## LOGIC-18 (НОВЫЙ) — Sonarr `monitor_type` не ограничен в `grab_series_release`, но валидация на стороне Sonarr (LOW)

Файл: `bot/services/add_service.py:422, 471-474`
Если handler передаст нерасположенный `monitor_type="lol"`, Sonarr API вернёт 400. Обрабатывается как ServiceConnectionError. Минор.

## LOGIC-19 (НОВЫЙ) — `check_qbittorrent` vs `check_service` — дубликат (LOW)

Файл: `bot/handlers/status.py:105-122`
Обе функции идентичны (см. BUG-31/DEAD). Убрать.

## LOGIC-20 (НОВЫЙ) — `_execute_grab` передаёт selected_content, но при "back"-button selected_content стирается → при повторном CONFIRM_GRAB snap-refetch (MED)

Файл: `bot/handlers/search.py:668-669, 559-566`
`handle_back` делает `session.selected_result = None; session.selected_content = None`. После back пользователь может нажать `CONFIRM_GRAB` напрямую (если он был на release_details screen) — но UI обычно не даёт такой flow. Low risk.

## Итого

- deferred (architectural refactor, ≥4 files): LOGIC-01, LOGIC-02, LOGIC-03, LOGIC-04, LOGIC-05
- fixes (single-module/behavior): LOGIC-08, LOGIC-09, LOGIC-10, LOGIC-15 — MED
- LOW: LOGIC-06, LOGIC-11, LOGIC-12, LOGIC-13, LOGIC-14, LOGIC-16, LOGIC-17, LOGIC-18, LOGIC-19, LOGIC-20

HIGH (deferred): 3 (god files)
MED: 5 (fixes) + 2 (deferred refactor for Arr dup)
LOW: 10
