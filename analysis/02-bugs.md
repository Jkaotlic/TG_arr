# Анализ bugs TG_arr

## Критические

### BUG-01: `log` используется до присвоения при исключении в `process_search`
- **Файл**: `bot/handlers/search.py:140,245`
- **Проблема**: Переменная `log` инициализируется внутри `try:` (строка 140 — `log = logger.bind(...)`). Если исключение поднимется до этой строки (например, `await message.answer(...)` упадёт при недоступности Telegram API), то в `except` (строка 245) `log.error(...)` выбросит `NameError: name 'log' is not defined`, что полностью скроет исходное исключение.
- **Риск**: Высокий — пользователи получат молчание или непонятную ошибку.
- **Решение**: Вынести `log = logger.bind(user_id=user_id, query=query)` перед `try:` (на строку 136-137).
- **Статус**: [ ] Не исправлено

### BUG-02: Race condition в `trending.py` — глобальные кеши мутируются без lock во всех путях
- **Файл**: `bot/handlers/trending.py:178-179, 238-239, 292-293, 378-379`
- **Проблема**: `_trending_movies_cache.get(tmdb_id)` и `.update(...)` защищены `_cache_lock` в местах записи, но чтения (handle_movie_from_trending, handle_add_movie_from_trending и т.д.) идут без lock. В asyncio это обычно безопасно для `dict.get()`, но при одновременной записи (`= {}` в ветке cleanup) возможна ситуация, когда чтение попадёт между очисткой и заполнением → `None` даже если только что добавили. Более важно: `global _trending_movies_cache` в handle_trending_movies **переназначает переменную**, а другие handlers держат ссылку через closure/module-level resolve — но `_trending_movies_cache.get(...)` всегда читает текущий модульный атрибут, так что OK.
- **Риск**: Низкий (dict.get — atomic в CPython), но логика `= {}` при переполнении теряет только что добавленные данные, если одновременно идёт чтение.
- **Решение**: Использовать `OrderedDict` + `popitem(last=False)` для LRU вместо полной очистки.
- **Статус**: [ ] Не исправлено

### BUG-03: `cursor.rowcount` возвращается из `cleanup_old_searches` без reassignment
- **Файл**: `bot/db.py:403-410`
- **Проблема**: `cursor = await self.conn.execute(...)` — переменная затеняет cursor для DELETE FROM search_results. Затем `return cursor.rowcount` возвращает rowcount от DELETE FROM searches. Это правильно, но в комментарии указано «delete searches» — комментарий OK. Реальная проблема: `await self.conn.execute("BEGIN")` в режиме `isolation_level=None` (autocommit) в SQLite — ведёт себя странно. В autocommit режиме `BEGIN` не начинает транзакцию, которая откатывается через `rollback()`. Это работает, но не как ожидает читатель кода.
- **Риск**: Средний (транзакционность при сбое).
- **Решение**: Либо убрать `isolation_level=None` (дефолт SQLite — DEFERRED transactions auto-begin), либо использовать явные `async with self.conn.execute("BEGIN TRANSACTION")`.
- **Статус**: [ ] Не исправлено

### BUG-04: `NotFoundError` бросается до retry — 404 не retryable, но retry/stop_after_attempt(3) применяется к ConnectError, что нормально; реальная проблема — ошибки 5xx кроме 503/504
- **Файл**: `bot/clients/base.py:147-161`
- **Проблема**: Ретраим только 429/503/504. 500, 502, 507 бросают `APIError` без retry. Особенно 502 (временный) — нужно ретраить.
- **Риск**: Средний.
- **Решение**: Добавить 500, 502 в список retryable.
- **Статус**: [ ] Не исправлено

### BUG-05: `search_service.detect_content_type` падает на None-запросах — проверки недостаточно
- **Файл**: `bot/services/search_service.py:61-66`
- **Проблема**: `asyncio.gather(..., return_exceptions=True)` — OK, но `movies_result if not isinstance(movies_result, Exception) else []` — если `lookup_movie` вернёт `None` вместо списка, `movies` станет None и `for movie in movies[:3]` упадёт.
- **Риск**: Низкий (маловероятно для корректных клиентов).
- **Решение**: `movies = movies_result if isinstance(movies_result, list) else []`.
- **Статус**: [ ] Не исправлено

## Высокие

