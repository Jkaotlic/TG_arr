# Анализ багов TG_arr (раунд 5)

Прочитаны все файлы `bot/`. Ключевая находка подтверждена по исходникам Radarr/Sonarr (upstream): `POST /release/push` возвращает **массив**, а не объект.

## Критические

### BUG-01: `push_release` выбрасывает ответ *arr (list → `{}`) — каждый push трактуется как «no explicit approval», guided-флоу сломан для ВСЕХ грабов
- **Файл**: `bot/clients/radarr.py:176`, `bot/clients/sonarr.py:205`, `bot/clients/lidarr.py:131` (потребители: `bot/services/add_service.py:401-416`, `:555-570`, `:758-771`)
- **Проблема**: Radarr/Sonarr/Lidarr `POST /api/v3(v1)/release/push` возвращает `List<ReleaseResource>` (проверено по `ReleasePushController.cs` в develop-ветках Radarr и Sonarr: `ActionResult<List<ReleaseResource>>` + `return MapDecisions(new[] { decision })`). Клиент делает `return result if isinstance(result, dict) else {}` — массив с полем `approved: true` превращается в `{}`. В `add_service` `result.get("approved") is True` никогда не срабатывает → `release_rejected = True` **всегда** (при наличии `download_url`). Последствия: (a) лог ровно как в проде — `"no explicit approval from Radarr/Sonarr"`; (b) прямой grab пропускается (add_service.py:421/575/775); (c) на обычном «✅ Скачать» релиз молча уходит в qBittorrent-байпас мимо *arr (add_service.py:431/585/784) — без импорт-трекинга/переименования; (d) без настроенного qBit — юзер видит «Релиз отклонён» и жмёт force_grab (наблюдалось в проде).
- **Риск**: Высокий
- **Решение**: в `push_release` всех трёх клиентов разворачивать список: `if isinstance(result, list): return result[0] if result and isinstance(result[0], dict) else {}`. Тесты мокают `push_release` уже dict'ом (`tests/test_r4_C8-coverage.py:354`) — добавить тест на list-ответ на уровне клиента.
- **Статус**: [ ] Не исправлено

## Средние

### BUG-02: Осиротевшие legacy-кнопки `page:N` после полумиграции на PageCB — вечные «часики»
- **Файл**: `bot/ui/keyboards.py:195,204` (новый формат `pg:search:N`), `bot/handlers/search.py:388` (хендлер только `PageCB`)
- **Проблема**: a430ad3 удалил строковый хендлер `page:`, но сообщения, отправленные до деплоя, содержат кнопки `page:N`. Ни один фильтр их не матчит → callback без `answer()` → бесконечный спиннер.
- **Риск**: Средний
- **Решение**: legacy-хендлер `F.data.startswith("page:")` → `callback.answer("Кнопка устарела — повторите поиск", show_alert=True)`.
- **Статус**: [ ] Не исправлено

### BUG-03: `handle_pagination`/`handle_back` не гасят «message is not modified» — дабл-тап оставляет «часики»
- **Файл**: `bot/handlers/search.py:440-453` (пагинация), `:889-902` (back)
- **Проблема**: быстрый дабл-тап «▶️»: второй callback рисует идентичные текст+клавиатуру → `TelegramBadRequest: message is not modified`, не перехвачено → `callback.answer()` не выполняется → часики. В downloads.py:203-205 и music.py:208-210 обёрнуто, в search.py — нет.
- **Риск**: Средний
- **Решение**: try/except `TelegramBadRequest` («message is not modified») + всегда `callback.answer()`.
- **Статус**: [ ] Не исправлено

### BUG-04: Двойной `callback.answer()` — после «Пауза всех»/«Возобновить все» список НЕ перерисовывается
- **Файл**: `bot/handlers/downloads.py:553-554`, `:570-571` (→ `handle_refresh`, снова `answer` на :187 **до** перерисовки); тот же паттерн: `downloads.py:720-723` → `handle_speed_menu` (:688), `bot/handlers/settings.py:146-149` и все 8 set-хендлеров → `handle_settings_back` (:99), `bot/handlers/emby.py:138-139,164-168,193-197,249-250,300-301` → `show_emby_status` (:74)
- **Проблема**: повторный `answerCallbackQuery` отклоняется Telegram. В pause_all/resume_all второй `answer` стоит ПЕРЕД перерисовкой → исключение обрывает `handle_refresh`, список остаётся старым. В settings/emby/speed — лог-шум. Это класс BUG-15, починенный в р4 для delete, но не для остальных.
- **Риск**: Средний
- **Решение**: render-хелперы без `answer` (как `_render_torrent_list`); `answer` — ровно один раз на callback.
- **Статус**: [ ] Не исправлено

