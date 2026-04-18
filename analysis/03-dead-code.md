# Анализ dead code TG_arr

## Критические

### DEAD-01: Устаревшие документы BUGFIX_REPORT.md и IMPROVEMENTS.md в корне
- **Файл**: `BUGFIX_REPORT.md` (8 KB), `IMPROVEMENTS.md` (10 KB)
- **Проблема**: Оба документа упоминают «решено», «добавлено» — это исторические отчёты из первых аудитов. Актуальные замечания перенесены в MEMORY.md пользователя. В репозитории они служат только «мусорным» источником (и отвлекают при поиске).
- **Риск**: Low (qualidade).
- **Решение**: Удалить оба файла, переместить в `docs/history/` или в ветку `archive`.
- **Статус**: [ ] Не исправлено

### DEAD-02: Устаревшие docs: `docs/FEATURE_QBITTORRENT.md` (35 KB), `docs/QUALITY_REPORT.md` (18 KB)
- **Файл**: `docs/FEATURE_QBITTORRENT.md`, `docs/QUALITY_REPORT.md`
- **Проблема**: FEATURE_QBITTORRENT.md — это план/спека на фичу, которая уже реализована (и многое отличается: там метод `_make_progress_bar`, в коде — `_progress_bar`). QUALITY_REPORT.md — план сейчас устарел.
- **Риск**: Low.
- **Решение**: Удалить или пометить как «historical design docs».
- **Статус**: [ ] Не исправлено

### DEAD-03: Build-артефакты `.coverage`, `.ruff_cache/`, `.pytest_cache/` в рабочей копии
- **Файл**: корень
- **Проблема**: В `.gitignore` указаны, но сами файлы/директории присутствуют локально (не закоммичены, но попадают в docker build context). В `.dockerignore` `.pytest_cache`, `.mypy_cache`, `.ruff_cache` указаны, но `.coverage` — нет.
- **Риск**: Low (размер образа).
- **Решение**: Добавить `.coverage` в `.dockerignore`; `make clean` чистит — рекомендовать в CI.
- **Статус**: [ ] Не исправлено

## Высокие

### DEAD-04: `EmbyClient.get_scheduled_tasks()` не используется нигде
- **Файл**: `bot/clients/emby.py:192-195`
- **Проблема**: Метод определён, но ни один handler/service его не вызывает.
- **Решение**: Удалить либо добавить handler (например, `/emby_tasks`).
- **Статус**: [ ] Не исправлено

### DEAD-05: `NotificationService.force_check()` и `NotificationService.get_stats()` — не используются
- **Файл**: `bot/services/notification_service.py:200-236, 238-248`
- **Проблема**: Обе функции существуют и тестируются (test_qbittorrent.py), но не вызываются в продакшн-коде. Нет хендлера `/notify_check` или `/notify_stats`.
- **Решение**: Либо добавить админ-команду для force_check/stats, либо удалить.
- **Статус**: [ ] Не исправлено

### DEAD-06: `NotificationService.unsubscribe_user()` — не вызывается
- **Файл**: `bot/services/notification_service.py:52-55`
- **Проблема**: Пользователи подписываются автоматически при старте (`main.py:71-72`), отписаться нельзя. Метод недостижим.
- **Решение**: Добавить callback_data `notify:off` + settings-toggle, либо удалить.
- **Статус**: [ ] Не исправлено

### DEAD-07: `Keyboards.series_list()` не используется
- **Файл**: `bot/ui/keyboards.py:228-274`
- **Проблема**: Метод есть, никто не вызывает (в search handler lookup_series возвращает list, но используется первый).
- **Решение**: Либо использовать при нескольких совпадениях, либо удалить.
- **Статус**: [ ] Не исправлено

### DEAD-08: `Keyboards.confirm_delete_torrent()` — не используется в боевом коде
- **Файл**: `bot/ui/keyboards.py:605-625`
- **Проблема**: Метод есть и тестируется, но delete-кнопки в `torrent_details` идут напрямую на t_delete: без confirmation. Callback `t_delete:confirm:<hash>` нигде не обрабатывается.
- **Решение**: Добавить confirmation flow либо удалить.
- **Статус**: [ ] Не исправлено

### DEAD-09: `TorrentState.COMPLETED` ставится только в `_parse_torrent` при progress>=1 — в `STATE_MAP` нет маппинга на «completed» (qBittorrent не отдаёт такого state)
- **Файл**: `bot/clients/qbittorrent.py:22-43`
- **Проблема**: Это не мёртвый код, но `STATE_MAP` не содержит «completed» — значит единственный путь установки — ручной override в `_parse_torrent:425`. OK, не баг.
- **Статус**: Ложное срабатывание

### DEAD-10: `EmbyLibrary.item_count` всегда 0 — комментарий объясняет, но поле бесполезно
- **Файл**: `bot/clients/emby.py:33, 168`
- **Проблема**: Поле всегда 0. Используется ли где? Проверка показала — не используется в Formatters.
- **Решение**: Удалить поле.
- **Статус**: [ ] Не исправлено

### DEAD-11: Импорт `MovieInfo, SeriesInfo` в handlers/search.py — `SeriesInfo` используется только в isinstance
- **Файл**: `bot/handlers/search.py:11-19`
- **Проблема**: `SeriesInfo` импортирован, используется в `isinstance(series, SeriesInfo)`. OK.
- **Статус**: Ложное

### DEAD-12: `from typing import Any` в `bot/handlers/trending.py:9` — используется только для type hint dict
- **Файл**: `bot/handlers/trending.py:9`
- **Проблема**: `_trending_movies_cache: dict[int, Any]`. OK, используется.
- **Статус**: Ложное