### BUG-06: `emby.py` — `EmbyError` при httpx.TimeoutException **внутри** retry-декоратора — tenacity не ретраит
- **Файл**: `bot/clients/emby.py:130-133`
- **Проблема**: `retry(retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError)))` — OK. Но внутри `try: ... except httpx.TimeoutException: raise EmbyError(...)` **конвертирует** TimeoutException в EmbyError **до выхода из try**, и tenacity не видит TimeoutException → retry не сработает. Тот же баг в `qbittorrent.py:192-195`.
- **Риск**: Высокий — потеря функциональности retry.
- **Решение**: Убрать except внутри декоратора, перенести конверсию в обёртку (как сделано в `base.py:175-189` через `_safe_request`).
- **Статус**: [ ] Не исправлено

### BUG-07: `qbittorrent.py` — после повторной аутентификации (403) запрос не повторяется, если сессия всё равно истекла
- **Файл**: `bot/clients/qbittorrent.py:156-174`
- **Проблема**: При получении 403 → `_authenticated = False` → вызывается `_ensure_authenticated()` → новый `login()`. Но re-issue запроса выполняется в том же try/except, и если новый запрос снова вернёт 403 (сессия не создалась, или куки не передаются — httpx по умолчанию управляет cookies через `cookies=`, но здесь `AsyncClient` создаётся без явного cookies-jar, хотя httpx.AsyncClient поддерживает cookies по умолчанию), пользователь получит ошибку.
- **Риск**: Средний.
- **Решение**: Явно передавать `cookies=httpx.Cookies()` в AsyncClient и проверять статус после re-issue.
- **Статус**: [ ] Не исправлено

### BUG-08: `prowlarr.py` — `_extract_year` ловит год в названии (`Shows 2022`), даже если это часть имени серии типа «2020»
- **Файл**: `bot/clients/prowlarr.py:302-317`
- **Проблема**: `r"[\(\[](\d{4})[\)\]]"` срабатывает на `[2020]`. В релизе `Show [2020] Complete Pack` — это не год премьеры, а часть обозначения. Потом этот year попадает в `detected_year` и участвует в матчинге.
- **Риск**: Низкий.
- **Решение**: Проверять, что перед/после года нет цифр, и значение в разумных пределах (уже делается 1900-2100).
- **Статус**: [ ] Не исправлено

### BUG-09: `BaseAPIClient.check_connection` всегда бьёт `/api/v1/system/status`, но Prowlarr v1, Radarr v3, Sonarr v3
- **Файл**: `bot/clients/base.py:248-259`
- **Проблема**: Метод в базовом клиенте использует v1. Radarr и Sonarr **переопределяют** `check_connection` (radarr.py:313-324, sonarr.py:368-379), но если забыть переопределить — будет 404. Это не баг прямо сейчас, но fragile дизайн.
- **Риск**: Низкий.
- **Решение**: Сделать `check_connection` абстрактным, либо использовать атрибут `api_version` в классе.
- **Статус**: [ ] Не исправлено

### BUG-10: `radarr.get_calendar` не учитывает timezone — `digitalRelease` приходит в UTC
- **Файл**: `bot/clients/radarr.py:196-228`
- **Проблема**: `start = now.strftime("%Y-%m-%d")` — это UTC дата, но пользователь видит в `TIMEZONE=Europe/Moscow`. Релиз сегодня в 23:00 UTC отобразится как «завтра» в Москве.
- **Риск**: Низкий.
- **Решение**: Конвертировать в локальную зону перед форматированием.
- **Статус**: [ ] Не исправлено

### BUG-11: `downloads.py:310` — рекурсивный вызов `handle_torrent_details(callback)` передаёт старый callback, у которого `callback.answer()` уже вызван
- **Файл**: `bot/handlers/downloads.py:310,339,372,405,485,502,654`
- **Проблема**: После `await callback.answer(...)` повторный `await callback.answer(...)` в рекурсивно вызванном handler'е выбросит `TelegramBadRequest: query is too old and response timeout expired or query ID is invalid`. Видно по тому, что `handle_torrent_details` в конце делает `await callback.answer()`.
- **Риск**: Высокий — UI постоянно ломается.
- **Решение**: Рефакторинг: выделить `_show_torrent_details(message, hash)` без callback, вызывать напрямую.
- **Статус**: [ ] Не исправлено

### BUG-12: `_execute_grab` — при force_download + rejected без download_url и magnet_url молча уйдёт в fallback search
- **Файл**: `bot/services/add_service.py:303-339`
- **Проблема**: Если release_rejected=True и qbittorrent есть, но `download_url = release.download_url or release.magnet_url` пустой (например, magnet indexer без download_url), условие `if download_url:` не сработает, блок скипается, и переход к fallback «search_movie» — хотя пользователь явно нажал «force grab».
- **Риск**: Средний (UX + неправильные ожидания пользователя).
- **Решение**: Явно возвращать ошибку «нет URL для force-скачивания».
- **Статус**: [ ] Не исправлено

