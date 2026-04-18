# Сводный отчёт аудита TG_arr — 2026-04-18

Объём проекта: ~8300 строк Python, Python 3.12, aiogram 3.26, httpx 0.28, aiosqlite 0.22, pydantic 2.12.

## Статистика по категориям

| Категория | Критические | Высокие | Средние | Низкие | Всего |
|---|---:|---:|---:|---:|---:|
| Security (SEC) | 0 | 8 | 6 | 4 | 18 |
| Bugs (BUG) | 5 | 13 | 10 | 8 | 36 |
| Dead code (DEAD) | 3 | 6 | 5 | 4 | 18 |
| Dependencies (DEP) | 1 | 4 | 4 | 4 | 13 |
| Logic (LOGIC) | 5 | 9 | 9 | 5 | 28 |
| **ИТОГО** | **14** | **40** | **34** | **25** | **113** |

## Топ-10 самых важных проблем

### 1. BUG-01: NameError в exception handler (search.py:245)
`log` инициализируется внутри try, используется в except — возможен NameError, скрывающий реальную ошибку. **Фикс: 1 строка переноса.**

### 2. BUG-06: retry-декоратор не работает у Emby и qBittorrent
`try/except httpx.TimeoutException → raise EmbyError` внутри `@retry`-обёрнутого метода перехватывает TimeoutException до того, как tenacity его увидит. Retry фактически disabled.

### 3. BUG-11: callback.answer() дважды при рекурсивном вызове handler'ов
`handle_pause_torrent` → `handle_torrent_details(callback)` → снова `callback.answer()` — TelegramBadRequest «query too old». UI регулярно ломается на торрентах.

### 4. LOGIC-01: Radarr/Sonarr клиенты — 500 строк дублирования
Близнецовые клиенты с идентичной структурой. Любой фикс надо делать в 2 местах. Базовый класс напрашивается.

### 5. SEC-04: API-ключи индексеров в логах
`logger.info("Attempting push_release", download_url=release.download_url[:100])` — первые 100 символов URL с query-string, содержащей `apikey=` приватного трекера, уходят в структурный лог.

### 6. SEC-01: SSRF-проверка с TOCTOU/DNS-rebinding
`_validate_download_url` проверяет DNS однократно через `socket.gethostbyname`, qBittorrent потом резолвит повторно — уязвимо к DNS-rebinding на внутреннюю сеть (Radarr/Sonarr/Emby в той же LAN).

### 7. DEP-01: pyproject.toml vs requirements.txt — разные стратегии
pyproject: `>=3.13.1`, requirements: `==3.26.0`. Разные пути установки → разные версии → невоспроизводимые ошибки.

### 8. DEAD-01/02: ~70 KB устаревших отчётов в репозитории
BUGFIX_REPORT.md, IMPROVEMENTS.md, docs/FEATURE_QBITTORRENT.md, docs/QUALITY_REPORT.md — это дизайн-доки и исторические отчёты. Отвлекают, путают.

### 9. BUG-04: 500/502 не retryable
Ретраятся только 429/503/504. Transient 502 (Bad Gateway) от reverse-proxy не ретраится, пользователь получает ошибку.

### 10. LOGIC-08: Magic numbers разбросаны
MAX_QUERY_LENGTH, MAX_CACHE_SIZE, TORRENTS_PER_PAGE, MAX_REQUESTS_PER_MINUTE, MAX_MSG_LEN — в разных модулях как локальные константы. Часть должна быть в Settings для operator'а.

## Рекомендуемый порядок исправлений

**Фаза 1 (quick wins — 1-2 часа):**
- BUG-01 (перенести log.bind)
- BUG-17 (заменить .replace на .removeprefix)
- SEC-04 (маскировать query-string в логах)
- DEAD-01/02/03 (удалить старые отчёты и артефакты)
- DEAD-25 (удалить `version: "3.9"` из compose.override)
- DEP-04 (удалить неиспользуемый orjson)

**Фаза 2 (средние изменения — 1 день):**
- BUG-06 (рефакторинг retry в emby/qbittorrent)
- BUG-11 (устранить рекурсивные callback.answer)
- BUG-04 (расширить retryable codes до 500/502)
- SEC-01 (использовать getaddrinfo + retry-проверку)
- SEC-13 (порядок middleware Auth → RateLimit)
- DEP-01 (согласовать pyproject и requirements)

**Фаза 3 (рефакторинг — неделя):**
- LOGIC-01 (базовый ArrClient)
- LOGIC-02/03 (разбить search.py, formatters.py, keyboards.py)
- LOGIC-04/05 (единая логика grab_release)
- LOGIC-11 (вынести бизнес-логику в сервисы)

## Общая оценка здоровья проекта

**Сильные стороны:**
- Корректная структурная организация (clients/handlers/services/ui/models)
- Полное покрытие тестами клиентов/сервисов
- Правильное использование asyncio + aiogram 3.x
- HTML-escape в форматтерах через централизованный `_e()`
- Типизация через pydantic моделями
- Retry-логика с tenacity
- Singleton-registry для клиентов

**Слабые стороны:**
- Два god-file (formatters 890 строк, keyboards 803, search 726)
- Radarr/Sonarr — серьёзное дублирование (~500 строк)
- Retry-декораторы у 2-х из 3-х клиентов сломаны (BUG-06)
- Несогласованность между dev-deps и prod-deps
- Устаревшая документация в корне и docs/
- Нет lock-файла для воспроизводимых сборок

**Общая оценка: 6.5/10** — рабочий продукт в эксплуатации, но требует фазы технической санитарии перед добавлением новых фич. Критических проблем безопасности нет (whitelist-модель защищает), но тех. долг накапливается быстрее, чем исправляется.
