# Dead Code Audit — TG_arr (Round 2)

Дата: 2026-04-18.

Закрыто: DEAD-01, 02, 03, 16, 17, 25.

## DEAD-04 — `CallbackData.SERIES` / `MOVIE` / `RADARR_PROFILE` и т.п. — часть уже удалена (LOW)

Файл: `bot/ui/keyboards.py:20-100`, `bot/handlers/*`

В `Keyboards.series_list` (line 256) есть ссылка на `CallbackData.SERIES` — но **этого атрибута в классе `CallbackData` больше нет**. Метод `series_list` вызывается только из `Keyboards`-самообразно — поиск показывает, что он **не вызывается нигде** в handlers. Скорее всего dead после рефакторинга. При попытке вызвать — `AttributeError`.
**Решение:** удалить `Keyboards.series_list` (метод целиком).

## DEAD-05 — `bot/clients/base.py: BaseAPIClient._get_http_timeout` дублирует logic (LOW)

Файл: `bot/clients/base.py:69-72`
Метод вызывается 1 раз в `_get_client()` (строка 81). Можно встроить. Минор.

## DEAD-06 — `bot/models.py:format_bytes/format_speed` не вынесены в helper-модуль (LOW)

Файл: `bot/models.py:474-495`
Утилитарные функции в файле моделей. Работает, но не идейно. Минор — не dead code, а архитектура.

## DEAD-07 — `bot/services/scoring.py: ScoringService.filter_by_quality` не вызывается (MED)

Файл: `bot/services/scoring.py:285-324`
Метод `filter_by_quality` реализован, протестирован (test_scoring.py), но **не вызывается в production-коде** (grep: только `tests/test_scoring.py`). Пользователь `preferred_resolution` в `UserPreferences` сохраняется, но никогда не применяется. То есть настройка «Предпочитаемое разрешение» в UI **ничего не делает**.
**Решение:** либо применять фильтр в `search_releases`, либо скрыть UI опцию.

## DEAD-08 — `bot/handlers/downloads.py:confirm_delete_torrent keyboard` не используется (LOW)

Файл: `bot/ui/keyboards.py:692-711`
Метод `confirm_delete_torrent` создаёт keyboard для подтверждения удаления. Grep: вызывается только в тестах. В handler'ах `handle_delete_torrent` сразу удаляет без confirmation. Очень опасно для `t_delf` (удаление с файлами). Либо реализовать confirmation flow, либо удалить keyboard.

## DEAD-09 (НОВЫЙ) — `SearchService.get_artist_by_mbid` не вызывается (LOW)

Файл: `bot/services/search_service.py:203-211`
Метод есть, но в коде не используется. `music.py` вызывает `search_service.lookup_artist`, а не `get_artist_by_mbid`.
**Решение:** либо использовать (при click на trending-artist запросить по mb_id для enrichment), либо удалить.

## DEAD-10 (НОВЫЙ) — `SearchService.get_movie_by_tmdb`/`get_series_by_tvdb` не вызываются (LOW)

Файл: `bot/services/search_service.py:213-231`
Методы есть, но production-код вызывает `radarr.lookup_movie_by_tmdb` / `sonarr.lookup_series_by_tvdb` напрямую (trending handler). Дубликат слоя, не нужен.

## DEAD-11 (НОВЫЙ) — `SearchService.lookup_album` не вызывается (LOW)

Файл: `bot/services/search_service.py:197-201`
Album-lookup присутствует в API SearchService, но в handler'ах не используется (music_handler только artist-lookup).
**Решение:** ok как публичный API для будущего; либо удалить.

## DEAD-12 (НОВЫЙ) — `bot/clients/deezer.py: search_artist` не вызывается (LOW)

Файл: `bot/clients/deezer.py:76-96`
Метод `search_artist` реализован, но production-код использует только `get_trending_artists/albums`. Мёртв.
**Решение:** удалить или использовать для enrichment.

## DEAD-13 (НОВЫЙ) — `bot/clients/lidarr.py: get_all_artists` не вызывается (LOW)

Файл: `bot/clients/lidarr.py:70-75`
Не используется нигде. Dead.

## DEAD-14 (НОВЫЙ) — `bot/clients/lidarr.py: push_release` не вызывается из handler flow (INFO)

Файл: `bot/clients/lidarr.py:140-157`
Используется только из `add_service.grab_music_release`, что ок. Не мёртв, просто отметка.

## DEAD-15 (НОВЫЙ) — `bot/clients/emby.py: install_update, restart_server, get_scheduled_tasks` — только admin, но UI есть не для всех

Файл: `bot/clients/emby.py:192-218`
`get_scheduled_tasks` **нигде не используется в handler**. UI нет. Dead.

## DEAD-18 (НОВЫЙ) — `bot/services/notification_service.py: force_check` возвращает список, никто не вызывает (MED)

