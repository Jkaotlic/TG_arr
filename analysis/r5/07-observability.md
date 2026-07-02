# Анализ наблюдаемости TG_arr (раунд 5)

Прочитаны все файлы `bot/` (main, config, webhook, db, models, middleware, 11 handlers, 10 clients, 4 services, ui/*), Dockerfile, docker-compose.yml.

## Критические

### OBS-01: stdlib-логгеры (aiogram/httpx/aiohttp) ломают JSON-поток и не настроены по уровням
- **Файл**: `bot/main.py:71-92`
- **Проблема**: `setup_logging()` конфигурирует только structlog. Для stdlib — лишь `logging.basicConfig(format="%(message)s", level=LOG_LEVEL)`. Итог: (1) aiogram (`Failed to fetch updates - TelegramNetworkError...` — подтверждено в проде), httpx (`HTTP Request: GET ... "HTTP/1.1 200 OK"` на INFO **на каждый API-вызов**), `aiogram.event` (`Update id=... is handled. Duration ...ms` на INFO на каждый апдейт) и `aiohttp.access` (access-лог webhook-сервера) пишут **голые строки без JSON, без timestamp, без уровня**; (2) уровни этих логгеров нигде не приглушены (`logging.getLogger("httpx").setLevel(...)` отсутствует — проверено grep'ом); (3) SEC-03-маскировка токена (`_mask_tokens`) действует **только** на structlog-пайплайн — stdlib-путь не маскируется, а именно aiogram-ошибки потенциально содержат URL с `bot<token>`.
- **Риск**: `docker logs | jq` спотыкается на голых строках; httpx/aiogram.event дублируют structlog-события и съедают лимит ротации (10 МБ); при сетевом флапе поток bare-строк маскирует полезные JSON-события; риск утечки токена мимо маскировщика.
- **Решение**: направить stdlib через `structlog.stdlib.ProcessorFormatter` (общий JSON-рендер + `_mask_tokens`), явно выставить `logging.getLogger("httpx").setLevel(WARNING)`, `httpcore` → WARNING, `aiogram.event` → WARNING, `aiohttp.access` → WARNING (или свой structured-лог в webhook-handler).
- **Статус**: [ ] Не исправлено

### OBS-02: приём webhook вообще не логируется
- **Файл**: `bot/webhook.py:80-91`
- **Проблема**: единственное событие всего входящего тракта — `webhook_notify_failed` (ошибка отправки в TG). Нет `webhook_received` (eventType, instanceName, service из `/webhook/{service}`); невалидный JSON → `400` молча; неизвестный/игнорируемый eventType (Grab, Health, Rename) → `200 ok` молча; успешная доставка нотификации тоже молчит.
- **Риск**: классический инцидент «настроил Connect→Webhook в Radarr, нотификации не приходят» недиагностируем из прод-логов.
- **Решение**: в `handle()` логировать `webhook_received` (INFO: event_type, service, matched=bool), `webhook_invalid_json` (WARNING, с remote IP), и `webhook_notified` (INFO) после успешного notify.
- **Статус**: [ ] Не исправлено

### OBS-03: «Sent completion notification» логируется даже когда отправка провалилась
- **Файл**: `bot/main.py:248-256` + `bot/services/notification_service.py:185-198`
- **Проблема**: обёртка `send_notification` в main.py ловит **все** исключения, пишет WARNING `"Failed to send notification"` и возвращает None. Поэтому в `_notify_completion` исключение никогда не долетает: ERROR-ветка (строки 193-198) — мёртвый код, а INFO `"Sent completion notification"` пишется **безусловно**, включая случаи фактического сбоя. Бонус: событие `"Failed to send notification"` существует в двух местах с разными уровнями (WARNING в main, ERROR в сервисе).
- **Риск**: прод-лог утверждает, что юзер уведомлён, хотя сообщение не ушло (бот заблокирован, flood-limit) — ложный след при разборе.
- **Решение**: не глотать исключение в обёртке (логировать и re-raise), либо возвращать bool и логировать успех только при True; удалить мёртвую ERROR-ветку.
- **Статус**: [ ] Не исправлено

### OBS-04: 89 из 92 `log.error(...)` — без `exc_info`, traceback теряется в catch-all
- **Файл**: примеры: `bot/services/add_service.py:474-478,634-638,824-828`; `bot/services/notification_service.py:109,134,175`; `bot/handlers/downloads.py:82,208,250` (и ~15 аналогичных); `bot/handlers/music.py:121,351`; `bot/handlers/trending.py:133,184,393,507`; `bot/main.py:342` («Bot crashed»)
- **Проблема**: `exc_info=True` присутствует лишь в 4 местах (auth.py:148, search.py:340/552/838). Все остальные `except Exception as e: log.error(..., error=str(e))` записывают одну строку. LoggingMiddleware с exc_info не спасает — эти хендлеры **сами** ловят исключение.
- **Риск**: неожиданный `TypeError`/`KeyError` в grab-цепочке или notification loop даёт в прод-логе только строку без stack trace — место падения не найти.
- **Решение**: добавить `exc_info=True` во все `log.error` внутри `except Exception` (catch-all); для ожидаемых `APIError/QBittorrentError` можно оставить без traceback.
- **Статус**: [ ] Не исправлено

## Средние

### OBS-05: нет единого терминального события исхода grab; `force_download` не логируется нигде
- **Файл**: `bot/services/add_service.py:454-461,470-472` (movie), `:608-615,630-632` (series), `:806-812,820-822` (music); `bot/handlers/search.py:727-840,918-947`
- **Проблема**: успешные пути логируются разными фразами («Release pushed successfully», «Release grabbed successfully», «Downloaded via qBittorrent...», «Triggered automatic search as fallback»), но два провальных терминала возвращаются **без итогового лога**: «релиз отклонён и qBit-fallback недоступен» и «Could not grab release or trigger search». Флаг `force_download` не попадает ни в одно событие (в `_execute_grab` и `handle_force_grab` логов нет вовсе). Исход пишется в БД (ActionLog), но не в docker logs.
- **Риск**: диагностика «нажал Скачать — ничего не скачалось» требует сопоставления 3-5 разноимённых событий; force-граб неотличим от обычного.
- **Решение**: одно INFO-событие `grab_completed` в конце `grab_*_release` (или `_execute_grab`): success, path=`push|direct|qbit|auto_search|rejected|failed`, force_download, content_type, rejections.
- **Статус**: [ ] Не исправлено

### OBS-06: notification loop — нет correlation id и ERROR-спам каждые 10 с при недоступном qBit
- **Файл**: `bot/services/notification_service.py:92-111,136-175`
- **Проблема**: (1) события цикла не имеют ни request_id (contextvars из middleware сюда не доходят — фоновая таска), ни собственного `component=` бинда; (2) при падении qBittorrent цикл логирует ERROR и ретраит через 10 с → ~360 одинаковых ERROR/час без backoff и дедупликации.
- **Риск**: лог забивается, ротация (10 МБ×3) вымывает полезные события; реальные ошибки тонут в шуме.
- **Решение**: в начале таски `structlog.contextvars.bind_contextvars(component="notification_service")` (аналогично для `_periodic_cleanup`, warmup); экспоненциальный backoff + логировать повторную ошибку раз в N циклов (счётчик consecutive_failures), восстановление — отдельным INFO.
- **Статус**: [ ] Не исправлено

### OBS-07: дубли-события и два стиля нейминга (подтверждено в проде)
- **Файл**: `bot/services/search_service.py:101,121` (`detect_content_type`) vs `:175` (`content_type_detected`); `bot/clients/prowlarr.py:150` («Search completed») vs `search_service.py:308` (`search_completed`); `bot/main.py:121` + `notification_service.py:73` («Notification service started» — дважды за один старт); `bot/clients/base.py:316` (`health_check_failed`) vs `radarr.py:323`/`sonarr.py:369`/`lidarr.py:331`/`emby.py:269`/`qbittorrent.py:541` («X health check failed») — 6 имён одного смысла
- **Проблема**: одно смысловое событие имеет 2-6 имён; snake_case сосуществует с Sentence case — оба стиля видны в прод-выгрузке.
- **Риск**: grep/jq-запросы по прод-логу пропускают половину случаев; «Notification service started» дважды выглядит как двойной запуск.
- **Решение**: конвенция snake_case для поля event; слить дубли; убрать дублирующий лог из `on_startup`.
- **Статус**: [ ] Не исправлено

### OBS-08: f-string вместо статичного имени события
- **Файл**: `bot/handlers/status.py:199` (`logger.warning(f"{name} health check failed", ...)`); `bot/handlers/calendar.py:67` (`logger.error(f"{source} calendar error", ...)`)
- **Проблема**: имя события динамическое; сервис должен быть kv-полем (`service=name`), как уже сделано в `base.py:316`.
- **Риск**: невозможно агрегировать/алертить по одному имени события.
- **Решение**: `logger.warning("health_check_failed", service=name, error=...)`, `logger.error("calendar_fetch_failed", service=source, ...)`.
- **Статус**: [ ] Не исправлено

### OBS-09: `get_stats()`/`force_check()` — мёртвый observability-код; нет периодической сводки-счётчиков
- **Файл**: `bot/services/notification_service.py:200-248`
- **Проблема**: `get_stats()` (running, tracked_torrents, subscribed_users) и `force_check()` не вызываются нигде в `bot/` (проверено grep). Счётчиков запросов к сервисам/ошибок за период нет вообще.
- **Риск**: нельзя из логов ответить «жив ли монитор и сколько торрентов трекается»; деградация видна только вручную.
- **Решение**: минимум — раз в N часов логировать INFO-сводку (`notification_stats` из get_stats()), либо удалить мёртвый код.
- **Статус**: [ ] Не исправлено

## Низкие

### OBS-10: старт фоновых тасок не логируется
- **Файл**: `bot/main.py:303-311` (liveness_task, cleanup_task, watchdog-thread)
- **Проблема**: `_periodic_cleanup`, `_liveness_touch` и watchdog-поток создаются молча; «жива ли таска» из логов не видно.
- **Риск**: тихо умершую/незапущенную таску не заметить (низкий).
- **Решение**: INFO `background_task_started` (task=...) при создании.
- **Статус**: [ ] Не исправлено

### OBS-11: WEBHOOK_* и PROWLARR_SEARCH_* не проброшены в docker-compose.yml
- **Файл**: `docker-compose.yml:8-47`
- **Проблема**: дубль DEPLOY-01 (см. 09-deployment.md) — webhook-фича в прод-деплое невключаема без правки compose.
- **Риск**: «config-gated» фича фактически недостижима в проде.
- **Решение**: см. DEPLOY-01.
- **Статус**: [ ] Не исправлено

### OBS-12: тихо проглоченные исключения без лога
- **Файл**: `bot/handlers/search.py:563-578` (`_emby_library_note`: таймаут/ошибка → `""` молча); `bot/handlers/downloads.py:288-295` (`_refetch_one`: `except Exception: refreshed = None`); `bot/handlers/downloads.py:138-139,166-167` (`cmd_pause`/`cmd_resume`: QBittorrentError отвечается юзеру, но не логируется)
- **Проблема**: best-effort-ветки без единого DEBUG-следа.
- **Риск**: низкий, но систематические таймауты Emby останутся незамеченными.
- **Решение**: DEBUG-лог в каждой глотающей ветке (`emby_note_skipped`, `refetch_failed`, `qbit_command_failed`).
- **Статус**: [ ] Не исправлено

### OBS-13: LOG_LEVEL=DEBUG переключает прод-формат на ConsoleRenderer
- **Файл**: `bot/main.py:84`
- **Проблема**: рендерер выбирается по уровню: DEBUG → ConsoleRenderer (ANSI, не JSON). Диагностический режим ломает парсинг ровно тогда, когда логи нужнее всего.
- **Решение**: отдельная переменная `LOG_FORMAT=json|console` (default json), не связанная с уровнем.
- **Статус**: [ ] Не исправлено

### OBS-14: нет итогового события «retries exhausted» в base-клиенте
- **Файл**: `bot/clients/base.py:113-118,204-210`
- **Проблема**: tenacity ретраит молча; attempt-номер не пишется и финальное «все попытки исчерпаны» отсутствует. У Prowlarr-поиска attempt логируется — у остальных нет.
- **Риск**: по логу не отличить «1 таймаут из 3, восстановился» от «все 3 попытки упали».
- **Решение**: `before_sleep_log`-хук tenacity или счётчик attempt в kv; итоговый WARNING `request_retries_exhausted`.
- **Статус**: [ ] Не исправлено

## Проверено — проблем нет

- **Ротация docker-логов**: `docker-compose.yml:50-54` — `json-file`, `max-size: 10m`, `max-file: 3` ✅.
- **Health/liveness**: HEALTHCHECK по свежести `/tmp/tgarr-alive` + OS-thread watchdog с дампом стеков перед `os._exit(1)` + команда `/health` — достаточно для single-host Pi.
- **request_id/user_id**: `LoggingMiddleware` (auth.py:118-151) биндит контекст в contextvars — сквозной до сервисов и клиентов (подтверждено прод-примером).
- **Latency**: `slow_api_call` WARNING >2с (base.py:157-162,285-290); `stage_done` с elapsed_ms в поисковом флоу; `prowlarr_ms` в `search_completed` ✅.
- **Маскировка секретов в structlog-пути**: `_mask_tokens`, `_mask_url`, `_safe_push_result` ✅.
- **`print()` в bot/ отсутствует**; formatters/keyboards/scoring — чистые от I/O.
- **БД-слой**: connect/close, WAL-checkpoint-fail, corrupt session — всё логируется; cleanup возвращает счётчики ✅.
- **Неавторизованный доступ и rate-limit** логируются WARNING с user_id/username ✅.

**Сводка приоритетов**: OBS-01 (JSON-целостность канала) → OBS-03 (ложный успех нотификаций) → OBS-02 (слепой webhook) → OBS-04 (traceback'и). OBS-05–OBS-08 — один PR по неймингу и терминальным событиям grab.
