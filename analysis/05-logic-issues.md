# Анализ logic-issues TG_arr

## Критические

### LOGIC-01: Radarr и Sonarr клиенты — ~92% дублированный код
- **Файл**: `bot/clients/radarr.py` (325 строк), `bot/clients/sonarr.py` (380 строк)
- **Проблема**: Методы `lookup_movie`/`lookup_series`, `get_quality_profiles`, `get_root_folders`, `grab_release`, `push_release`, `get_calendar`, `check_connection` имеют идентичную структуру. Отличаются только endpoint и парсинг. Суммарно ~500 строк дублирования. Это типичная «Radarr/Sonarr — близнецы» проблема.
- **Риск**: Высокий — любое исправление (таймаут, логирование, retry) надо делать в 2-х местах.
- **Решение**: Вынести общую часть в `bot/clients/arr_base.py` (наследник BaseAPIClient), Radarr/Sonarr становятся тонкими подклассами с разными парсерами.
- **Статус**: [ ] Не исправлено

### LOGIC-02: `handlers/search.py` 726 строк — god-file, смешивает handlers + бизнес-логику + форматирование
- **Файл**: `bot/handlers/search.py`
- **Проблема**: Файл содержит 13+ handlers, функции `_execute_grab`, `grab_release`, `_resolve_folder`, `process_search`, `get_services`. Одна функция `_execute_grab` — 110 строк с дублированным кодом для movie/series. Нарушение SRP.
- **Риск**: Средний (maintenance).
- **Решение**: Разнести по файлам `handlers/search_commands.py`, `handlers/search_callbacks.py`, `handlers/search_grab.py`.
- **Статус**: [ ] Не исправлено

### LOGIC-03: `ui/keyboards.py` 803 строки + `ui/formatters.py` 890 строк — god-files
- **Файл**: `bot/ui/formatters.py`, `bot/ui/keyboards.py`
- **Проблема**: Все форматеры/клавиатуры в одном классе `Formatters` / `Keyboards` как static-методы. При добавлении новой фичи файлы разрастаются линейно.
- **Решение**: Разбить по доменам: `formatters/search.py`, `formatters/torrent.py`, `formatters/emby.py`, `formatters/calendar.py`. То же для keyboards.
- **Статус**: [ ] Не исправлено

### LOGIC-04: Дублирование логики «получить профиль + папку + упасть» во всех handler'ах
- **Файл**: `bot/handlers/search.py:540-586`, `bot/handlers/trending.py:322-345,406-419`
- **Проблема**: Один и тот же блок «get_radarr_profiles + get_radarr_root_folders + fallback на первый» продублирован 4+ раз. Функция `_resolve_folder` есть, но логика resolve профиля не выделена.
- **Решение**: Вынести в `AddService.get_effective_settings(service_name, prefs) -> (profile_id, folder_path)`.
- **Статус**: [ ] Не исправлено

### LOGIC-05: `add_service.grab_movie_release` и `grab_series_release` — код почти идентичен (110+110 строк)
- **Файл**: `bot/services/add_service.py:208-348, 350-498`
- **Проблема**: Различия: у series есть tvdb_id check и season/episode search. Всё остальное — копия.
- **Решение**: Выделить `_grab_release(content, release, ..., service='radarr'|'sonarr')`.
- **Статус**: [ ] Не исправлено

## Высокие

### LOGIC-06: Две версии API `/api/v1/` vs `/api/v3/` — несогласованность
- **Файл**: `bot/clients/base.py:252` (`/api/v1/system/status`), `bot/clients/radarr.py:317` (`/api/v3/system/status`)
- **Проблема**: Radarr/Sonarr поддерживают v1 (legacy) и v3 (current). Base использует v1, подклассы переопределяют. Prowlarr — только v1. Tmdb — `/trending/...` без версии (v3 API).
- **Решение**: Унифицировать: сделать `api_prefix` атрибутом класса.
- **Статус**: [ ] Не исправлено

