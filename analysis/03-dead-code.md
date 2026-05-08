# Dead code TG_arr v1.0 (раунд 3)

Дата: 2026-05-08. Корень: `f:/VScode/TG_arr/`. Обзор после round 2 (analysis_round2/03-dead-code.md). Многие R2-находки актуальны, появились новые.

---

## Очевидно мёртвое

### DEAD-01: `bot/constants.py` — модуль не импортируется (HIGH, новый)
- **Файл**: `bot/constants.py` (целиком, 22 строки)
- **Проблема**: 10 констант — `MAX_MESSAGE_LENGTH`, `SAFE_MESSAGE_LENGTH`, `TORRENTS_PER_PAGE`, `SEARCH_RESULTS_MAX`, `MAX_QUERY_LENGTH`, `PROWLARR_SEARCH_LIMIT`, `TRENDING_LIMIT`, `QBT_ETA_INFINITY`, `SESSION_TTL_HOURS`, `SEARCH_HISTORY_DAYS`. Ни одна не импортируется (`from bot.constants` / `import constants` → 0 совпадений в `bot/`, `tests/`).
- **Подтверждение**:
  - `Grep "from bot.constants|import constants"` → пусто
  - `MAX_QUERY_LENGTH = 200` объявляется ЛОКАЛЬНО в `bot/handlers/music.py:99` и `bot/handlers/search.py:132` (дубль).
  - `TORRENTS_PER_PAGE = 5` объявляется ЛОКАЛЬНО в `bot/handlers/downloads.py:19` (дубль).
  - `SAFE_MESSAGE_LENGTH = 3800` хардкодится в `bot/ui/formatters.py:619, 983` (дубль).
  - `MAX_MESSAGE_LENGTH`, `SEARCH_RESULTS_MAX`, `PROWLARR_SEARCH_LIMIT`, `TRENDING_LIMIT`, `QBT_ETA_INFINITY` (8640000), `SESSION_TTL_HOURS` (24), `SEARCH_HISTORY_DAYS` (7) — ни разу не используются (но 8640000 есть inline в `bot/models.py:385` и 24/7 в `bot/main.py:104,109` — литералы вместо констант).
- **Решение**: либо удалить файл и принять статус-кво (литералы inline), либо мигрировать на импорт из `bot/constants.py` все дубли. Рекомендую второе — это и DEAD-01 закроет, и DUP-01 (см. ниже).
- **Статус**: [ ]

### DEAD-02: `ProwlarrClient.grab_release` не вызывается (LOW, новый)
- **Файл**: `bot/clients/prowlarr.py:375-385`
- **Проблема**: Метод реализован, но `prowlarr.grab_release` не вызывается нигде — production-код всегда идёт через `radarr/sonarr/lidarr.grab_release` или `push_release`. `Grep "prowlarr\.grab_release|self\.prowlarr\.grab"` → 0.
- **Подтверждение**: `Grep` показывает только определение в `prowlarr.py:375` и упоминание в analysis_round2.
- **Решение**: удалить метод. (8 строк)
- **Статус**: [ ]

### DEAD-03: `Keyboards.confirm_delete_torrent` — только в тестах (LOW, повтор R2 DEAD-08)
- **Файл**: `bot/ui/keyboards.py:644-663`
- **Проблема**: keyboard для confirm-flow удаления, но в `bot/handlers/downloads.py` `handle_delete_torrent`/`handle_delete_with_files` (строки 388, 422) делают delete сразу без подтверждения. Метод вызывается только из `tests/test_qbittorrent.py:476-485`.
- **Подтверждение**: `Grep "confirm_delete_torrent"` → keyboards.py + tests + analysis_round2.
- **Решение**: либо удалить keyboard и тесты, либо реализовать confirm-flow для `t_delf:` (опасный «удалить с файлами»). Рекомендую второе — продолжает быть UX-багом.
- **Статус**: [ ]

