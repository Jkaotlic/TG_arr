# Observability Audit — TG_arr

Дата: 2026-04-18.

## OBS-01 — Log coverage в handlers: user_id/chat_id пропускаются (HIGH)

Файлы: большинство handlers.

`bot/middleware/auth.py:LoggingMiddleware` обогащает события `user_id` и `chat_id`/`data` через `log.bind`, но этот `log` — локальная переменная; внутри handler'а при вызове `logger.error(...)` используется **module-level** `logger = structlog.get_logger()` без bind'ов.

Пример: `bot/handlers/downloads.py:77-81`
```python
except Exception as e:
    logger.error("Failed to get downloads", error=str(e))
```
Без user_id. При инциденте невозможно ответить «кто это сделал».

Примеры аналогичные: `search.py:260, 459, 648`; `music.py:109, 252, 288`; `trending.py:107, 158, 188, 302, 360`; `calendar.py:47-62`; `settings.py:*`; `emby.py:87`.

**Решение:** либо использовать `structlog.contextvars` + `clear_contextvars`/`bind_contextvars` в middleware (у нас есть `merge_contextvars` в `main.py:35`); либо в каждом handler'е `log = logger.bind(user_id=...)` как делает `search.py:process_search`.

**Половина пути уже пройдена** (`structlog.contextvars.merge_contextvars` в setup_logging) — но middleware **не кладёт** значения в contextvars. Нужно:
```python
# auth.py LoggingMiddleware
structlog.contextvars.bind_contextvars(user_id=user_id, chat_id=chat_id)
```

## OBS-02 — Structured vs f-string logs непоследовательны (MED)

Примеры inconsistency:
- `bot/handlers/search.py:260` — `log.error("Search failed", error=str(e))` (structured — OK)
- `bot/handlers/downloads.py:77` — `logger.error("qBittorrent error", error=str(e))` (structured — OK)
- `bot/handlers/history.py:42` — `logger.error("Failed to load history", error=str(e))` (OK)
- `bot/clients/prowlarr.py:85` — `log.error("Search failed", error=str(e))` (OK)
- `bot/handlers/trending.py:188` — `logger.error("Failed to lookup movie", tmdb_id=tmdb_id, error=str(e))` (OK)
- `bot/handlers/emby.py:87` — `logger.error("Failed to get Emby status", error=str(e))` — no error type

В целом нормально. Проблема: нет единого поля `exception` / `exc_info=True`. При JSON-логах трейсбек теряется. `LoggingMiddleware` (`auth.py:132`) использует `exc_info=True`, что ok — но это только верхний уровень.

**Решение:** везде `log.error(..., exc_info=True)` для exception-branches.

## OBS-03 — Метрики отсутствуют (MED)

Нет `prometheus_client`, `statsd`. Невозможно мониторить:
- QPS по handler'у
- latency distribution (p50/p95/p99) для API вызовов к *arr/Prowlarr/qBit
- error rate
- active users, concurrent sessions
- torrent-monitor loop health

Для однопользовательской whitelist-модели (10-20 users) можно обойтись без prometheus, но минимум:
- counter "api_call{service=...}" increment в BaseAPIClient
- histogram "api_latency" через `elapsed_ms` уже логируется

**Решение:** добавить опциональный `prometheus_client` exporter на `:9090/metrics`. Оставить как `deferred` — это не багфикс.

## OBS-04 — Log levels не используют WARNING для retry (MED)

Файл: `bot/clients/base.py:157, 177, 181`
Retryable-ошибки (429/500/502/503/504) логируются как `log.warning("Retryable HTTP error")` — ок. Но `TimeoutException`/`ConnectError` также `log.warning("will retry if attempts remain")`. Хорошо, что помечены WARNING. 👍

Противоположно: `bot/clients/qbittorrent.py:*` использует `log.error` даже для retryable, нет WARNING для retry-attempts.

**Решение:** в qbittorrent.py тоже разделить WARNING (retry) vs ERROR (final).

## OBS-05 — Error sampling: все трейсы в логе (LOW)

`LoggingMiddleware` кладёт `exc_info=True` для всех exceptions. Если spam-ошибка (например, Prowlarr down → каждый search exception), лог превращается в flood.
**Решение:** rate-limit per error-type, или счётчик `"logged_once_per_minute"`.

## OBS-06 — Невозможно отладить prod инцидент: «что пользователь искал» (HIGH)

Сценарий: пользователь пожаловался «не добавился фильм X». Логи показывают `Grab failed user_id=... error=...`. Но:
- Не логируется, какой именно SearchResult выбран (title, guid, indexer, score)
- Не логируется, каким был download_url перед fallback (есть `_mask_url` — ok)
- ActionLog в БД содержит `content_title`, `release_title`, `error_message`, `content_id`. Хорошо, но **нет `release_download_url_masked`**, нет `rejections`. При mystery-rejection оператор видит только «Отклонено» без деталей.

**Решение:** расширить ActionLog или добавить отдельное поле `details: dict` (JSON в SQLite) — туда класть `rejections`, `fallback_used`, `attempted_paths`.

## OBS-07 — Нет request_id / trace_id в логах (MED)

Каждый handler-вызов → много вложенных await'ов → логи из разных слоёв без корреляции.
**Решение:** в `LoggingMiddleware` сгенерировать `uuid4()` → `bind_contextvars(request_id=...)`. `merge_contextvars` уже в setup — всё что нужно.

## OBS-08 (НОВЫЙ) — `notify_check_interval` и другие циклы не отчитываются о последней итерации (LOW)

Файл: `bot/services/notification_service.py:92-111`
При 60s loop, если бот застрял в `qbt.get_torrents()` на 50s, невозможно понять по логам. Нужен heartbeat: `log.debug("monitor_tick", iter=N, duration_ms=...)`.

## OBS-09 (НОВЫЙ) — `AuthMiddleware` молча пересоздаёт пользователя при conflict (LOW)

Файл: `bot/middleware/auth.py:84-89`
```python
except Exception as e:
    logger.debug("User creation conflict, re-fetching", user_id=user_id, error=str(e))
```
`DEBUG` — скрыт в prod (JSON-renderer выше INFO). Не видно сколько таких conflict'ов.

## OBS-10 (НОВЫЙ) — Нет централизованного логирования для `TelegramBadRequest` (LOW)

Файлы: `bot/handlers/downloads.py:201-203, 242-244, 278-281, 536-538, 576-578, 616-618`
"message is not modified" повторяется 6+ раз. Не логируется вообще. Если появится новый `TelegramBadRequest` тип (`message too long`, `entity not found`), он проглотится raise-ом вне exception-filter.

**Решение:** helper `safe_edit_text(callback, text, ...)` который логирует все `BadRequest`, фильтрует `not modified`.

## Итого

HIGH: OBS-01, OBS-06
MED: OBS-02, OBS-03, OBS-04, OBS-07
LOW: OBS-05, OBS-08, OBS-09, OBS-10

Общий вывод: проект использует structlog правильно на уровне setup, но middleware не кладёт идентификаторы в contextvars → обычные `logger.error` в handler'ах без context. Плюс нет request_id и нет метрик. Prod-инцидент отлаживать тяжело, но не невозможно.