### LOGIC-07: Side-effects на import: `get_settings()` вызывается в `BaseAPIClient.__init__`
- **Файл**: `bot/clients/base.py:65` (`self._settings = get_settings()`)
- **Проблема**: При импорте клиента (напр., в тестах до monkeypatch'а env) `get_settings()` может упасть на отсутствующих TELEGRAM_BOT_TOKEN. `@lru_cache` усугубляет: кеш заполняется первым вызовом.
- **Решение**: Lazy-инициализация settings в первом `_get_client()`.
- **Статус**: [ ] Не исправлено

### LOGIC-08: Magic numbers без констант
- **Файлы**: различные
- **Проблема**: 
  - `bot/handlers/search.py:125`: `MAX_QUERY_LENGTH = 200` — local const, не в settings
  - `bot/handlers/calendar.py:24`: `_MAX_USER_PERIOD_ENTRIES = 100`
  - `bot/handlers/trending.py:31`: `_MAX_CACHE_SIZE = 200`
  - `bot/handlers/downloads.py:19`: `TORRENTS_PER_PAGE = 5`
  - `bot/middleware/auth.py:15`: `MAX_REQUESTS_PER_MINUTE = 30`
  - `bot/ui/formatters.py:844`: `MAX_MSG_LEN = 3800`
  - `bot/services/notification_service.py:111`: `await asyncio.sleep(10)` — retry delay
- **Решение**: Собрать в `bot/constants.py` либо в `Settings`.
- **Статус**: [ ] Не исправлено

### LOGIC-09: Несогласованность возвращаемых типов: `get_calendar` возвращает `list[dict]`, а `lookup_movie` — `list[MovieInfo]`
- **Файл**: `bot/clients/radarr.py:187-232` vs `bot/clients/radarr.py:21-50`
- **Проблема**: Часть клиента отдаёт pydantic-модели, часть — словари. Форматтер `format_calendar` должен уметь оба.
- **Решение**: Ввести `CalendarMovie`/`CalendarEpisode` pydantic-модели.
- **Статус**: [ ] Не исправлено

### LOGIC-10: Дублированные API-запросы на один callback
- **Файл**: `bot/handlers/downloads.py:310-339` (`handle_torrent_details` вызывает `get_torrents()` заново после pause/resume → **один паузинг = 2 полных get_torrents**)
- **Проблема**: Каждый pause/resume/delete → refresh списка → повторный полный fetch всех торрентов. При 500+ торрентах может тормозить.
- **Решение**: Кешировать результат `get_torrents()` на 5-10 секунд в памяти.
- **Статус**: [ ] Не исправлено

### LOGIC-11: Handler делает всё: и парсинг, и API-запрос, и форматирование, и DB
- **Файл**: `bot/handlers/search.py:116-246` (`process_search`)
- **Проблема**: 130-строчная функция выполняет: проверку query, парсинг, определение типа, поиск, сохранение в БД, форматирование, отправку, логирование action. Нарушение SRP.
- **Решение**: Выделить `SearchFlowService.execute_search(query, content_type, user) -> SearchResult` + handler только обрабатывает UI.
- **Статус**: [ ] Не исправлено

### LOGIC-12: `Keyboards.torrent_list` не принимает `current_filter` через callback — всегда `TorrentFilter.ALL`
- **Файл**: `bot/handlers/downloads.py:72` (передаётся `TorrentFilter.ALL`)
- **Проблема**: При применении фильтра и потом нажатии refresh — фильтр сбрасывается.
- **Статус**: [ ] Средне

### LOGIC-13: `scoring.py` — все веса захардкожены в dataclass, нет настройки через env
- **Файл**: `bot/services/scoring.py:10-91`
- **Проблема**: `ScoringWeights` — dataclass с дефолтами. Менять веса можно только пересборкой. В TG-боте нет команды `/scoring_tune`.
- **Решение**: Добавить в Settings override для ключевых весов.
- **Статус**: [ ] Низко

### LOGIC-14: `SearchResult.calculated_score: int = 0` default — если scoring не выполнен, сортировка сбита
- **Файл**: `bot/models.py:75`
- **Проблема**: Если забыть вызвать `scoring.sort_results`, результаты отобразятся в произвольном порядке.
- **Решение**: Сделать обязательным, либо default None с явной проверкой.
- **Статус**: [ ] Низко

## Средние

### LOGIC-15: Дублированный код парсинга quality в `ProwlarrClient._parse_quality` — ветки if/elif, сложно тестировать отдельные аспекты
- **Файл**: `bot/clients/prowlarr.py:195-300`
- **Проблема**: 100-строчный метод с десятками if/elif. Парсинг резолюции, источника, кодека, HDR, аудио, субтитров — всё вместе.
- **Решение**: Разделить на `_parse_resolution`, `_parse_source`, `_parse_codec` и т.д. (каждый ≤15 строк).
- **Статус**: [ ] Не исправлено

### LOGIC-16: `EmbyClient` не наследует `BaseAPIClient` — дублирует retry, get_client, error handling
- **Файл**: `bot/clients/emby.py:54-133`
- **Проблема**: Авторизация через X-Emby-Token вместо X-Api-Key, но всё остальное идентично. Mini-дубликат BaseAPIClient.
- **Решение**: Наследовать, переопределив `_get_headers`.
- **Статус**: [ ] Не исправлено

### LOGIC-17: `QBittorrentClient` — тоже не наследует BaseAPIClient. Это оправдано (нужна cookie-auth), но всё равно дубликат retry-логики.
- **Файл**: `bot/clients/qbittorrent.py:61-200`
- **Статус**: [ ] Низко

### LOGIC-18: `add_movie` в `AddService` — если existing найден, возвращает existing, но action.success=True и action.content_id = old.tmdb_id вместо actual
- **Файл**: `bot/services/add_service.py:123-128`
- **Проблема**: При попытке добавить фильм, который уже есть, пользователь не узнает об этом явно (возвращается success=True). UX.
- **Решение**: Добавить статус "already_exists".
- **Статус**: [ ] Не исправлено

### LOGIC-19: `handle_type_selection` вызывает `process_search(callback.message, ...)` — callback.message от бота, а не от пользователя
- **Файл**: `bot/handlers/search.py:271-277`
- **Проблема**: См. BUG-18. Логика «повторный вызов» должна быть отдельной функцией `_perform_search`.
- **Статус**: [ ] Не исправлено

### LOGIC-20: `_should_monitor_season` не различает seasonNumber=0 (specials)
- **Файл**: `bot/clients/sonarr.py:147-165`
- **Проблема**: Sonarr выделяет seasonNumber=0 как specials. Метод по умолчанию мониторит и их (для type="all").
- **Решение**: Для seasonNumber=0 возвращать False (или брать из prefs).
- **Статус**: [ ] Не исправлено

### LOGIC-21: `NotificationService._tracked_torrents` — растёт линейно
- **Файл**: `bot/services/notification_service.py:38,158-164`
- **Проблема**: Добавляются новые хеши, удаляются только пропавшие из qBit. Если пользователь держит 10000 торрентов, dict будет расти.
- **Решение**: LRU с max-size.
- **Статус**: [ ] Не исправлено

### LOGIC-22: `handle_series_from_trending` — нет fallback при пустом кеше
- **Файл**: `bot/handlers/trending.py:239-247`
- **Проблема**: См. BUG-28.
- **Статус**: [ ] Не исправлено

### LOGIC-23: Нет общей абстракции для `ArrCalendar` (Radarr movies и Sonarr episodes)
- **Файл**: `bot/handlers/calendar.py:30-63`
- **Проблема**: Handler вручную собирает списки episodes + movies + формирует строку. Формирование в Formatter, но fetch в handler.
- **Решение**: `CalendarService.get_upcoming(days)` возвращает объединённую структуру.
- **Статус**: [ ] Не исправлено

## Низкие

### LOGIC-24: `Formatters._get_rating` — пытается разные источники, но `ratings` от TMDb хранится как `{"tmdb": 7.5}` (число), а от Radarr как `{"tmdb": {"value": 7.5}}`
- **Файл**: `bot/ui/formatters.py:654-666`
- **Проблема**: Метод обрабатывает оба случая (isinstance dict/float). OK, но это признак смешанной модели.
- **Статус**: [ ] Низко (см. LOGIC-09)

### LOGIC-25: `format_bytes` и `format_speed` в `models.py` — не место для UI-утилит
- **Файл**: `bot/models.py:411-432`
- **Проблема**: Formatting functions среди моделей. Вызываются и из моделей (через property), и из formatters. Логично — избегаем circular import, но архитектурно странно.
- **Статус**: [ ] Низко

### LOGIC-26: `AuthMiddleware` создаёт пользователя при первом сообщении — но все allowed-пользователи могли бы быть созданы при старте
- **Файл**: `bot/middleware/auth.py:68-89`
- **Проблема**: Lazy create: если allowed_tg_ids=[1,2,3], но они не писали боту — их в БД нет. `notification_service.subscribe_user(user_id)` в main.py подписывает всех allowed, независимо от наличия в БД. Логически OK, но user в БД отсутствует.
- **Статус**: [ ] Низко

### LOGIC-27: `settings.is_admin(user_id)` вызывается в AuthMiddleware дважды (строки 74, 93)
- **Файл**: `bot/middleware/auth.py:74,93`
- **Проблема**: Двойной поиск в списке. Мелкая оптимизация.
- **Статус**: [ ] Минорно

### LOGIC-28: `_trending_movies_cache: dict[int, Any]` — слабо типизировано (Any вместо MovieInfo)
- **Файл**: `bot/handlers/trending.py:27-28`
- **Решение**: `dict[int, MovieInfo]`.
- **Статус**: [ ] Низко

## Итоговый подсчёт
- Критические: 5 (LOGIC-01..05 — фундаментальные дублирования и god-files)
- Высокие: 9 (LOGIC-06..14)
- Средние: 9 (LOGIC-15..23)
- Низкие: 5 (LOGIC-24..28)