### DEAD-04: `Keyboards.series_list` отсутствует — было в R2-DEAD-04/31 (CLOSED)
- **Файл**: `bot/ui/keyboards.py`
- **Проблема**: В round-2 отчёте отмечен метод `series_list` со ссылкой на несуществующий `CallbackData.SERIES`. На текущем коде `Keyboards.series_list` уже отсутствует (видимо, удалён в round 2).
- **Подтверждение**: `Grep "series_list"` в keyboards.py → нет.
- **Решение**: подтверждение, что чистка состоялась.
- **Статус**: [x]

### DEAD-05: `Formatters.format_torrent_compact` — только в тестах (LOW, повтор R2 DEAD-29)
- **Файл**: `bot/ui/formatters.py:603-609`
- **Проблема**: реализован, но в production-handler не вызывается.
- **Подтверждение**: `Grep "format_torrent_compact"` → formatters.py + tests/test_qbittorrent.py + analysis_round2.
- **Решение**: удалить функцию и тест `test_format_torrent_compact`.
- **Статус**: [ ]

### DEAD-06: `Formatters.format_torrent_action` — только в тестах (LOW, новый)
- **Файл**: `bot/ui/formatters.py:697-717`
- **Проблема**: Возвращает форматированную строку для pause/resume/delete, но в `bot/handlers/downloads.py` все callback'и формируют alert-текст inline (например, `"⏸️ Приостановлен: {torrent.name[:30]}"` на строке 342). Используется только в `tests/test_qbittorrent.py:359-368`.
- **Подтверждение**: `Grep "format_torrent_action"` → formatters.py + tests + analysis_round2.
- **Решение**: удалить функцию и тест либо использовать в downloads.py.
- **Статус**: [ ]

### DEAD-07: `Formatters.format_no_torrents` не вызывается (LOW, новый)
- **Файл**: `bot/ui/formatters.py:666-682`
- **Проблема**: Реализован, но handler `cmd_downloads` показывает «📭 Торренты не найдены.» inline (строка 62), а пустые-фильтр-результаты вообще не формируют сообщение через эту функцию.
- **Подтверждение**: `Grep "format_no_torrents"` → formatters.py + tests/test_qbittorrent.py.
- **Решение**: удалить функцию (или использовать в `handle_filter_select`, где сейчас `format_torrent_list` вызывается даже на пустом списке).
- **Статус**: [ ]

### DEAD-08: `Formatters.format_album_info` не вызывается (LOW, новый)
- **Файл**: `bot/ui/formatters.py:275-304`
- **Проблема**: Метод для отображения `AlbumInfo`, но `AlbumInfo` нигде в handler-flow не показывается — music-flow только artist (см. `handlers/music.py`).
- **Подтверждение**: `Grep "format_album_info"` → только formatters.py.
- **Решение**: удалить (30 строк) либо использовать при отображении календарных альбомов из Lidarr (сейчас `format_calendar` форматирует их inline на строках 974-980).
- **Статус**: [ ]

### DEAD-09: `ScoringService.filter_by_quality` не вызывается в production (MED, повтор R2 DEAD-07)
- **Файл**: `bot/services/scoring.py:290-329`
- **Проблема**: Реализован и протестирован (`tests/test_scoring.py`), но в production вызовов нет — `process_search` не фильтрует по `preferred_resolution`. Соответственно настройка «Предпочитаемое разрешение» (UI: `Keyboards.resolution_selection`, handler `handle_set_resolution`) **не работает**: значение сохраняется в `db_user.preferences.preferred_resolution`, но никогда не читается.
- **Подтверждение**: `Grep "filter_by_quality"` → scoring.py + tests/test_scoring.py + analysis_round2.
  `Grep "preferred_resolution"` (только чтение, не присвоение) → 0 в production.
- **Решение**: либо вызывать `scoring.filter_by_quality(results, preferred_resolution=prefs.preferred_resolution)` в `search_service.search_releases`, либо скрыть UI-опцию.
- **Статус**: [ ]

