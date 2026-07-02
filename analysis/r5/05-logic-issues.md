# Анализ логики/архитектуры TG_arr (раунд 5)

Прочитаны все 40 файлов `bot/` (~12.9k строк). Крупные рефакторинги раунда 4 (ArrBaseClient, разбиение god-files, унификация grab_*) не дублируются — переоценены в отдельной секции.

## Критические

Критических находок (падение, потеря данных, дубль-операции) в этом раунде нет: гонки закрыты в раунде 4 (`_claim_grab`, `Database._write_lock`, `update_session`), side effects при импорте отсутствуют.

## Средние

### LOGIC-01: Пагинация и refresh списка торрентов сбрасывают выбранный фильтр
- **Файл**: `bot/handlers/downloads.py:212` (handle_page), `:175` (handle_refresh), `:261` (_render_torrent_list), `:578` (t_back)
- **Проблема**: `handle_filter_select` рендерит страницу 0 отфильтрованного списка, но кнопки пагинации несут только `t_page:N` без фильтра. `handle_page`, `handle_refresh`, `_render_torrent_list` и возврат из деталей всегда делают `get_torrents()` без фильтра и рендерят с `TorrentFilter.ALL`. Состояние фильтра нигде не хранится. `Keyboards.torrent_list` даже принимает `current_filter`, но не использует его (keyboards.py:469).
- **Риск**: юзер выбирает «Загрузка», листает на стр. 2 — видит уже нефильтрованный список; UX-ложь.
- **Решение**: типизированный `TorrentPageCB(page, filter)` (продолжение миграции #1) либо in-memory `_user_filter: dict[int, TorrentFilter]` по образцу `_user_period` из calendar.py.
- **Статус**: [ ] Не исправлено

### LOGIC-02: pause_all/resume_all вызывают handle_refresh — двойной ack колбэка, регресс паттерна BUG-15
- **Файл**: `bot/handlers/downloads.py:554`, `:571`
- **Проблема**: `handle_pause_all`/`handle_resume_all` делают свой `callback.answer(...)`, затем зовут `handle_refresh(callback)`, который делает второй `callback.answer("Обновляю...")`. Комментарии BUG-15 в этом же файле (строки 366, 438) прямо запрещают этот паттерн — но два хендлера остались непереведёнными.
- **Риск**: второй ack молча теряется или даёт TelegramBadRequest → ложный alert «Ошибка операции».
- **Решение**: заменить на `await _render_torrent_list(callback.message, qbt)` (как в delete-хендлерах).
- **Статус**: [ ] Не исправлено

### LOGIC-03: Меню лимитов скорости всегда помечает «Без лимита» как текущий пресет
- **Файл**: `bot/handlers/downloads.py:682`, `bot/ui/keyboards.py:605`
- **Проблема**: `handle_speed_menu` получает `status` с реальными `download_limit`/`upload_limit`, но вызывает `Keyboards.speed_limits_menu()` без аргументов. Дефолты 0/0 совпадают с пресетом «без лимита» → маркер всегда на «Без лимита». Логика маркера мёртвая с момента написания.
- **Решение**: `Keyboards.speed_limits_menu(status.download_limit, status.upload_limit)`. Заодно свернуть 4 копии цикла построения рядов в helper.
- **Статус**: [ ] Не исправлено

### LOGIC-04: Рендер страницы результатов поиска скопирован трижды в search.py
- **Файл**: `bot/handlers/search.py:292-323` (process_search), `:419-453` (handle_pagination), `:865-902` (handle_back)
- **Проблема**: блок «per_page → total_pages → срез → best_result → show_grab_best → format + Keyboards» повторён три раза почти дословно (~35 строк).
- **Решение**: локальный хелпер `_render_results_page(target_message, session, db_user, page)`.
- **Статус**: [ ] Не исправлено

### LOGIC-05: settings.py — 12 клонированных хендлеров + 4 API-вызова на каждый возврат в меню
- **Файл**: `bot/handlers/settings.py:106-428`
- **Проблема**: 6 пар menu/set — одинаковый код с точностью до prefix/getter/поля (~300 строк шаблона). Каждый клик заканчивается `handle_settings_back` → заново 4 HTTP-вызова (профили+папки Radarr и Sonarr).
- **Решение**: (1) табличная диспетчеризация + два generic-хендлера; (2) TTL-кеш профилей/папок (5–15 мин).
- **Статус**: [ ] Не исправлено

### LOGIC-06: Detection выбрасывает результаты lookup, затем флоу делает те же lookup'ы повторно (до 3 раз на один grab)
- **Файл**: `bot/services/search_service.py:107-111`, `bot/handlers/search.py:505,529` (второй lookup), `:752,790` (третий при отсутствии selected_content)
- **Проблема**: `detect_with_confidence` уже получил полные `MovieInfo`/`SeriesInfo`, но сохраняет только titles. `handle_release_selection` делает lookup заново; для «Скачать лучшее» `selected_content` не установлен → `_execute_grab` делает ещё один. Итог: 2–3 одинаковых запроса к *arr в одном флоу.
- **Риск**: лишние 0.5–2 с на шаг; лишняя нагрузка на *arr (усиливает PERF-01).
- **Решение**: сохранить объекты lookup'а из detection в `SearchSession`; минимум — покрыть grab_best через selected_content.
- **Статус**: [ ] Не исправлено

### LOGIC-07: Тексты кнопок главного меню живут в трёх несинхронизируемых местах
- **Файл**: `bot/handlers/search.py:63-67` (MENU_BUTTONS), `bot/ui/keyboards.py:116-124` (main_menu), плюс константы MENU_* в 8 handler-модулях
- **Проблема**: `handle_text_search` матчит «любой текст не из MENU_BUTTONS» — рукописной копии текстов кнопок.
- **Риск**: переименовали кнопку → нажатие уходит в текстовый поиск релизов.
- **Решение**: единый модуль-источник (`bot/ui/menu.py`), `MENU_BUTTONS = frozenset(...)`, main_menu строится из них же.
- **Статус**: [ ] Не исправлено

### LOGIC-08: Уведомления (qBit-поллер и webhook) игнорируют runtime-allowlist из фичи #6
- **Файл**: `bot/main.py:117`, `bot/main.py:319`
- **Проблема**: оба места итерируют только env-списки. Юзеры из `/adduser` не подписываются на уведомления; подписка одноразовая на старте. (= DB-04.)
- **Решение**: включить `await db.list_allowed_users()` в множество получателей; subscribe в `/adduser`.
- **Статус**: [ ] Не исправлено

### LOGIC-09: Конфиг не валидирует парные/зависимые настройки — интеграции молча отключаются
- **Файл**: `bot/config.py:142-160`
- **Проблема**: `lidarr_enabled = url is not None and api_key is not None` — если задана половина пары, интеграция тихо off без warning. Нет проверок `notify_download_complete=True` при неполном qBit; `webhook_enabled=True` без shared-secret.
- **Риск**: часовой дебаг «почему Lidarr не ищет» из-за опечатки в .env.
- **Решение**: `@model_validator(mode="after")`: «URL без ключа» → warning; поле `webhook_token: Optional[str]` (связка с SEC-02).
- **Статус**: [ ] Не исправлено

### LOGIC-10: Пласт мёртвого кода, включая 135-строчный grab_music_release и сломанный confirm-флоу удаления
- **Файл**: `bot/services/add_service.py:694-828`, `:169` (`self.prowlarr` не используется), `bot/ui/keyboards.py:684` (confirm_delete_torrent — мёртвый и сломанный), `bot/handlers/downloads.py:481,507` (t_recheck/t_prio без кнопок), `bot/ui/formatters.py:571,633,665`, `bot/services/scoring.py:275,303`, `bot/services/notification_service.py:200,238`, `bot/services/search_service.py:65`, `bot/clients/lidarr.py:321` (check_connection — дубль базового)
- **Проблема**: детали в 03-dead-code.md (DEAD-02..09). Особо: удаление торрента с файлами происходит без подтверждения, хотя UI подтверждения написан (но с несовместимым форматом callback).
- **Решение**: см. DEAD-отчёт; решить продуктово судьбу confirm-флоу удаления.
- **Статус**: [ ] Не исправлено

### LOGIC-11: trending.py — бизнес-логика и прямые вызовы клиентов в хендлере, третья копия резолва папки
- **Файл**: `bot/handlers/trending.py:458-481` (резолв TVDB через Sonarr lookup в хендлере), `:346-352,433-439` (ручная сборка AddService позиционно, без lidarr), `:364-369,450-456` (инлайн-копия `_resolve_folder`; третья копия — `bot/handlers/music.py:321`)
- **Проблема**: резолв «профиль из prefs или первый» и «папка из prefs или первая» существует в 4 вариантах.
- **Решение**: перенести `_resolve_folder`/`_resolve_profile` в AddService; TVDB-резолв — в AddService.add_series; сборку сервисов — через `get_services()`.
- **Статус**: [ ] Не исправлено

### LOGIC-12: downloads.py фильтрует колбэки строковыми литералами при живых константах CallbackData
- **Файл**: `bot/handlers/downloads.py:175,212,319,345,381,413,447,481,507,544,561,578,584,592,610,655` — 21 литерал
- **Проблема**: keyboards.py создаёт кнопки через `CallbackData.TORRENT_*`, а хендлеры матчат сырые строки. Переименование константы тихо отвяжет кнопки от хендлеров.
- **Решение**: минимум — константы в фильтрах; правильно — мигрировать семейство t_* на типизированный CallbackData (решает и LOGIC-01).
- **Статус**: [ ] Не исправлено

## Низкие

### LOGIC-13: Три разных способа парсинга аргументов команды
- **Файл**: `bot/handlers/search.py:85` (`_strip_command` — safe), `bot/handlers/downloads.py:124,153` (`text.replace("/pause","")` — все вхождения, не режет `@botname`), `bot/handlers/music.py:74` (без `@botname`)
- **Риск**: `/pause@mybot all` → args=`"@mybot all"` → поиск торрента по мусорному «хешу».
- **Решение**: вынести `_strip_command` в общий util.
- **Статус**: [ ] Не исправлено

### LOGIC-14: Рендер списка артистов скопирован трижды, back ≡ пагинация(0), per_page захардкожен
- **Файл**: `bot/handlers/music.py:156-166,196-211,225-240`; per_page=5 в `music.py:187` и `keyboards.py:360`; `artists[:25]` в `music.py:137`
- **Решение**: хелпер `_render_artist_list(message, artists, page)`; per_page из settings.
- **Статус**: [ ] Не исправлено

### LOGIC-15: Паттерн «message is not modified» размазан по ~10 местам, в music.py — через голый Exception
- **Файл**: `bot/handlers/downloads.py` (6 мест), `bot/handlers/emby.py:70`, `bot/handlers/music.py:208,237` (ловится `Exception`)
- **Решение**: `async def safe_edit(message, text, **kw)` в ui-утилитах.
- **Статус**: [ ] Не исправлено

### LOGIC-16: Двойная проверка qBittorrent в каждом хендлере
- **Файл**: `bot/handlers/downloads.py:49-54,90-95,116-121,145-150`
- **Решение**: `check_qbt_enabled` возвращает `Optional[QBittorrentClient]`.
- **Статус**: [ ] Не исправлено

### LOGIC-17: cmd_status и cmd_health дублируют сборку health-чеков
- **Файл**: `bot/handlers/status.py:81-101` и `:152-170`
- **Решение**: общий `_collect_statuses(include_deezer: bool)`.
- **Статус**: [ ] Не исправлено

### LOGIC-18: Инверсия слоёв: services → ui; webhook сам форматирует HTML
- **Файл**: `bot/services/notification_service.py:13` (import Formatters из ui), `bot/webhook.py:32-74`
- **Проблема**: сервисный слой зависит от ui; webhook показывает только `episodes[0]` — импорт сезона выглядит как одна серия.
- **Решение**: формат в колбэке main.py; parse_arr_event → структура; диапазон "S01E01-E10" для пачки.
- **Статус**: [ ] Не исправлено

### LOGIC-19: Функции с 6+ аргументами и мёртвая обёртка grab_release
- **Файл**: `bot/handlers/search.py:680-689` (pass-through, но patch-point в тестах), `:727` (_execute_grab — 7), `bot/ui/keyboards.py:141`, `bot/ui/formatters.py:691` (format_emby_status — 7 скаляров), `bot/clients/lidarr.py:49` (add_artist — 8)
- **Решение**: format_emby_status принимает `EmbyServerInfo`; остальное по мере.
- **Статус**: [ ] Не исправлено

### LOGIC-20: show_emby_status(message_or_callback, edit=bool) — флаг-переключатель с isinstance-ветвлением
- **Файл**: `bot/handlers/emby.py:23-104`
- **Решение**: `_render_status_text()` + два тонких вызывающих.
- **Статус**: [ ] Не исправлено

### LOGIC-21: In-memory состояние хендлеров против SQLite-сессий — несогласованная модель хранения
- **Файл**: `bot/handlers/music.py:47-48`, `bot/handlers/trending.py:27-28`, `bot/handlers/calendar.py:23`
- **Проблема**: три разных cap-механизма; clear-all при переполнении выбивает кеш у всех юзеров разом.
- **Решение**: общий bounded-dict util; артист-кандидатов можно хранить в SearchSession.
- **Статус**: [ ] Не исправлено

### LOGIC-22: Мелкие несуразности (пакетом)
- `bot/handlers/search.py:160` и `bot/handlers/music.py:99` — MAX_QUERY_LENGTH=200 дважды.
- `bot/handlers/search.py:70-82` — get_services() возвращает scoring, который никто не использует.
- `bot/handlers/music.py:33` — импорт приватного `_SCORING_SERVICE` из чужого handler-модуля.
- `bot/handlers/trending.py:460` — повторный `await get_sonarr()` (уже получен на :437).
- `bot/handlers/downloads.py:622-624` — мёртвая ветка `filter_value == "menu"`.
- `bot/ui/keyboards.py:469,473` — неиспользуемый `current_filter`, бессмысленное `page_torrents = torrents`.
- `bot/ui/formatters.py:970` — `except (ZoneInfoNotFoundError, Exception)`; get_settings()/ZoneInfo на каждую дату календаря.
- `bot/ui/formatters.py:364` — в истории music-действия получают эмодзи сериала.
- `bot/clients/qbittorrent.py:321` — docstring «first 8 chars», фактически 16.
- `bot/models.py:385` — магическое `8640000` без именованной константы.
- `bot/main.py:117,319` — `settings.admin_tg_ids or []` — поле уже default_factory=list.
- `"noop"` — литерал в 6+ местах keyboards, не в классе CallbackData.
- Лимит 500 результатов сессии — три места истины (models.py, db.py save_session, db.py update_session).
- **Статус**: [ ] Не исправлено

### LOGIC-23: process_search при ошибке оставляет висеть статус-сообщение
- **Файл**: `bot/handlers/search.py:339-341`
- **Проблема**: except шлёт ошибку новым `message.answer`, а «Ищу релизы...» остаётся висеть. После выбора типа старое сообщение с кнопками остаётся активным — повторный клик запускает второй поиск.
- **Решение**: редактировать status_msg в except; после выбора типа — edit исходного сообщения.
- **Статус**: [ ] Не исправлено

## Отложенный рефакторинг (переоценка)

- **Унификация grab_movie/series/music_release** — актуальность выросла, объём уменьшился: после удаления мёртвого `grab_music_release` остаются 2 копии по ~150 строк. Шаблонный метод внутри AddService — локальный рефакторинг. Рекомендуется в ближайший PR вместе с удалением мёртвого кода.
- **ArrBaseClient** — по-прежнему отложен, но дешёвый промежуточный шаг: class-атрибут `_api_prefix` + generic-методы в BaseAPIClient (push_release/get_quality_profiles/get_root_folders/check_connection в трёх клиентах дословно одинаковы).
- **God-files (search.py 1009, formatters.py 1005, keyboards.py 925)** — рост остановился; разбиение остаётся отложенным.
- **FSM-миграция сессий (#3)** — «отложить» остаётся верным.
- **Доведение миграции CallbackData (#1)** — самый приоритетный из отложенных: попутно чинит LOGIC-01, LOGIC-12.

## Карта миграции CallbackData

Типизировано: **1 семейство из ~20**. Девять литералов `settings:*` вообще не объявлены в классе `CallbackData`.

| Колбэк | Где создаётся / матчится | Типизирован? |
|---|---|---|
| `pg:search:N` (PageCB) | keyboards.py:195,204 / search.py:388 | Да |
| `art_page:N` | keyboards.py:385-392 / music.py:169 | Нет — первый кандидат `PageCB(scope="artist")` |
| `type:movie/series/music` | keyboards.py:131-136 / search.py:344 | Нет |
| `rel:N` | keyboards.py:178 / search.py:456 | Нет |
| `grab_best`, `confirm_grab`, `force_grab` | keyboards.py / search.py:600,632,918 | Нет |
| `back`, `cancel`, `music_back` | keyboards.py / search.py, music.py | Нет |
| `season_menu`, `season_set:*` | keyboards.py:250,264-267 / search.py:950,969 | Нет |
| `artist:N` | keyboards.py:377 / music.py:243 | Нет |
| `add_movie:ID`, `add_series:ID` | keyboards.py:888,899 / trending.py:308,400 | Нет |
| `settings` + `settings:*` (9 шт.) | keyboards.py:319-336 / settings.py | Нет; литералы вне класса |
| `set:rp/rf/sp/sf/lp/lm/lf/res/ag:` | keyboards.py / settings.py | Нет |
| `t:` `t_pause:` `t_resume:` `t_delete:` `t_delf:` `t_refresh` `t_filter:` `t_page:` `t_back` `t_pause_all` `t_resume_all` `t_close` `speed:` `speed_menu` | keyboards.py / downloads.py (литералы, LOGIC-12) | Нет — второй приоритет: t_page с полем filter чинит LOGIC-01 |
| `t_recheck:` `t_prio:` | только хендлеры — кнопок нет | Мёртвые (DEAD-02) |
| `emby_*` (8 шт.) | keyboards.py / emby.py | Нет |
| `trending_*`, `trend_m/s/a:` | keyboards.py / trending.py, music.py | Нет |
| `cal_7` `cal_14` `cal_30` `cal_refresh` | keyboards.py:908-925 / calendar.py | Нет — тривиальный `CalCB(days:int)` |
| `noop` | keyboards.py (6+ мест) / search.py:1006 | Нет; литерал вне класса |

## Проверено — проблем нет

- **Side effects при импорте**: клиенты лениво через registry под asyncio-локами; webhook стартует только из main() при флаге.
- **Диспетчеризация CONFIRM_GRAB (BUG-27)**: единая точка с ветвлением по типу; music.py корректно не регистрирует свой хендлер.
- **Grab-guard (RACE-01)**: все четыре пути через общий `_claim_grab`, release в finally.
- **SSRF-валидация**: присутствует во всех push/qBit-путях всех трёх grab-методов.
- **DB-слой**: `_write_lock` у всех писателей; `update_session` применён в slow-lookup путях; RACE-05 соблюдён.
- **Строковые префиксы колбэков**: коллизий не найдено; порядок регистрации `t_filter:menu` до `t_filter:*` корректен.
- **config.parse_comma_separated_ids**: корректен (str/int/bool/list/None).
- **webhook.py**: невалидный JSON → 400, ошибки notify не роняют ответ *arr.
- **Auth-middleware**: fail-closed, DB-allowlist с деградацией в deny.