### BUG-13: `models.py:325` — `if hours > 24` (после divmod 3600) сравнивает общее часы, а не часы-после-divmod-3600
- **Файл**: `bot/models.py:319-335` (`eta_formatted`)
- **Проблема**: После `hours, remainder = divmod(self.eta, 3600)` — `hours` это абсолютное число часов (например 48). Затем `if hours > 24: days = hours // 24; hours = hours % 24`. Это правильно. Но `return f"{days}d {hours}h"` теряет minutes. Если eta = 2 дня 5 часов 30 минут = 192600 сек → hours=53, remainder=1800, minutes=30. После days=2, hours=5 → "2d 5h" — minutes потеряны. Не баг, но мелкая неточность.
- **Риск**: Низкий (UX).
- **Решение**: Включать минуты в формат `{days}d {hours}h {minutes}m`.
- **Статус**: [ ] Не исправлено

### BUG-14: `SearchSession.results: max_length=500` — если Prowlarr вернёт >500, pydantic.validator упадёт
- **Файл**: `bot/models.py:213`
- **Проблема**: `limit=100` в `prowlarr.search` (prowlarr.py:30), так что по-умолчанию не превышает. Но если вручную выставить больше — `model_validate` упадёт.
- **Риск**: Низкий.
- **Решение**: Обрезать `results[:500]` перед сохранением.
- **Статус**: [ ] Не исправлено

### BUG-15: Message length не контролируется — `format_search_results_page` может превысить 4096 байт Telegram
- **Файл**: `bot/ui/formatters.py:77-96`
- **Проблема**: В отличие от `format_calendar` (где MAX_MSG_LEN=3800), поиск и другие форматеры не обрезают результат. При `per_page=5` и длинных названиях (Cyrillic multi-byte) легко превысить.
- **Риск**: Средний (сообщение не доставится).
- **Решение**: Добавить truncation.
- **Статус**: [ ] Не исправлено

### BUG-16: `LoggingMiddleware` логирует `text[:50]` — URL-ы и токены попадают в логи
- **Файл**: `bot/middleware/auth.py:116`
- **Проблема**: `text=event.text[:50]` — первые 50 символов команды. Для `/start PAYLOAD` payload может содержать data.
- **Риск**: Низкий.
- **Решение**: Маскировать или не логировать text.
- **Статус**: [ ] Не исправлено

### BUG-17: `callback.data.replace(CallbackData.TRENDING_MOVIE, "")` — .replace удаляет все вхождения
- **Файл**: `bot/handlers/trending.py:171,231,285,371`; `bot/handlers/settings.py:134,186,237,288,326,368`; `bot/handlers/search.py:296,366`
- **Проблема**: `str.replace("set:rp:", "")` на `"set:rp:set:rp:10"` (маловероятно, но возможно при коллизиях) вернёт «10». Безопаснее `str.removeprefix()` (Py 3.9+) или проверять `startswith + len-skip`.
- **Риск**: Низкий (не возникает в штатном использовании).
- **Решение**: `callback.data.removeprefix(CallbackData.X)`.
- **Статус**: [ ] Не исправлено

### BUG-18: `process_search` вызывается из `handle_type_selection` с `callback.message` вместо отдельного message
- **Файл**: `bot/handlers/search.py:271-277`
- **Проблема**: `callback.message` — это сообщение, отправленное ботом (от имени бота). `message.from_user` у него — bot, поэтому в комменте `bot/handlers/search.py:138` указано: «Always use db_user.tg_id - message.from_user can be bot when called from callback». OK. Но затем `message.answer("🔍 Определяю тип контента...")` (строка 148) — отвечает в тот же chat, что правильно. Только `message.answer(...)` в контексте callback'а создаёт новое сообщение, в то время как правильнее было бы edit_text. Это UX-баг: появляется лишнее сообщение.
- **Риск**: Низкий (UX).
- **Решение**: Отправлять новое сообщение в chat через `bot.send_message(callback.message.chat.id, ...)`.
- **Статус**: [ ] Не исправлено

## Средние