### DEAD-10: `NotificationService.force_check` / `unsubscribe_user` / `get_stats` не вызываются (LOW, повтор R2 DEAD-18)
- **Файл**: `bot/services/notification_service.py:200-236, 52-55, 238-248`
- **Проблема**: `force_check` — нет команды `/check`, никто не вызывает. `unsubscribe_user` — никто не вызывает (subscribe есть в `main.on_startup`, отписаться нельзя). `get_stats` — нет admin-команды.
- **Подтверждение**: `Grep "force_check"` → notification_service.py + tests + analysis_round2. `Grep "unsubscribe_user|get_stats"` → notification_service.py + tests/test_qbittorrent.py.
- **Решение**: либо добавить команды (`/check`, `/notif_stats`, `/notif_off`), либо удалить методы. Рекомендую первое — действительно полезные admin-инструменты.
- **Статус**: [ ]

### DEAD-11: `LidarrClient.search_album` не вызывается (LOW, новый)
- **Файл**: `bot/clients/lidarr.py:160-164`
- **Проблема**: `search_album(album_id)` реализован, но в `add_service.grab_music_release` (line 759) триггерится только `search_artist`, не `search_album`. AlbumInfo нигде не используется как selected_content.
- **Подтверждение**: `Grep "search_album"` → только lidarr.py.
- **Решение**: удалить (5 строк) или использовать когда добавится album-flow (см. DEAD-08, DEAD-13).
- **Статус**: [ ]

### DEAD-12: `LidarrClient.lookup_album` / `SearchService.lookup_album` (если есть) не вызываются (LOW, повтор R2 DEAD-11)
- **Файл**: `bot/clients/lidarr.py:42-61`
- **Проблема**: метод реализован, но handler-flow никогда альбомы не lookup'ит.
- **Подтверждение**: `Grep "lookup_album"` → только lidarr.py + analysis_round2. `bot/services/search_service.py` — нет `lookup_album`.
- **Решение**: удалить (~20 строк) или использовать в album-flow.
- **Статус**: [ ]

### DEAD-13: `LidarrClient.get_artist_by_mbid`, `RadarrClient.get_movie_by_tmdb`, `SonarrClient.get_series_by_tvdb` (LOW, повтор R2 DEAD-09/10)
- **Файлы**: `bot/clients/lidarr.py:63-68`, `bot/clients/radarr.py:63-70`, `bot/clients/sonarr.py:61-68`
- **Проблема**: Используются ТОЛЬКО в `bot/services/add_service.py` (existence-check перед add). В handler'ах не вызываются. Не dead, но узкий scope.
- **Подтверждение**: `Grep` подтверждает использование только в `add_service.py`.
- **Решение**: оставить как есть (используются add_service) — отзываю как кандидата на удаление. INFO.
- **Статус**: [x] (false positive)

### DEAD-14: `EmbyClient.install_update` / `restart_server` — admin без UI guard в коде самого клиента (INFO, повтор R2 DEAD-15)
- **Файлы**: `bot/clients/emby.py:192-203, 205-213, 215-218`
- **Проблема**: `install_update` и `restart_server` используются в `handlers/emby.py:227, 277` (admin-only через `is_admin` параметр). `get_sessions` тоже используется. `get_scheduled_tasks` — отсутствует в текущем коде (R2-замечание устарело).
- **Подтверждение**: `Grep "install_update|restart_server"` → emby.py + handlers/emby.py.
- **Решение**: не dead — работают. Отзываю.
- **Статус**: [x] (false positive)

### DEAD-15: `Keyboards.torrent_filters(current_filter)` — параметр не используется (LOW, новый)
- **Файл**: `bot/ui/keyboards.py:527`
- **Проблема**: Сигнатура принимает `current_filter: TorrentFilter = TorrentFilter.ALL`, в теле используется для маркировки активного фильтра (строка 545: `display_label = f"• {label}" if filter_type == current_filter else label`). Но в handler `handle_filter_menu` (downloads.py:567) keyboard вызывается БЕЗ аргумента (`Keyboards.torrent_filters()`), поэтому всегда подсвечивается ALL.
- **Подтверждение**: `Grep "torrent_filters\("` → keyboards.py:527 + downloads.py:575 + tests.
- **Решение**: либо убрать параметр (если markers не нужны), либо передавать текущий фильтр (требует знать его на момент клика «Фильтр»). Минор, не строго dead.
- **Статус**: [ ]