Файл: `bot/services/notification_service.py:200-236`
Метод реализован (вернёт newly-completed torrents), но не вызывается — нет `/check`/`/notify-now` команды. Mожно удалить или добавить команду.

## DEAD-19 (НОВЫЙ) — `MENU_SEARCH` / `MENU_DOWNLOADS` etc. разбросаны по handler'ам (LOW)

Файлы: `bot/handlers/start.py:15-20`, `bot/handlers/search.py:31`, `bot/handlers/downloads.py:22-23`, etc.
Константы дублируются в каждом файле. Не dead, но consolidate в единый `bot/ui/menu.py`.

## DEAD-20 (НОВЫЙ) — `from bot.clients.registry import get_prowlarr, get_radarr, get_sonarr` в `music.py` (LOW)

Файл: `bot/handlers/music.py:10`
В `music.py` импортируется `get_prowlarr, get_radarr, get_sonarr`, но используются только в `_get_music_services`. Вынесены "на всякий случай", т.к. создаётся `AddService(prowlarr, radarr, sonarr, ...)`. ОК, но бросается в глаза.

## DEAD-21 (НОВЫЙ) — `bot/services/search_service.py: parse_query` — quality удаляется из title, но не используется (LOW)

Файл: `bot/services/search_service.py:279-284`
`parsed["quality"]` извлекается, но после `parse_query` ничего с ним не делает — в `process_search` используется только `parsed["season"]` и `parsed["title"]`. Таким образом удаление `1080p` из title лишено смысла (позже всё равно запрос идёт в `search_releases(query, ...)`, а не `(parsed["title"], ...)`).
**Решение:** передать `parsed["title"]` в Prowlarr вместо оригинального query (это уже LOGIC-issue).

## DEAD-22 (НОВЫЙ) — `bot/clients/radarr.py: search_movie` не вызывается из handler напрямую (INFO)

Файл: `bot/clients/radarr.py:178-185`
Используется в `add_service.grab_movie_release` (fallback search). Не dead.

## DEAD-27 — `gcc` в Dockerfile (LOW, из прошлого)

Файл: `Dockerfile:6-8`
`gcc` нужен **только** если кому-то из deps требуется сборка C-extension. В прошлом аудите отмечено что `aiosqlite` имеет wheels. На slim с Python 3.12 all deps имеют wheels. Можно убрать `gcc` → -150MB image size.
**Решение:** проверить `pip install --only-binary :all:` на CI, если ок — удалить `gcc`.

## DEAD-28 (НОВЫЙ) — `bot/ui/formatters.py: format_search_result` не используется (LOW)

Файл: `bot/ui/formatters.py:38-76`
`format_search_result` реализован и протестирован, но production вызывает `format_search_results_page`, который делает то же самое inline через `format_search_result`. Подождите, перепроверяю: `format_search_results_page` (line 92-95) вызывает `Formatters.format_search_result(result, ...)`. Не dead. Отзываю.

## DEAD-29 (НОВЫЙ) — `bot/ui/formatters.py: format_torrent_compact` не используется (LOW)

Файл: `bot/ui/formatters.py:603-608`
Grep показывает использование только в тестах. В handler'ах — нет. Dead.

## DEAD-30 (НОВЫЙ) — Лог-сообщение `"Deleted torrents"` в qbittorrent.py, но action не логируется в БД (INFO)

Файл: `bot/clients/qbittorrent.py:343`
`logger.info("Deleted torrents", ...)` — ok, но `handlers/downloads.py:handle_delete_torrent` не вызывает `db.log_action` — нет записи в action-log БД. Это feature gap, не dead.

## DEAD-31 (НОВЫЙ) — `Keyboards.series_list` использует несуществующий `CallbackData.SERIES` (CRIT если вызвать)

Файл: `bot/ui/keyboards.py:256`
```python
callback_data=f"{CallbackData.SERIES}{s.tvdb_id}",
```
`CallbackData.SERIES` не определён — `AttributeError` при вызове. `series_list` не вызывается нигде → dead code, но if someone reinstates — crash. Удалить метод (см. DEAD-04).

## DEAD-32 (НОВЫЙ) — `bot/clients/base.py: _post_no_retry` возвращает `{"raw": response.text}` при не-JSON (INFO)

Файл: `bot/clients/base.py:248-251`
Не dead, но downstream-код в `radarr.grab_release` делает `result if isinstance(result, dict) else {}`. `{"raw": "..."}` — dict → пройдёт. Но такой результат никогда не проверяется на `approved` key, поэтому автоматически трактуется как rejected. Minor.

## Итого

Всего: ~19 уникальных находок.
- CRIT-if-invoked: 1 (DEAD-31)
- MED: 2 (DEAD-07, DEAD-18)
- LOW/INFO: остальные 16

Приоритет: DEAD-07 (preferred_resolution — user-visible broken feature), DEAD-31 (dead + crash-prone), DEAD-27 (Dockerfile slim).