### BUG-19: `tenacity` `stop_after_attempt(3)` + `wait_exponential(min=2, max=30)` — суммарно до ~45 сек блокировки
- **Файл**: `bot/clients/base.py:95-97`
- **Проблема**: 3 попытки × экспоненциальный backoff: 2 + 4 + 8 = 14 сек ожидания + сам timeout (30 сек) × 3 = до 104 сек. Telegram callback имеет timeout 15 сек — callback умрёт задолго до ответа.
- **Риск**: Средний (UX).
- **Решение**: Уменьшить до `stop_after_attempt(2)` + `wait_exponential(max=5)`.
- **Статус**: [ ] Не исправлено

### BUG-20: `dp.start_polling(drop_pending_updates=True)` — при рестарте теряются активные операции
- **Файл**: `bot/main.py:198-202`
- **Проблема**: `drop_pending_updates=True` — все сообщения, пришедшие во время простоя, теряются. Это может быть OK, но пользователи, нажавшие «Скачать лучшее» во время рестарта, увидят тишину.
- **Риск**: Низкий.
- **Решение**: Опциональный флаг в settings.
- **Статус**: [ ] Не исправлено

### BUG-21: `Optional[str]` в callback_data — `callback.data` может быть None, не все хендлеры проверяют
- **Файл**: различные callback handlers
- **Проблема**: `F.data.startswith(...)` гарантирует, что data не None при матчинге, но для `F.data == "cal_7"` — тоже. В `handle_trending_series_item` (`trending.py:232`) — есть проверка `if not callback.data:` — OK. Но, например, в `handle_pagination` (search.py:280) нет явной проверки `callback.data is not None` до `.replace(...)`.
- **Риск**: Низкий.
- **Решение**: Добавить единый guard в middleware.
- **Статус**: [ ] Не исправлено

### BUG-22: `session_data[:200]` в логе — обрезает utf-8 посреди мультибайтного символа → UnicodeDecodeError при JSON-сериализации лога
- **Файл**: `bot/db.py:284`
- **Проблема**: `row_data[:200]` — это срез строки, а не bytes; обрезание мультибайтного символа в строке технически не происходит (Python 3 str — unicode), но если лог выводит JSON и использует surrogate strings, может упасть. Маловероятно, но fragile.
- **Риск**: Низкий.
- **Статус**: [ ] Не исправлено

### BUG-23: `conftest.py` — monkeypatch.setenv вызывается в фикстуре `mock_env`, но `get_settings` кеширован через `@lru_cache` — в тестах settings всё равно будут из первого вызова
- **Файл**: `tests/conftest.py:13-25`, `bot/config.py:135`
- **Проблема**: `get_settings` имеет `@lru_cache`, поэтому после первого вызова env-переменные игнорируются. В текущих тестах `mock_env` используется мало, но при расширении тестов будет проблема.
- **Риск**: Низкий.
- **Решение**: Вызывать `get_settings.cache_clear()` в фикстуре.
- **Статус**: [ ] Не исправлено

### BUG-24: Handler `@router.message(F.text & ~F.text.startswith("/") & ~F.text.in_(MENU_BUTTONS))` — emoji "📅 Календарь" не включен в MENU_BUTTONS
- **Файл**: `bot/handlers/search.py:35-39`
- **Проблема**: MENU_BUTTONS содержит "📅 Календарь" — OK, проверено. А "📺 Emby"? — есть. А "🔥 Топ"? — есть. OK, набор полон, баг снимается.
- **Статус**: OK (фальшивое срабатывание)

### BUG-25: `Settings.telegram_bot_token: str = Field(..., min_length=1)` — валидирует min_length, но токен Telegram имеет формат `<id>:<hash>` длиной ~46 символов
- **Файл**: `bot/config.py:21`
- **Проблема**: `min_length=1` позволит запустить бот с фейковым токеном, который упадёт только при первом обращении к API.
- **Риск**: Низкий.
- **Решение**: Добавить `pattern=r"^\d+:[A-Za-z0-9_-]{30,}$"`.
- **Статус**: [ ] Не исправлено

### BUG-26: `SearchResult.get_size_gb()` делит на `1024**3`, но size может быть None
- **Файл**: `bot/models.py:91-93`
- **Проблема**: `size: int = Field(default=0)` — не None. OK. Но `seeders: Optional[int]` — может быть None, и в scoring.py:197 проверяется корректно. Ложное срабатывание.
- **Статус**: OK