### DEAD-16: `Keyboards.torrent_list(current_filter)` — параметр не используется в теле (LOW, новый)
- **Файл**: `bot/ui/keyboards.py:425-494`
- **Проблема**: Сигнатура принимает `current_filter: TorrentFilter = TorrentFilter.ALL` (строка 429), но в теле параметр никогда не читается (нет ни одного `current_filter` после декларации). Параметр носят с собой все вызовы (downloads.py:72, 198, 239, 268, 616), но без эффекта.
- **Подтверждение**: чтение тела keyboards.py:425-494, ни одного `current_filter` после параметра.
- **Решение**: удалить параметр или использовать (например, метку выбранного фильтра в footer keyboard).
- **Статус**: [ ]

### DEAD-17: `Keyboards.speed_limits_menu(current_dl_limit, current_ul_limit)` — параметры не передаются (LOW, новый)
- **Файл**: `bot/ui/keyboards.py:566`
- **Проблема**: Реализованы маркеры `✓` для текущего лимита (строки 590, 601, 617, 627), но handler `handle_speed_menu` (downloads.py:629-666) вызывает `Keyboards.speed_limits_menu()` БЕЗ передачи текущих значений. Маркер всегда на `0` (Без лимита).
- **Подтверждение**: `Grep "speed_limits_menu"` → keyboards.py:566 + downloads.py:656 + tests.
- **Решение**: передавать `current_dl_limit=status.download_limit, current_ul_limit=status.upload_limit` (status уже получен в строке 640).
- **Статус**: [ ]

