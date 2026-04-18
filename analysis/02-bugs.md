# Bugs Audit — TG_arr (Round 2)

Дата: 2026-04-18.

Закрыто: BUG-01, 04, 05, 17, 19, 23, LOGIC-07.

## BUG-10 — `cleanup_old_searches` не чистит результаты в `search_results` при частичной ошибке (HIGH)

Файл: `bot/db.py:387-410`

Код использует `BEGIN` + `rollback()`, но с `isolation_level=None` (autocommit mode) SQLite считает `BEGIN` началом явной транзакции — OK. Но `cursor.rowcount` читается **после** `commit()`, у aiosqlite это может вернуть `-1` на старых версиях. Более серьёзно: при `execute(DELETE FROM search_results WHERE search_id IN (SELECT id FROM searches WHERE created_at < ?))` и последующем `DELETE FROM searches WHERE created_at < ?` — если между двумя DELETE сеанс добавил новый search (маловероятно, но возможно), он будет удалён вместе со **связанными** `search_results`. На практике OK.
**Решение:** поменять порядок — сначала сохранить IDs в tmp-list, потом удалить.

## BUG-11 — Timezone mix в `Formatters._extract_date_key` (MED)

Файл: `bot/ui/formatters.py:958-967`

`datetime.fromisoformat(date_str.replace("Z", "+00:00"))` возвращает tz-aware, но `today = now.date()` в `format_calendar` — naive local date. Сравнение в `_format_date_header` делается через `dt_date - today` — `date - date` → `timedelta`, ОК. Однако если `air_date` пришёл в UTC (e.g. `2026-04-19T22:00:00Z` = Moscow `2026-04-20T01:00:00`), `dt.strftime("%Y-%m-%d")` даст `2026-04-19`, а пользователь в MSK ждёт `2026-04-20`. Календарь съезжает на один день для поздних эпизодов.
**Решение:** конвертировать в `settings.timezone` до `strftime`.

## BUG-12 — Message length exceeded truncation prone (MED)

Файл: `bot/ui/formatters.py:951-955`, `format_calendar`
`MAX_MSG_LEN = 3800`, но Telegram hard-limit **4096**. Truncation `rsplit('\n', 1)[0]` может обрезать в середине HTML-тэга (`<b>...<`) → Telegram `Bad Request: can't parse entities`. Также нет truncation в `format_torrent_list`, `format_trending_movies`, `format_action_log` — при большом количестве сущностей сообщение превысит 4096 и упадёт.
**Решение:** safe-truncation с регексом, закрывающим открытые теги; либо HTML-strip-after-position.

## BUG-14 — Rejected release возвращает success=True при fallback search (MED)

Файл: `bot/services/add_service.py:404-412, 548-558, 734-742`

Когда release отклонён, direct grab не удаётся, qBittorrent нет — выполняется `search_movie/search_series/search_artist` (триггерит автопоиск) и возвращается `success=True, msg="Запущен автопоиск"`. Но действие пользователя было «захватить конкретный релиз». Он увидит «Запущен автопоиск» и может подумать, что конкретный релиз грабнулся.
**Решение:** возвращать флаг `fallback_used=True` и писать юзеру: «Выбранный релиз отклонён/не удалось захватить. Запущен автопоиск Radarr».

## BUG-15 — `recursive callback` в downloads handler (HIGH)

Файл: `bot/handlers/downloads.py:309-311, 338-339`

`handle_pause_torrent` и `handle_resume_torrent` после действия вызывают `await handle_torrent_details(callback)`, который ещё раз `await callback.answer()`. При вторичном вызове Telegram вернёт `TelegramBadRequest: query is too old` — обычно тихо проигнорируется, но `callback.answer()` уже был вызван (`await callback.answer(f"⏸️ Приостановлен: ...")`), второй раз — ошибка.
Также `handle_delete_torrent/handle_delete_with_files` → `handle_refresh(callback)` → тот опять делает `callback.answer("Обновляю...")`. Двойной answer → `TelegramBadRequest`.
**Решение:** не вызывать handler напрямую, а извлечь общую функцию без `answer()`.

## BUG-20 — `drop_pending_updates=True` при каждом рестарте (MED)

Файл: `bot/main.py:201`

При рестарте контейнера (Portainer deploy) все команды, отправленные между остановкой и стартом, теряются молча. Типичный сценарий: deploy 5 минут, пользователь пишет `/search`, при старте команды дропнуты. Пользователь не поймёт почему бот «проигнорировал».
**Решение:** `drop_pending_updates=False` по умолчанию; очищать только при явном флаге `DROP_PENDING_UPDATES=true`.

## BUG-24 (НОВЫЙ) — `SearchSession.results` max_length=500 но Prowlarr возвращает больше (MED)

Файлы: `bot/models.py:276`, `bot/clients/prowlarr.py:48`
`SearchResult.results: list[SearchResult] = Field(..., max_length=500)`; Prowlarr search вызывается с `limit=100` — OK. Но если query matches сотни релизов у разных indexers (nCore, RuTracker, HD-Torrents через Prowlarr-aggregator), ответ может быть >500. Pydantic `max_length=500` сработает **при десериализации из БД** → `get_session` упадёт с `ValidationError`, session очистится.
**Решение:** или убрать `max_length`, или truncate до 500 **перед** сохранением в session.

## BUG-27 — Recursive callback в `handle_music_confirm` vs `handle_confirm_grab` (HIGH)

Файлы: `bot/handlers/music.py:258-264`, `bot/handlers/search.py:497-514`