### BUG-27: `_should_monitor_season` возвращает True в `missing`/`existing`/`future` — что не соответствует Sonarr-семантике
- **Файл**: `bot/clients/sonarr.py:147-165`
- **Проблема**: Для monitor_type="missing" метод возвращает True для всех сезонов. Но Sonarr ожидает, что seasons.monitored отражает какие эпизоды монитор**нас** интересуют. Для "future" — только непоявившиеся. Комментарий «Future episodes will be monitored automatically» плюс return True/False в зависимости от типа — не соответствует Sonarr API поведению addOptions.monitor.
- **Риск**: Средний (неверное добавление сериалов).
- **Решение**: Для "future" ставить False на всех существующих seasons, True только на несмотренные. Либо оставить решение Sonarr и не передавать seasons.
- **Статус**: [ ] Не исправлено

### BUG-28: `Keyboards.trending_series(series[:10])` ожидает `series.tmdb_id`, но для TMDb trending `tvdb_id=0` — callback несёт TMDb ID, handler `handle_series_from_trending` читает его как TMDb ID, OK. Но `handle_add_series_from_trending` тоже читает TMDb ID, и ищет через `_trending_series_cache.get(tmdb_id)` — если кеш истёк, вернёт ошибку «Попробуйте обновить список»
- **Файл**: `bot/handlers/trending.py:379-388`
- **Проблема**: Нет fallback на Sonarr lookup по названию — только сообщение об ошибке. Для движка детализации (handle_series_from_trending) тоже нет fallback.
- **Риск**: Средний (UX после рестарта или через 200 записей).
- **Решение**: Добавить fallback через Sonarr lookup по `series.title`.
- **Статус**: [ ] Не исправлено

## Низкие

### BUG-29: `_is_season_pack` — `Show.Season.1.Complete` и `Show.Complete.Season.1` — тест говорит оба True, но код смотрит только на order "complete season"/"season pack"/"full season". Работает за счёт `season[\s.]*\d` ветки.
- **Файл**: `bot/clients/prowlarr.py:347-367`
- **Проблема**: Зависит от порядка слов, не robust.
- **Статус**: [ ] Минорно

### BUG-30: `format_torrent_action("resume", ...)` возвращает русское «Возобновлён», а тест проверяет «Возобновлен» (без ё)
- **Файл**: `tests/test_qbittorrent.py:365`, `bot/ui/formatters.py:597`
- **Проблема**: Проверка через `or "▶️"` — тест всё равно проходит за счёт эмодзи. Но в коде ё, в тесте е.
- **Статус**: [ ] Несоответствие символов

### BUG-31: `_extract_year` паттерн `r"\s(\d{4})\s"` не найдёт год в начале/конце
- **Файл**: `bot/clients/prowlarr.py:305-310`
- **Проблема**: `"2024 Movie"` не распарсится паттерном `\s(\d{4})\s`. Есть fallback `r"[\.\s](\d{4})$"`, но не начало.
- **Статус**: [ ] Низко

### BUG-32: `DefaultBotProperties(parse_mode=ParseMode.HTML)` — не все сообщения HTML-safe
- **Файл**: `bot/main.py:138`
- **Проблема**: По-умолчанию все `answer()` вызовы отправляют с HTML-parse_mode. Если в ответе есть `<` из пользовательского ввода — 400. Частично уже есть `html.escape`, но не везде.
- **Статус**: [ ] См. SEC-07

### BUG-33: `datetime.fromtimestamp(item["added_on"], tz=timezone.utc)` — qBittorrent отдаёт unix timestamp local time, не UTC
- **Файл**: `bot/clients/qbittorrent.py:431,435`
- **Проблема**: По qBittorrent docs timestamps в UTC — OK, но в некоторых версиях localtime. Фактически UTC.
- **Статус**: [ ] Возможно ложное

### BUG-34: `trailing slash` обрабатывается для prowlarr/radarr/sonarr/qbittorrent/emby, но не для `tmdb_proxy_url`
- **Файл**: `bot/config.py:97-109`, клиент tmdb.py:33-38
- **Проблема**: `proxy=self._proxy_url` передаётся в httpx, httpx сам парсит URL. trailing slash не опасен для proxy, но несогласованно.
- **Статус**: [ ] Минорно

### BUG-35: `drop_pending_updates=True` стирает также команды `/start` от новых пользователей в очереди
- **Файл**: `bot/main.py:201`
- **Статус**: [ ] См. BUG-20

### BUG-36: `handle_cancel` удаляет сессию, но не отменяет выполняющийся fetch/grab
- **Файл**: `bot/handlers/search.py:684-694`
- **Проблема**: Нет asyncio.Task cancellation.
- **Статус**: [ ] Низко (UX)

## Итоговый подсчёт
- Критические: 5 (BUG-01..05)
- Высокие: 13 (BUG-06..18)
- Средние: 10 (BUG-19..28)
- Низкие: 8 (BUG-29..36)