### BUG-05: «Прямой grab» шлёт Prowlarr-guid в Radarr/Sonarr/Lidarr `/release` — мёртвый путь, маскируемый «Запущен автопоиск»
- **Файл**: `bot/clients/radarr.py:129-145`, `bot/clients/sonarr.py:158-174`, `bot/clients/lidarr.py:108-112`; вызовы `bot/services/add_service.py:421-428, 575-582, 775-782`
- **Проблема**: `POST /api/v3/release {guid, indexerId}` у *arr граббит только релиз из ИХ кэша интерактивного поиска; guid/indexerId из Prowlarr *arr неизвестны → всегда 404/ошибка. Сейчас путь недостижим из-за BUG-01; после фикса BUG-01 станет fallback'ом при `APIError` пуша и будет всегда падать → тихо триггерится общий автопоиск с сообщением об успехе, а выбранный релиз теряется.
- **Риск**: Средний
- **Решение**: убрать вызовы `*arr.grab_release` c Prowlarr-параметрами; сообщение «Запущен автопоиск» пометить как fallback, а не успех граба.
- **Статус**: [ ] Не исправлено

### BUG-06: TZ-рассинхрон в календаре: даты релизов в Moscow, «сегодня» — в UTC
- **Файл**: `bot/ui/formatters.py:873-874` (`today = datetime.now(timezone.utc).date()`) vs `:962-975` (`_extract_date_key` конвертирует в `settings.timezone`)
- **Проблема**: между 00:00 и 03:00 MSK UTC-дата — ещё «вчера»: эпизод, выходящий сегодня по Москве, подписывается «завтра». Дополнительно все datetime выводятся в UTC без конверсии (−3ч): `formatters.py:154` (publish_date), `:561,565` (added/completed торрента), `:627` (уведомление), `:372` (история).
- **Риск**: Средний
- **Решение**: `today = datetime.now(ZoneInfo(get_settings().timezone)).date()`; общий хелпер `to_local(dt)`.
- **Статус**: [ ] Не исправлено

### BUG-07: `callback.answer()` в конце `handle_release_selection` — после сетевых lookup до 30-95с
- **Файл**: `bot/handlers/search.py:560` (answer), `:504-558` (lookup + `_emby_library_note` до него)
- **Проблема**: выбор релиза сначала делает lookup в Radarr/Sonarr (HTTP_TIMEOUT=30с × до 3 попыток) и лишь потом отвечает на callback. Пока *arr туп (прод-503) — часики; `answer` спустя >15-30с отклоняется («query is too old»), исключение на :560 не перехвачено.
- **Риск**: Средний
- **Решение**: `await callback.answer()` сразу после валидации сессии; финальный ack убрать.
- **Статус**: [ ] Не исправлено

## Низкие

### BUG-08: Inbound-вебхук без аутентификации на 0.0.0.0
- **Файл**: `bot/webhook.py:93-96`, `bot/config.py:100`
- **Проблема**: = SEC-02. Любой хост в LAN может заспамить юзеров фейковыми уведомлениями.
- **Решение**: секретный путь `/webhook/<token>` или заголовок; токен в конфиг.
- **Статус**: [ ] Не исправлено

### BUG-09: `QBittorrentClient` без локов на создание клиента/логин (гонка notification-loop vs хендлеры)
- **Файл**: `bot/clients/qbittorrent.py:77-98`
- **Проблема**: `_get_client` не защищён (в `BaseAPIClient` есть `_client_lock`, тут — нет): при одновременном первом обращении создаются два `AsyncClient`, один утекает. `_ensure_authenticated` тоже без лока → параллельные логины (в проде «session expired → re-auth» наблюдался).
- **Риск**: Низкий
- **Решение**: `asyncio.Lock` вокруг создания клиента и login (по образцу base.py).
- **Статус**: [ ] Не исправлено

### BUG-10: Очистка per-user кэшей целиком сбрасывает активные выборы других юзеров
- **Файл**: `bot/handlers/music.py:39-42` (`cache.clear()` при >100), `bot/handlers/trending.py:120-122,170-173`
- **Проблема**: при переполнении сносится весь dict — юзер посреди выбора получит «Список истёк». (= PERF-12 частично.)
- **Решение**: удалять старейшие записи (LRU), не clear().
- **Статус**: [ ] Не исправлено

### BUG-11: Страница результатов поиска не ограничена 4096 символами
- **Файл**: `bot/ui/formatters.py:79-98` (и карточка в `search.py:517-521`)
- **Проблема**: 5 релизов с длинными заголовками RuTracker могут превысить 4096 → `MESSAGE_TOO_LONG` → «Поиск временно недоступен» при живом поиске. (= TEST-07.)
- **Решение**: `_safe_truncate(…, 3800)` для страницы; резать `result.title` до ~150 символов.
- **Статус**: [ ] Не исправлено

