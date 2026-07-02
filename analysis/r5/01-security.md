# Анализ безопасности TG_arr (раунд 5)

## Критические

Не обнаружено. С учётом прод-контекста (webhook выключен, единственный админ, секреты не попадают в образ и в git) критичных дыр в текущем коде нет.

## Средние

### SEC-01: SSRF-валидатор доверяет всему хосту целиком, игнорируя порт
- **Файл**: `bot/services/add_service.py:88-111` (`_trusted_service_hosts`) и `bot/services/add_service.py:134-135` (проверка доверия в `_validate_download_url`)
- **Проблема**: после фикса 7b3de49 доверие строится по `parsed.hostname.lower() in _trusted_service_hosts()` — сравнивается ТОЛЬКО имя хоста, порт отбрасывается. В типичном self-hosted стеке Prowlarr/Radarr/Sonarr/qBit/Emby живут на одном LAN-IP (например `192.168.1.10:9696`, `:8080`, `:8096`). Из-за этого доверенным становится ЛЮБОЙ порт на этом IP. Вредоносный индексер, вернувший `downloadUrl=http://192.168.1.10:6379/…` или `:22`, пройдёт валидатор и будет передан в `radarr/sonarr/lidarr.push_release` или в `qbittorrent.add_torrent_url` (см. `add_service.py:393,434,547,588,750,787`). «Доверять своим сервисам» расширилось до «доверять всем сервисам на тех же хостах».
- **Риск**: Средний (нужен подконтрольный/скомпрометированный индексер в Prowlarr; но обход SSRF-защиты реальный)
- **Решение**: сравнивать пару (host, port). Собирать в `_trusted_service_hosts()` множество `(hostname, port)` из сконфигурированных URL (с учётом дефолтных портов схемы) и в валидаторе сверять `parsed.hostname` + `parsed.port`. Для не-совпавших портов — обычная IP-проверка `_is_internal_ip`.
- **Статус**: [ ] Не исправлено

### SEC-02: webhook-сервер без аутентификации и с bind 0.0.0.0 (при включении)
- **Файл**: `bot/webhook.py:77-96` (`build_webhook_app`/`handle`), `bot/config.py:98-100` (`webhook_bind` default `0.0.0.0`), `bot/main.py:315-329`
- **Проблема**: `handle()` принимает любой POST на `/webhook` (и `/webhook/{service}`) без токена/секрета/allowlist источника и шлёт Telegram-уведомление ВСЕМ allowed+admin (`main.py:318-323`). Сегмент `{service}` в пути игнорируется. Bind по умолчанию `0.0.0.0` — сервис доступен со всей LAN. Вектор спама/фишинга в личку (текст title/artist контролируется отправителем; HTML экранируется, XSS нет — только произвольный текст) и DoS-флуд через send_message ко всем пользователям.
- **Риск**: Средний (в проде выключено, но код небезопасен by-default при включении)
- **Решение**: требовать секрет (заголовок или секретный сегмент пути `/webhook/<token>`), сверять с конфигом; по умолчанию биндить `127.0.0.1`; документировать. Опционально ограничить частоту.
- **Статус**: [ ] Не исправлено

### SEC-03: неполное маскирование download-URL — passkey утекает в prod-логи (INFO)
- **Файл**: `bot/services/add_service.py:37` (`_SENSITIVE_QUERY_PARAMS`) и `bot/services/add_service.py:40-57` (`_mask_url`); вызовы на `add_service.py:400,554,757`
- **Проблема**: `_mask_url` маскирует только query-параметры из списка (`apikey/token/passkey/auth/authkey`), но:
  1. Prowlarr-прокси формирует URL вида `…/download?apikey=<prowlarr_key>&link=<encoded>&file=…` — `apikey` замаскируется, а `link` (закодированный оригинальный download-URL приватного трекера, часто с passkey) НЕ входит в список и логируется как есть.
  2. Пасскей в сегменте ПУТИ (частый формат: `https://tracker/download/<id>/<PASSKEY>/name.torrent`) не трогается вообще — `_mask_url` возвращает `scheme://netloc/path` без редактирования пути.
  Логи push_release идут на уровне INFO — секрет реально попадает в `docker logs`.
- **Риск**: Средний (утечка reusable-креденшелов трекера в персистентные логи)
- **Решение**: добавить `link`, `file`, `r`, `rss` в чувствительные ключи ИЛИ редактировать значения всех длинных query-параметров; для путей — маскировать хвост. Проще всего: логировать только `scheme://netloc` + маркер.
- **Статус**: [ ] Не исправлено

## Низкие