Оба роутера зарегистрированы на `F.data == CallbackData.CONFIRM_GRAB`. aiogram при match первого router'а (music — включён первым) вызывает handler; если selected_content не ArtistInfo — функция просто ничего не делает (`return`). Второй handler (`search.handle_confirm_grab`) **не вызовется**, т.к. aiogram не делает fall-through между router'ами на одной data по умолчанию для того же event. Результат: для movie/series кнопка «Скачать» тихо не работает после добавления music_router первым.
Проверка: комментарий `# music_router is included BEFORE search_router so that its CONFIRM_GRAB handler gets a chance to run first for music sessions.` — это предположение неверно в aiogram 3.x: router-приоритет не означает fallback-chain. Первый matching handler съест event.
**Решение:** вынести обработку в одну функцию с type-dispatch по `session.selected_content`. Либо использовать `SkipHandler()` в music handler для не-music сессий — но aiogram 3.x не даёт чистого API для этого.

## BUG-28 (НОВЫЙ) — BaseAPIClient retry не пересоздаёт `httpx.AsyncClient` при `is_closed` (LOW)

Файл: `bot/clients/base.py:74-83`
Если `_client.aclose()` вызвался из другого места (например, при shutdown / reconnect), при следующем `_request()` → `_get_client()` создаст новый. ОК. Но `tenacity.retry` сохраняет reference на предыдущий client через замыкание? Нет, не сохраняет — каждый attempt вызывает `_get_client()` заново. **Не баг.** (оставляю для полноты аудита, чтобы показать что проверял.)

## BUG-29 (НОВЫЙ) — `_parse_torrent` не учитывает new qBit v5 state names (MED)

Файл: `bot/clients/qbittorrent.py:22-43`

В qBittorrent v5 появились новые state'ы: `stoppedUP`, `stoppedDL`, `running`. `STATE_MAP` их не содержит → `TorrentState.UNKNOWN` → на UI юзер видит `❓`.
**Решение:** добавить mapping для v5-state'ов.

## BUG-30 (НОВЫЙ) — Concurrent user actions → double `session.selected_content` write (LOW)

Файл: `bot/handlers/search.py:404-426, 441-451`
Пользователь нажал `rel:0` и, не дожидаясь ответа, `rel:1`. Оба колбэка идут параллельно, оба делают `get_session()` → `set selected_result` → `save_session()`. Результат недетерминирован: последний выигрывает. На практике неощутимо.
**Решение:** либо per-user lock, либо optimistic — ок как есть.

## BUG-31 (НОВЫЙ) — `check_service` в `bot/handlers/status.py:86-102` ловит все exceptions, но `check_connection` сам уже ловит (DEAD)

Файл: `bot/handlers/status.py:86-102, 105-122`
`check_connection()` в каждом клиенте всегда возвращает `(False, None, elapsed)` при ошибке (никогда не бросает). Поэтому `try/except Exception` в `check_service` — dead code. Также `check_qbittorrent` — дубликат `check_service` для QBittorrentClient (один и тот же протокол `check_connection()`).
**Решение:** удалить `check_qbittorrent`, обе ветки использовать `check_service`.

## BUG-32 (НОВЫЙ) — season-monitor логика в `_execute_grab` ломает non-season-pack episodes (MED)

Файл: `bot/handlers/search.py:617-622`

```python
monitor_type = "all"
if result.detected_season is not None and not result.is_season_pack:
    monitor_type = "none"
```

Если пользователь грабит single episode S01E05, `monitor_type="none"` добавит сериал с **ни одним** сезоном замониторенным. Sonarr не будет заниматься future episodes, missing episodes не будут найдены. Правильное поведение — `"existing"` или `"future"`.
**Решение:** для single-episode release → `monitor_type="existing"` + `search_for_missing=False`; для season pack → `"all"`.

## BUG-33 (НОВЫЙ) — `add_series` в trending.py переопределяет `monitor_type="all"` без запроса пользователю (LOW)

Файл: `bot/handlers/trending.py:447`
В отличие от search flow, пользователь не может выбрать monitor_type из trending. Захардкожено `"all"`. ОК для UX, но не согласуется с search path.

## BUG-34 (НОВЫЙ) — `NotificationService._monitor_loop` не переподписывает новых пользователей (LOW)

Файл: `bot/services/notification_service.py:72-76`, `bot/main.py:71-74`
При старте `subscribe_user` для всех из allowlist. Если админ обновит `ALLOWED_TG_IDS` и рестартнёт бот, новые попадут — ок. Но если обновил **без** рестарта (тяжело, но теоретически), новый пользователь будет авторизован, но нотификаций не получит. Minor.

## BUG-35 (НОВЫЙ) — `_row_to_user` при corrupt JSON в preferences удаляет всю сессию? (LOW)

Файл: `bot/db.py:168-183`
Если `prefs_data` в БД повреждён (ручной edit), `UserPreferences(**prefs_data)` кинет `ValidationError`, это пробросится наружу → `get_user` провалится → middleware упадёт, юзер не сможет пользоваться ботом. Нет graceful fallback на default preferences.
**Решение:** `try: UserPreferences(**prefs_data); except: UserPreferences()`.

## BUG-36 (НОВЫЙ) — Trending series cache не включает TVDB-resolved series (MED)

Файл: `bot/handlers/trending.py:421-440`
Когда пользователь добавляет series из trending, `series.tvdb_id=0`, код делает `sonarr.lookup_series(series.title)` и находит match по `tmdb_id`. Но в cache (`_trending_series_cache`) остаётся старый объект с `tvdb_id=0`. При повторном click из того же trending-списка lookup будет повторён — лишний API call.
**Решение:** после resolve обновить cache.

## Итого

HIGH: 3 (BUG-15, BUG-27, BUG-24), MED: 8, LOW: 5.