### BUG-12: Неэкранированный пользовательский/сервисный текст при default parse_mode=HTML
- **Файл**: `bot/handlers/downloads.py:136,164` (`f"❌ Торрент не найден: {args}"`), `bot/handlers/trending.py:390,504` (`f"❌ Ошибка: {error_msg}"`)
- **Проблема**: `args` — сырой ввод юзера, `error_msg` — текст ошибки *arr; `<` ломает HTML-парсинг → сообщение не отправляется, исключение не обработано → юзер без ответа.
- **Решение**: `html.escape()` для обеих подстановок.
- **Статус**: [ ] Не исправлено

### BUG-13: TMDb: v3-ключ отправляется как Bearer-токен
- **Файл**: `bot/clients/tmdb.py:50-56`, `bot/config.py:63` («TMDb API key (v3)»)
- **Проблема**: `Authorization: Bearer` принимает только v4-токен; с v3-ключом все запросы дадут 401. В проде трендинг работает — значит в env лежит v4-токен, но описание конфига вводит в заблуждение.
- **Решение**: слать v3-ключ параметром `api_key` при отсутствии `eyJ`-префикса, либо поправить описание/валидацию.
- **Статус**: [ ] Не исправлено

### BUG-14: Удаление торрента без подтверждения; клавиатура подтверждения — мёртвый код с битым префиксом
- **Файл**: `bot/ui/keyboards.py:684-703`, `bot/handlers/downloads.py:428,462`
- **Проблема**: = DEAD-03. «🗑 + Файлы» удаляет сразу (admin-gated, но необратимо).
- **Решение**: подключить confirm-клавиатуру (и починить префикс) либо удалить.
- **Статус**: [ ] Не исправлено

### BUG-15: Runtime-allowlist юзеры (#6) не получают уведомления и вебхук-нотификации
- **Файл**: `bot/main.py:117-118`, `:319`
- **Проблема**: = DB-04/LOGIC-08.
- **Решение**: включать `db.list_allowed_users()` в оба множества; подписывать при `/adduser`.
- **Статус**: [ ] Не исправлено

### BUG-16: «Назад» из меню мониторинга сезонов сбрасывает выбранный релиз
- **Файл**: `bot/ui/keyboards.py:268` (кнопка → `CallbackData.BACK`) → `bot/handlers/search.py:843-863`
- **Проблема**: `handle_back` очищает `selected_result`/`selected_content` и возвращает к списку, хотя юзер ожидает карточку релиза.
- **Решение**: отдельный callback `season_back`, перерисовывающий карточку релиза.
- **Статус**: [ ] Не исправлено

### BUG-17: Мелкие: календарь без ack-гарда и мёртвый rate-limit cleanup
- **Файл**: `bot/handlers/calendar.py:112-115,127-130,142-145` (повтор активного периода → «message is not modified» в лог); `bot/middleware/auth.py:200-204` (условие `if not reqs` невыполнимо — cleanup не удалит ничего; = PERF-09)
- **Решение**: try/except вокруг `edit_text` в календаре; чистить юзеров со всеми таймстампами старше окна.
- **Статус**: [ ] Не исправлено

## Проверено — проблем нет
- **Длины callback_data**: максимум ~23 байта — везде < 64; кнопок ≤ ~13 — < 100.
- **Границы пагинации**: guard `page < 0 or page >= total_pages` в search/music/downloads; clamp в `handle_back`; пустых страниц не возникает.
- **RACE-01 (двойной grab)**: per-user claim с lock + `finally: release`; двойной тап и Confirm→Force отсекаются.
- **RACE-04**: `update_session` (UPDATE-only) не воскрешает сессию, удалённую во время медленного lookup.
- **БД**: единый `_write_lock`, busy_timeout, транзакции с rollback; datetime консистентно UTC-ISO.
- **SSRF-валидатор (7b3de49)**: trust — точное совпадение hostname; все A/AAAA проверяются; magnet — только `xt=urn:btih:`. (Но см. SEC-01: порт игнорируется.)
- **qBit re-auth**: 403 → один повтор после успешного login; `add_torrent_url` различает «Fails.»/пустой ответ (v5.2).
- **NotificationService**: state-machine completed/notified корректна; удалённые хэши чистятся; ошибки цикла не убивают поллер.
- **Prowlarr retry**: без retry-поверх-retry; 5xx не ретраится.
- **Экранирование HTML**: `_e()` повсеместно (кроме BUG-12/SEC-04); webhook экранирует title/instance.
- **Watchdog/liveness/фоновые таски**: ссылки хранятся, cancel в finally.
- **`_decide_monitor_type`**: пресет юзера > force=all > season-pack=all > одиночный сезон=none — корректно.

**Примечание к прод-симптомам**: «no explicit approval from Radarr/Sonarr» — это ровно BUG-01 (list-ответ push отбрасывается), а не реальный отказ Radarr; одновременные 503 Radarr/Lidarr на lookup — транзиентная недоступность самих сервисов + PERF-01 (detection-burst).