### SEC-04: `_format_health` рендерит версию сервисов и путь папок в HTML без экранирования
- **Файл**: `bot/handlers/status.py:46-54` (`ver = f" <code>{s.version}</code>"`, `f"{icon} {s.service}{ver}"`, `f"  <code>{path}</code>: {free_str}"`)
- **Проблема**: `/health` шлётся с `parse_mode="HTML"`, но `s.version` (из ответа *arr `system/status`) и `path` вставляются без `html.escape`. Для сравнения — `Formatters.format_system_status` (`ui/formatters.py:301,305`) те же поля экранирует через `_e()`.
- **Риск**: Низкий (источник — доверенные *arr)
- **Решение**: обернуть `s.version`, `s.service`, `path` в `html.escape`.
- **Статус**: [ ] Не исправлено

### SEC-05: маскирование секретов в логах узкое (только Telegram-токен, только top-level строки)
- **Файл**: `bot/main.py:29-37` (`_TOKEN_PATTERN` + `_mask_tokens`)
- **Проблема**: structlog-процессор маскирует только паттерн `bot\d+:…` и только у top-level строковых значений event_dict; не рекурсирует в вложенные dict/list и не знает про TMDB_PROXY_URL (пароль в URL), *arr API-ключи, qBit-пароль. Сейчас эти секреты по проверенным путям в логи не попадают — это defense-in-depth. Но при появлении `httpx.ProxyError` с URL прокси в тексте они не будут вычищены.
- **Риск**: Низкий
- **Решение**: рекурсивный обход значений; паттерн для `//user:pass@`-креденшелов.
- **Статус**: [ ] Не исправлено

### SEC-06: не-деструктивные операции qBittorrent доступны всем allowed-юзерам
- **Файл**: `bot/handlers/downloads.py` — `handle_pause_all`/`handle_resume_all` (`:544,561`), `handle_speed_set` (`:695`), `handle_recheck` (`:481`), `handle_priority` (`:507`), команды `/pause` `/resume` (`:113,142`) не проверяют `is_admin`; `t_delete`/`t_delf` проверяют (`:416-417,450-451`)
- **Проблема**: пауза/резюм всех, смена глобальных лимитов скорости, recheck и приоритеты доступны runtime-юзерам из `/adduser`.
- **Риск**: Низкий (в текущем проде единственный админ)
- **Решение**: закрыть изменяющие qBit-операции `is_admin`, либо задокументировать «/adduser = полный доступ к загрузкам».
- **Статус**: [ ] Не исправлено

### SEC-07: Docker без дополнительного hardening
- **Файл**: `docker-compose.yml:1-64`, `Dockerfile`
- **Проблема**: non-root и лимиты есть, но нет `security_opt: [no-new-privileges:true]`, `cap_drop: [ALL]`, `read_only: true` + `tmpfs: /tmp`. (= DEPLOY-05.)
- **Риск**: Низкий
- **Решение**: добавить в compose; liveness-файл `/tmp/tgarr-alive` переживёт tmpfs.
- **Статус**: [ ] Не исправлено

### SEC-08: остаточный риск DNS-rebinding (TOCTOU) для внешних хостов
- **Файл**: `bot/services/add_service.py:142-155`
- **Проблема**: валидатор резолвит DNS и проверяет A/AAAA-записи, но фактическую загрузку делают *arr/qBittorrent отдельным резолвом позже. Между валидацией и загрузкой DNS может «перевернуться». Архитектурное ограничение подхода «проверить-URL-заранее».
- **Риск**: Низкий (нужен контроль над DNS индексера + гонка)
- **Решение**: задокументировать как принятый риск.
- **Статус**: [ ] Не исправлено

## Проверено — проблем нет

- **SQL-инъекции**: все запросы в `bot/db.py` параметризованы. Единственная f-string — `PRAGMA user_version = {v}` с принудительным `int` — безопасно.
- **HTML-инъекция в основных путях**: `title`/`overview`/`indexer`/`query`/`name`/`save_path`/`category`/`tags` экранируются через `html.escape` (formatters, search.py, music.py, trending.py, webhook.py). Единственное исключение — SEC-04.
- **Обход авторизации через типы апдейтов**: polling запрашивает только `allowed_updates=["message","callback_query"]` (`main.py:336-340`); middleware — deny по умолчанию. inline_query/edited_message/channel_post не доставляются. ADMIN vs ALLOWED разграничены корректно.
- **Инъекции в qBittorrent**: хэши из объектов qBit, категории захардкожены, download-URL через SSRF-валидатор.
- **API-ключи *arr в логах**: в заголовке `X-Api-Key`, не в URL; `response_body` ошибок не логируется; `_safe_push_result` вырезает `downloadUrl`; qBit-пароль в form-body.
- **Секреты в образе / git**: `.env` исключён из образа и git; Dockerfile копирует лишь `bot/`; base-image по digest.
- **Path traversal**: пользовательский ввод в файловые пути не попадает.
- **magnet-валидация**: префикс `magnet:?xt=urn:btih:` + белый список схем `{http,https,magnet}`.
- **Race conditions прошлых раундов**: `_write_lock`, per-user grab-guard, UPDATE-only сессии — на месте.