### DEAD-13: Неиспользуемый импорт `format_speed` в `bot/handlers/downloads.py:11`
- **Файл**: `bot/handlers/downloads.py:11,599`
- **Проблема**: `format_speed` используется в `handle_speed_menu` (строки 599-600). OK.
- **Статус**: Ложное

### DEAD-14: `MovieInfo`, `RootFolder`, `QualityProfile`, `SearchResult` импортированы в `add_service.py`, но `RootFolder` и `SearchResult` тоже — используются (как аргументы). OK.
- **Файл**: `bot/services/add_service.py:15-25`
- **Статус**: Ложное

## Средние

### DEAD-15: `SystemStatus` модель — `error`, `version`, `response_time_ms` могут быть None, есть использование
- **Статус**: Все используются

### DEAD-16: `CallbackData.MOVIE = "movie:"` и `CallbackData.SERIES = "series:"` — не используются
- **Файл**: `bot/ui/keyboards.py:35-36`
- **Проблема**: Константы определены, но ни один handler не матчит `F.data.startswith(CallbackData.MOVIE)`. Вероятно остатки от предыдущего дизайна (сейчас используются `TRENDING_MOVIE`, `ADD_MOVIE`).
- **Решение**: Удалить.
- **Статус**: [ ] Не исправлено

### DEAD-17: `CallbackData.SPEED_LIMIT = "speed:"` — используется в handle_speed_set, но handler матчит `F.data.startswith("speed:")` вручную, не через константу
- **Файл**: `bot/handlers/downloads.py:626`, `bot/ui/keyboards.py:66`
- **Проблема**: `@router.callback_query(F.data.startswith("speed:"))` — literal, не `CallbackData.SPEED_LIMIT`. Константа используется только при генерации в keyboards.py. Минорная несогласованность.
- **Решение**: Использовать `F.data.startswith(CallbackData.SPEED_LIMIT)`.
- **Статус**: [ ] Не исправлено

### DEAD-18: Закомментированный код в `.env.example` (строки 27-28, 32, 38, 55, 58)
- **Файл**: `.env.example`
- **Проблема**: `# QBITTORRENT_TIMEOUT=30.0` и пр. — документирующие, но избыточные.
- **Решение**: Оставить либо документировать в README.
- **Статус**: Не проблема

### DEAD-19: `MovieInfo.content_model_type: Literal["movie"] = "movie"` — вспомогательное поле для Pydantic discriminator
- **Файл**: `bot/models.py:101, 126`
- **Проблема**: Не мёртвый код, но увеличивает размер JSON в БД.
- **Статус**: OK

### DEAD-20: `_e(text)` в формате — обёртка над html.escape с проверкой на truthy
- **Файл**: `bot/ui/formatters.py:26-30`
- **Проблема**: Используется, нормально.
- **Статус**: OK

### DEAD-21: Variables `hours`/`minutes` unused в `bot/models.py:333` после days overflow
- **Файл**: `bot/models.py:327-335`
- **Проблема**: При `hours > 24` переопределяется hours, но `minutes, seconds = divmod(remainder, 60)` выше — `minutes`/`seconds` посчитаны, но в возврате `{days}d {hours}h` игнорируются.
- **Решение**: См. BUG-13. Dead вычисление.
- **Статус**: [ ] Не исправлено

### DEAD-22: `_trending_movies_cache`, `_trending_series_cache` — module-level dict'ы с global rebinding
- **Файл**: `bot/handlers/trending.py:27-28`
- **Проблема**: Переопределение `global _trending_movies_cache = {}` при превышении — теряется локальное обращение к старому dict в других handlers.
- **Решение**: Использовать `.clear()` вместо `= {}`.
- **Статус**: [ ] Не исправлено (см. BUG-02)

## Низкие

### DEAD-23: `data/.gitkeep` в Dockerfile `COPY data/.gitkeep ./data/`
- **Файл**: `Dockerfile:16`
- **Проблема**: Копируется единственный файл `data/.gitkeep` — нужно для существования директории. Можно заменить на `RUN mkdir -p /app/data`.
- **Статус**: Минорно

### DEAD-24: `asyncio_default_fixture_loop_scope = "function"` в pyproject.toml
- **Файл**: `pyproject.toml:36`
- **Проблема**: Настройка нужна для pytest-asyncio 1.x. OK.
- **Статус**: OK

### DEAD-25: `version: "3.9"` в docker-compose.override.yml — depricated с compose v2
- **Файл**: `docker-compose.override.yml:5`
- **Проблема**: Compose v2 выдаёт warning. Устарело.
- **Решение**: Удалить строку.
- **Статус**: [ ] Не исправлено

### DEAD-26: Makefile target `install` vs `dev` — оба делают `pip install -r requirements.txt`, разница только в ruff+mypy
- **Файл**: `Makefile:21-27`
- **Проблема**: Достаточно одного, с опциональным `dev` через `pip install -e ".[dev]"`.
- **Статус**: [ ] Не исправлено

### DEAD-27: Dockerfile `RUN apt-get install -y ... gcc` — gcc нужен для компиляции native-модулей, но среди зависимостей (requirements.txt) их нет (orjson, aiosqlite, pydantic — все wheel). Gcc лишний, увеличивает образ.
- **Файл**: `Dockerfile:6-8`
- **Статус**: [ ] Не исправлено

### DEAD-28: Пустой `bot/ui/__init__.py` (1 строка комментарий)
- **Файл**: `bot/ui/__init__.py`
- **Проблема**: `"""UI components for Telegram bot."""` — единственная строка. Это нормально для пакета.
- **Статус**: OK

## Итоговый подсчёт
- Критические: 3 (DEAD-01..03 — устаревшие отчёты и артефакты)
- Высокие: 6 (DEAD-04..08, DEAD-10)
- Средние: 5 (DEAD-16..22)
- Низкие: 4 (DEAD-25..27)