### DEAD-18: Локальное `MENU_HISTORY` в `bot/handlers/start.py:20` не используется (LOW, повтор R2 DEAD-19)
- **Файл**: `bot/handlers/start.py:20`
- **Проблема**: `start.py` декларирует константу `MENU_HISTORY = "📋 История"`, но handler для неё лежит в `handlers/history.py:16` (тоже свою константу определяет). В start.py `MENU_HISTORY` нигде не читается.
- **Подтверждение**: `start.py` — единичная декларация в строке 20, ни одного `F.text == MENU_HISTORY` или другого использования в этом файле.
- **Решение**: удалить из start.py (как и `MENU_DOWNLOADS`, `MENU_QSTATUS`, `MENU_STATUS`, `MENU_SETTINGS` — все из строк 16-20 объявлены, но не используются в start.py — handler'ы для них в других модулях).
- **Статус**: [ ]

### DEAD-19: `bot/handlers/start.py:15-19` MENU_* — мёртвые константы (LOW, повтор R2 DEAD-19)
- **Файл**: `bot/handlers/start.py:15-19`
- **Проблема**: `MENU_SEARCH`, `MENU_DOWNLOADS`, `MENU_QSTATUS`, `MENU_STATUS`, `MENU_SETTINGS` — все объявлены, но в `start.py` НЕ используются (handler-фильтры в других модулях, каждый со своей копией). Только `MENU_HISTORY` дублируется. См. также DEAD-18.
- **Подтверждение**: `Grep "MENU_(SEARCH|DOWNLOADS|QSTATUS|STATUS|SETTINGS|HISTORY)"` в start.py → только декларации.
- **Решение**: удалить блок 15-20 из start.py. Опционально: вынести все MENU_* в `bot/ui/menu.py` (single source of truth).
- **Статус**: [ ]

### DEAD-20: `SearchSession.monitor_type` поле не используется (LOW, новый)
- **Файл**: `bot/models.py:282`
- **Проблема**: Поле `monitor_type: Literal[...] = "all"` объявлено в модели, но в коде НИГДЕ не читается и не присваивается — `monitor_type` для add_series вычисляется заново в `bot/handlers/search.py:644-652` на каждый grab.
- **Подтверждение**: `Grep "session\.monitor_type|\.monitor_type\b"` (исключая объявление) → 0 совпадений.
- **Решение**: удалить поле либо использовать его (сохранять выбор пользователя в sessione).
- **Статус**: [ ]

### DEAD-21: `MovieInfo.fanart_url` / `SeriesInfo.fanart_url` / `ArtistInfo.fanart_url` — записываются, не читаются (LOW, новый)
- **Файлы**: `bot/models.py:113, 140, 165`
- **Проблема**: Все три модели имеют поле `fanart_url`. Парсеры (`radarr.py:284`, `sonarr.py:325`, `lidarr.py:258`, `tmdb.py:123,159`) его заполняют, но **ни одного чтения** в коде нет — formatters используют только `poster_url` (через `answer_photo`).
- **Подтверждение**: `Grep "\.fanart_url"` → 0 чтений (только присвоения в парсерах).
- **Решение**: либо удалить поля и логику парсинга (упрощение), либо использовать (например, отправлять fanart как album-art в музыкальном flow).
- **Статус**: [ ]

### DEAD-22: `TorrentInfo.tracker` не отображается (LOW, новый)
- **Файл**: `bot/models.py:375`
- **Проблема**: Поле tracker заполняется в `qbittorrent._parse_torrent` (строка 467), но `Formatters.format_torrent_details` его НЕ выводит (см. formatters.py:530-601).
- **Подтверждение**: `Grep "torrent\.tracker"` → 0 в production.
- **Решение**: удалить поле или добавить вывод в `format_torrent_details` (полезно для debug).
- **Статус**: [ ]

---

## Возможно мёртвое (требует проверки)

### DEAD-23: `BaseAPIClient._get_http_timeout` — единственный вызов из единственного места (LOW, повтор R2 DEAD-05)
- **Файл**: `bot/clients/base.py:69-72`
- **Проблема**: Метод вызывается ТОЛЬКО из `_get_client()` (строка 81). Можно встроить.
- **Подтверждение**: `Grep "_get_http_timeout"` → 2 совпадения (декларация + 1 вызов).
- **Решение**: minor refactor — встроить.
- **Статус**: [ ]

### DEAD-24: `Settings.notify_check_interval` — параметр конфига, но валидация диапазона избыточна (INFO)
- **Файл**: `bot/config.py:69`
- **Проблема**: `notify_check_interval: int = Field(default=60, ge=10, le=3600, ...)`. Используется в `notification_service.py:101, 247`. Не dead.
- **Подтверждение**: `Grep "notify_check_interval"` → config.py + notification_service.py + docker-compose.yml + .env.example.
- **Решение**: не dead. INFO.
- **Статус**: [x] (false positive)

### DEAD-25: `Formatters._safe_truncate` — один вызов из одного места (INFO)
- **Файл**: `bot/ui/formatters.py:619-648, 983`
- **Проблема**: Вызывается только в `format_calendar` (строка 983). Можно встроить или сделать общим helper'ом.
- **Подтверждение**: `Grep "_safe_truncate"` → 2 совпадения.
- **Решение**: оставить как есть — generic-helper, уместен. INFO.
- **Статус**: [x] (info only)

### DEAD-26: `ScoringWeights.bad_keywords` — конструктор хардкода списка (INFO)
- **Файл**: `bot/services/scoring.py:71-90`
- **Проблема**: `bad_keywords` — параметр dataclass с `default=None` и `__post_init__` дефолтит словарь. Если у `ScoringWeights` нигде не передаются custom values (а grep показывает 1 use в test_scoring.py), эта гибкость не используется. Не dead, но over-engineered.
- **Подтверждение**: `Grep "ScoringWeights"` → scoring.py + tests/test_scoring.py + analysis_round2 (only customization in test).
- **Решение**: оставить — тесты опираются на этот API. INFO.
- **Статус**: [x] (info only)

### DEAD-27: `bot/handlers/music.py:33` — импорт `_SCORING_SERVICE` из `search.py` (циркуляр-избегание) (INFO, повтор R2 DEAD-20)
- **Файл**: `bot/handlers/music.py:33`
- **Проблема**: PERF-04 — импорт сделан внутри модуля (после блока imports), чтобы избежать циркуляра. Workaround, не dead.
- **Решение**: рефактор — вынести `_SCORING_SERVICE` в отдельный `bot/services/_singletons.py`. Минор.
- **Статус**: [ ]

### DEAD-28: `bot/services/search_service.py: SearchService.parse_query` — `quality` извлекается, но не передаётся дальше (LOW, повтор R2 DEAD-21)
- **Файл**: `bot/services/search_service.py:248-253`
- **Проблема**: `parsed["quality"]` вычисляется, но в `process_search` (handlers/search.py:200) только `parsed["title"]` и `parsed["season"]` передаются дальше. После очистки title от quality-токена, поиск всё равно идёт без фильтра по `quality`.
- **Подтверждение**: `Grep 'parsed\["quality"\]|parsed\.get\("quality"\)'` → 0 чтений вне самой parse_query.
- **Решение**: либо использовать `parsed["quality"]` как `preferred_resolution` фильтр (см. DEAD-09), либо удалить block 247-253. Связан с DEAD-09.
- **Статус**: [ ]

### DEAD-29: `bot/services/search_service.py: SearchService.parse_query[year]` — извлекается, не используется (LOW, новый)
- **Файл**: `bot/services/search_service.py:223-230`
- **Проблема**: `parsed["year"]` извлекается из query, но в production-flow (`process_search`) не читается. Заметим что `parsed["episode"]` тоже извлекается (`s_match.group(2)`) и не читается.
- **Подтверждение**: `Grep 'parsed\["year"\]|parsed\.get\("year"\)|parsed\["episode"\]'` → 0.
- **Решение**: либо использовать (передавать в Prowlarr как фильтр), либо упростить parse_query до title+season.
- **Статус**: [ ]

### DEAD-30: `LidarrClient.push_release` — путь vs grab_release (INFO, повтор R2 DEAD-14)
- **Файл**: `bot/clients/lidarr.py:135-152`
- **Проблема**: `push_release` используется в `add_service.grab_music_release` (line 704). Не dead.
- **Решение**: false positive. INFO.
- **Статус**: [x] (false positive)

---

## Дубликаты

### DUP-01: `MAX_QUERY_LENGTH = 200` × 2 (LOW)
- **Файлы**: `bot/handlers/search.py:132`, `bot/handlers/music.py:99`
- **Проблема**: Литерал и одинаковая логика. В `bot/constants.py:12` уже есть `MAX_QUERY_LENGTH = 200` — но никто не импортирует.
- **Решение**: вынести в `bot/constants.py` (или импортировать из существующего, см. DEAD-01).
- **Статус**: [ ]

### DUP-02: `TORRENTS_PER_PAGE = 5` × 2 (LOW)
- **Файлы**: `bot/handlers/downloads.py:19`, `bot/constants.py:8`
- **Проблема**: Дубль с константой в `constants.py`. handlers/downloads.py объявляет свою копию.
- **Решение**: импортировать из `bot/constants.py`.
- **Статус**: [ ]

### DUP-03: `MENU_*` константы декларируются в каждом handler-модуле (LOW)
- **Файлы**: `bot/handlers/start.py:15-20`, `search.py:35`, `downloads.py:22-23`, `emby.py:18`, `history.py:16`, `settings.py:26`, `status.py:26`, `calendar.py:19`, `music.py:29`, `trending.py:22`
- **Проблема**: Каждый модуль свою копию соответствующего MENU-текста объявляет. Также в `search.py:39-43` есть `MENU_BUTTONS` set'ом, который должен быть единым source-of-truth для всех меню-кнопок и **не синхронизирован автоматически** при добавлении новых пунктов.
- **Решение**: вынести в `bot/ui/menu.py` (или `bot/constants.py`).
- **Статус**: [ ]

### DUP-04: `_safe_truncate` macroconst `SAFE_MESSAGE_LENGTH = 3800` × 3 (LOW)
- **Файлы**: `bot/constants.py:5`, `bot/ui/formatters.py:619 (default arg)`, `bot/ui/formatters.py:983 (max_len=3800)`
- **Проблема**: Одинаковые литералы.
- **Решение**: импортировать из constants.py.
- **Статус**: [ ]

### DUP-05: `_get_client()` / `_get_headers()` дублируются между `BaseAPIClient` (httpx с retry) и `EmbyClient`/`QBittorrentClient` (httpx без BaseAPIClient) (LOW)
- **Файлы**: `bot/clients/base.py:74-92`, `bot/clients/emby.py:63-85`, `bot/clients/qbittorrent.py:76-92`
- **Проблема**: Emby и qBittorrent наследуют не от `BaseAPIClient`, дублируют 90% логики (httpx-клиент, headers, login). Историческое — Emby/qBit имеют разные auth-механики (X-Emby-Token, cookie-session). Не dead, но архитектурный smell.
- **Решение**: оставить как есть (deferred refactor — было LOGIC-02 в R2).
- **Статус**: [ ] (deferred)

### DUP-06: `parse_*` regex-логика частично дублируется между `Prowlarr._parse_quality` и `SearchService.parse_query` (LOW)
- **Файлы**: `bot/clients/prowlarr.py:201-306` (полная парсинг качества из title), `bot/services/search_service.py:202-258` (упрощённая версия для query)
- **Проблема**: Год, season-episode, quality — оба места реализуют свою версию regex. Одно для индексер-title, другое для user-query, но логика 70% одинакова.
- **Решение**: общий `bot/services/title_parser.py`. Deferred (LOGIC).
- **Статус**: [ ] (deferred)

### DUP-07: `format_bytes` / `format_speed` лежат в `bot/models.py:475-496` (LOW, повтор R2 DEAD-06)
- **Файлы**: `bot/models.py:475-496`
- **Проблема**: Утилитарные функции в файле моделей. Архитектурно неудачное место.
- **Решение**: `bot/utils/formatters.py`. Минор.
- **Статус**: [ ]

---

## Build-артефакты, мусорные файлы

Проверены:
- `*.pyc`, `__pycache__/` — игнорируются (`.gitignore` строки 1-3, 47).
- `*.bak`, `.DS_Store`, `*_OLD.py` — нет (`Glob "**/*.bak"` → пусто, `**/*_OLD*` → пусто, `**/.DS_Store` → пусто).
- `*.log`, `logs/` — нет (`.gitignore`).
- `data/` — папка пустая (БД создаётся в runtime).
- `analysis_round2/` — старые отчёты (исключены по запросу).

**Итог**: build-артефактов нет. [x]

---

## Закомментированный код блоками

Сканирование показало:
- `bot/handlers/music.py:270-273` — комментарий-нота про BUG-27 (актуальная документация, не dead).
- `bot/handlers/search.py:506-513` — docstring CONFIRM_GRAB про BUG-27 (документация).
- `bot/handlers/calendar.py:23-25` — комментарий про PERF-03 (документация).
- В `.env.example` закомментированы опциональные ENV (`# LIDARR_URL=...`, `# DEEZER_ENABLED=true` и т.д.) — это нормально для шаблона.

**Итог**: закомментированных блоков кода нет. [x]

---

## TODO/FIXME которые висят больше года

Сканирование `Grep "TODO|FIXME|XXX|HACK"` по `bot/`, `tests/`:

```
Нет ни одного TODO/FIXME в коде (по grep).
```

Только refer'ы на BUG-* / SEC-* / DEPLOY-* / PERF-* — это документация ранее закрытых аудитом находок, не TODO.

**Итог**: stale TODO нет. [x]

---

## Дубли логики (одно и то же реализовано в двух местах)

См. DUP-01..DUP-07 выше.

Дополнительно:
### DUP-08: HTML-escape helper `_e` (formatters.py:29-33) дублирует `html.escape` (LOW)
- **Файл**: `bot/ui/formatters.py:29-33`
- **Проблема**: `_e(text)` оборачивает `html.escape(str(text))` с проверкой на falsy. Используется внутри formatters, но в handlers (`bot/handlers/music.py:127, 155, 219, 252, 338` и др.) напрямую вызывается `html.escape()` без обёртки. Не строго dead, но inconsistent.
- **Решение**: либо экспортировать `_e` (сейчас `_e` приватный) и использовать везде, либо удалить.
- **Статус**: [ ]

---

## Неиспользуемые конфиг-параметры в `bot/config.py`

Все поля `Settings` проверены через grep:

| Поле | Используется? |
|---|---|
| `telegram_bot_token` | да (main.py:183) |
| `allowed_tg_ids` | да (main.py:117, config.py:147) |
| `admin_tg_ids` | да |
| `prowlarr_url`, `prowlarr_api_key` | да (registry.py) |
| `radarr_*` | да |
| `sonarr_*` | да |
| `lidarr_url`, `lidarr_api_key`, `lidarr_enabled` | да |
| `deezer_enabled` | да |
| `qbittorrent_*`, `qbittorrent_enabled` | да |
| `emby_*`, `emby_enabled` | да |
| `tmdb_*`, `tmdb_enabled` | да |
| `notify_download_complete`, `notify_check_interval` | да |
| `timezone` | да (main.py:166, formatters.py:1001) |
| `log_level` | да |
| `database_path` | да |
| `auto_grab_score_threshold` | да |
| `http_timeout` | да |
| `results_per_page` | да |

**Итог**: все поля используются. [x]

---

## Неиспользуемые env-переменные в `.env.example` / `docker-compose.yml`

Сравнение `.env.example` ↔ `Settings`:

- ✅ Все required и documented в `.env.example` присутствуют в `Settings`.
- В `docker-compose.yml` есть переменные `QBITTORRENT_TIMEOUT`, `EMBY_TIMEOUT`, `HTTP_TIMEOUT` — присутствуют в `Settings` (`qbittorrent_timeout`, `emby_timeout`, `http_timeout`). ✅
- `RESULTS_PER_PAGE` в compose ✅, в env.example закомментирован.

**Итог**: расхождений нет. [x]

---

## Тесты, которые тестируют удалённый код

### TEST-DEAD-01: `tests/test_qbittorrent.py:475-485 test_confirm_delete_torrent` — тестирует `Keyboards.confirm_delete_torrent` (см. DEAD-03)
- Тест валиден, но keyboard в production не используется. Если удалить keyboard — удалить и тест.

### TEST-DEAD-02: `tests/test_qbittorrent.py:334-339 test_format_torrent_compact` — тестирует `Formatters.format_torrent_compact` (см. DEAD-05)
- Удалить вместе с функцией.

### TEST-DEAD-03: `tests/test_qbittorrent.py:359-368 test_format_torrent_action` — тестирует `Formatters.format_torrent_action` (см. DEAD-06)
- Удалить вместе с функцией.

### TEST-DEAD-04: `tests/test_qbittorrent.py:341-347 test_format_no_torrents` — тестирует `Formatters.format_no_torrents` (см. DEAD-07)
- Удалить вместе с функцией.

### TEST-DEAD-05: `tests/test_scoring.py: test_filter_by_quality_*` — 3 теста (LOW)
- **Файл**: `tests/test_scoring.py:283-348`
- **Проблема**: 3 теста для `filter_by_quality`, который не вызывается в production (см. DEAD-09).
- **Решение**: оставить тесты пока решение по DEAD-09 не принято; если функция активируется — тесты нужны. Если удаляется — удалить и тесты.

---

## Итого

| Категория | Кол-во |
|---|---|
| HIGH | 1 (DEAD-01 — `bot/constants.py` целиком dead) |
| MED | 1 (DEAD-09 — `filter_by_quality` + UI broken) |
| LOW | 19 (DEAD-02, 03, 05-08, 10-12, 15-22, 27-29 + DUP-01..04, 07, 08) |
| INFO/false positive | 5 (DEAD-04, 13, 14, 24-26, 30) |
| Deferred refactor | 2 (DUP-05, 06) |
| Test-only dead | 5 (TEST-DEAD-01..05) |

**Топ-3 приоритета:**
1. **DEAD-01** — удалить `bot/constants.py` или мигрировать на импорты (избавит от DUP-01, 02, 04 заодно).
2. **DEAD-09** — починить или скрыть `preferred_resolution` UI (user-visible broken feature: настройка ничего не меняет).
3. **DEAD-17** — `speed_limits_menu` теряет визуальные маркеры текущего лимита (UX-bug).

**Архитектурные указатели (не dead, но связанные):**
- DUP-03 — централизовать MENU_*. Очевидный win.
- DUP-05/06 — отложенный рефакторинг (LOGIC).
- DEAD-21 / DEAD-22 — поля моделей, которые заполняются, но не показываются → либо использовать (UX-улучшение), либо чистить.
