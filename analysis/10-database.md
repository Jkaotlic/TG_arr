# Database TG_arr v1.0 (раунд 3)

Дата: 2026-05-08.
Файл: `bot/db.py` (510 строк, ~16 KB), aiosqlite 0.22.1, single-file SQLite в `/app/data/bot.db`.

Контекст: жалоба юзера «бот плохо ищет». Ниже разбираю, является ли БД причиной.

Прошлый отчёт (`analysis_round2/10-database.md`): из 12 находок зафиксировано: DB-01 (миграции через `PRAGMA user_version`), DB-02 (`journal_mode=WAL`), DB-04 (`foreign_keys=ON`), DB-05 (частично — `synchronous=NORMAL` стоит, но `isolation_level=None` остался), DB-11/DB-06/DB-07/DB-10/DB-12 — НЕ исправлены.

---

## Связь с жалобой «бот плохо ищет»

**Краткий вывод:** БД **не основная причина** медленного поиска.

`process_search` (handlers/search.py:170-265) делает только: `save_search` → `save_session` → `log_action`. Это 3 коротких INSERT'а уже **после** долгого `search_releases()` (Prowlarr HTTP, ~секунды). Объём — десятки KB JSON, никаких JOIN.

Однако три точки в БД могут давать **видимую задержку** на rpie4 SD-card:
1. **DB-14 (новый, MED)** — `save_session` со 100-500 SearchResult → большой JSON в одном UPSERT, при `synchronous=NORMAL+WAL` всё равно sync на каждый commit.
2. **DB-15 (новый, MED)** — каждое нажатие пагинации/выбора релиза делает `get_session` + `save_session` (две serial round-trip к одному `_connection`). Их 4-7 на user-flow.
3. **DB-16 (новый, MED)** — нет `busy_timeout` PRAGMA. На SD-card при WAL checkpoint можно поймать `SQLITE_BUSY` → исключение всплывёт в handler как «поиск временно недоступен».

Реальный bottleneck поиска — Prowlarr / scoring / TMDb (см. `analysis_round2/06-performance.md`). Но если SD-card деградирует, БД станет co-cause.

---

## Критические

(нет)

---

## Высокие

### DB-13: Отсутствует `PRAGMA busy_timeout` (HIGH на rpie4)
- **Файл**: `bot/db.py:46-52`
- **Проблема**: после `aiosqlite.connect(..., isolation_level=None)` ставятся только `journal_mode=WAL`, `foreign_keys=ON`, `synchronous=NORMAL`. **`busy_timeout` не задан** → дефолт 0 ms. На SD-карте rpie4 при WAL-checkpoint или конкурентном чтении (тесты, бэкап-скрипт через `sqlite3` CLI) любой write может вылететь с `OperationalError: database is locked`. Эта ошибка всплывёт в handler'ы как generic «поиск временно недоступен» (search.py:267-269) — пользователь увидит таймаут.
- **Решение**: после строки 52 добавить `await self._connection.execute("PRAGMA busy_timeout=5000")` (5 секунд). Дёшево, идемпотентно, ноль рисков.
- **Статус**: [ ]

### DB-14: `save_session` UPSERT-ит JSON 100-500 SearchResult'ов на каждый клик
- **Файл**: `bot/db.py:321-347`, вызовы — `bot/handlers/search.py:189,221,302,346,413,434,453,495,700` (10 точек!) и `music.py:142,191`.
- **Проблема**: на 500 результатов `session.model_dump_json()` ~300-700 KB; UPSERT на rpie4 SD-card может занять 50-150 ms. После `search_releases` сохранение делается **единожды** (211→221) — это норма. Но **handle_pagination** (324-346) делает `get_session` + `save_session` на каждый клик пагинации, чтобы записать только `current_page`. Для пользователя это +50-150 ms latency на каждой странице. **handle_release_selection** делает 2-3 `save_session` подряд (412, 434, 453).
- **Подтверждение**: BUG-14 уже трункейтит до 500 (строка 328-329). Но 500 — всё равно много.
- **Решение**:
  1. Хранить `current_page` в callback_data (`page:N`), а не в session — pagination станет read-only. (Большой рефакторинг.)
  2. Минимально: разбить `sessions.session_data` на `session_meta` (query, content_type, current_page, selected) и `session_results_json` — UPDATE только meta при пагинации.
  3. Альтернатива: вернуть `isolation_level=""` (deferred) и батчить save в одну транзакцию, либо асинхронно `asyncio.create_task(db.save_session(...))` (fire-and-forget) после обновления UI — terminal handlers (grab) уже завершают операцию.
- **Статус**: [ ]

### DB-15: `cleanup_old_sessions` запускается только при startup (HIGH перенесено из DB-06)
- **Файл**: `bot/main.py:104,109`, `bot/db.py:476-509`
- **Проблема**: `on_startup` вызывает `cleanup_old_sessions(hours=24)` и `cleanup_old_searches(days=7)` единственный раз. В долгоживущем процессе (бот не рестартится неделями) `sessions` и `searches` копятся бесконечно. Жалоба round2 (DB-06) не закрыта.
- **Анализ роста**: 1 пользователь × 5 поисков/день × 200 KB JSON = 1 MB/день мёртвых сессий. На 5 пользователей × 30 дней = 150 MB. На SD-card — ощутимо.
- **Решение**: запустить периодический taskasyncio в `on_startup`:
  ```python
  async def cleanup_loop():
      while True:
          await asyncio.sleep(3600)
          await db.cleanup_old_sessions(hours=24)
          await db.cleanup_old_searches(days=7)
  asyncio.create_task(cleanup_loop())
  ```
  Storing handle для cancel в `on_shutdown`.
- **Статус**: [ ]

---

## Средние

### DB-16: `isolation_level=None` (autocommit) делает каждый execute() отдельной транзакцией
- **Файл**: `bot/db.py:46`
- **Проблема**: aiosqlite passes это в sqlite3 — отключается auto-BEGIN. Каждый `execute(INSERT)` → один fsync (даже при `synchronous=NORMAL` — fsync в WAL). На SD-card fsync = 5-30 ms. `log_action` (один INSERT + commit) = 1 fsync = 10-30 ms. Plus `save_session` = ещё 1. На каждый клик = 20-60 ms скрытой latency.
- **Текущие явные `BEGIN`/`COMMIT`** в `save_search` (278-307) и `cleanup_old_searches` (490-508) работают, потому что autocommit прерывается явным `BEGIN` — это OK. Но 90% операций (`log_action`, `save_session`, `update_user_preferences`, `delete_session`) — single-statement autocommit.
- **Решение**: убрать `isolation_level=None` (вернуть `""` deferred). aiosqlite будет авто-обёртывать INSERT/UPDATE в транзакцию и коммитить при `await conn.commit()` явном вызове — что уже есть в коде. Это **меняет поведение** `save_search`/`cleanup_old_searches` — там стоит `BEGIN` явно, может стать ошибкой «cannot start a transaction within a transaction». Нужно прогнать тесты.
- **Альтернатива (безопаснее)**: оставить как есть, но засунуть в `save_session` пакетный `BEGIN`...`COMMIT` — будет один fsync вместо одного на каждое нажатие.
- **Статус**: [ ]

### DB-17: `search_results.search_id` FK без `ON DELETE CASCADE`
- **Файл**: `bot/db.py:99` (`FOREIGN KEY (search_id) REFERENCES searches(id)`)
- **Проблема**: DB-07 из раунда 2 не закрыто. С учётом включённого `PRAGMA foreign_keys=ON` (DB-04 fixed) **DELETE на parent теперь ВЫЛЕТИТ с `FOREIGN KEY constraint failed`** если есть child. `cleanup_old_searches` (490-508) обходит это вручную (сначала чистит `search_results`, потом `searches`) — работает, но хрупко: любой будущий `DELETE FROM searches` забыв про child упадёт.
- **Решение**: пересоздать FK с `ON DELETE CASCADE`. Через миграцию v3:
  ```sql
  -- SQLite не умеет ALTER FK; нужен new table + copy + drop + rename
  ```
  Либо проще: оставить ручной cleanup, но **тестом** зафиксировать инвариант.
- **Статус**: [ ]

### DB-18: `actions.user_id` FK без `ON DELETE CASCADE/SET NULL`
- **Файл**: `bot/db.py:115`
- **Проблема**: при удалении user'а (которого, впрочем, нет в коде) FK свалится. Нет admin-команды remove user — пока теоретическая.
- **Решение**: при добавлении remove-user-команды — `ON DELETE CASCADE` для actions/searches/sessions; либо `ON DELETE SET NULL` (но user_id NOT NULL).
- **Статус**: [ ]

### DB-19: `search_results` хранит monolithic JSON — `get_search_results` парсит всё
- **Файл**: `bot/db.py:294,309-318`
- **Проблема**: DB-11 из раунда 2 не закрыто. `results_json` ~500 KB на 500 результатов. **`get_search_results` нигде не используется в handlers'ах** (Grep подтверждает: только `tests/test_db.py:120`). Эта таблица — write-only (история, никем не читается). 7 дней × N поисков × 500 KB = десятки MB мёртвого пространства.
- **Решение**:
  1. Если history-feature не реализуется — убрать INSERT в `search_results` из `save_search` (хранить только metadata в `searches`).
  2. Если планируется `/history` со снапшотом результатов — лимитировать `results[:20]` перед сериализацией.
- **Статус**: [ ]

### DB-20: Нет `PRAGMA wal_autocheckpoint` тюнинга и `wal_checkpoint(TRUNCATE)` при shutdown
- **Файл**: `bot/db.py:54-56`, `bot/db.py:58-63`
- **Проблема**: WAL включён (DB-02 fixed), но дефолтный autocheckpoint = 1000 страниц (~4 MB). На rpie4 SD-card неконтролируемые checkpoint'ы во время user-action создают latency-spike. При shutdown `close()` не делает `wal_checkpoint(TRUNCATE)` — `bot.db-wal` файл может остаться в десятки MB.
- **Решение**:
  - В `connect()`: `PRAGMA wal_autocheckpoint=200` (~800 KB чаще, но короче).
  - В `close()` перед `await self._connection.close()`: `await self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")`.
- **Статус**: [ ]

---

## Низкие

### DB-21: Нет database backup стратегии (DB-12 не закрыто)
- **Файл**: контекст — Docker volume `bot-data`, `compose.yml`
- **Проблема**: при отказе SD-card теряются `users.preferences`, `actions` (history). `searches`/`sessions` не критичны — voluntary. DB-21 = legacy DB-12.
- **Решение**:
  1. Cron на хосте: `sqlite3 bot.db ".backup /backup/bot-$(date +%F).db"` раз в день, ротация 7 дней.
  2. Либо admin-команда `/backup` отправляющая файл в Telegram (учесть лимит 50 MB Telegram bot upload).
- **Статус**: [ ]

### DB-22: Нет recovery при corruption SQLite файла
- **Файл**: `bot/db.py:35-56` (`connect`)
- **Проблема**: если `bot.db` corrupt (unclean shutdown, SD-bitrot), `aiosqlite.connect` либо упадёт, либо `_create_tables` упадёт на `executescript`. Бот не стартует, нет fallback. Также `PRAGMA integrity_check` не вызывается.
- **Решение**: в `connect()` после открытия добавить `PRAGMA integrity_check`; на ошибку — переименовать файл в `bot.db.corrupt-<ts>` и стартовать со свежей. Логировать `error` уровнем.
- **Статус**: [ ]

### DB-23: Нет VACUUM (DB-10 не закрыто)
- **Файл**: `bot/db.py:58-63`
- **Проблема**: после `cleanup_old_searches` файл не уменьшается. Не критично, но вредит, когда диск кончается.
- **Решение**: при shutdown — необязательно (VACUUM медленный). Сделать admin-команду `/vacuum`.
- **Статус**: [ ]

### DB-24: `INSERT INTO sessions` в `save_session` использует `ON CONFLICT(user_id) DO UPDATE` — корректно (OK)
- **Файл**: `bot/db.py:336-345`
- **Анализ**: правильный UPSERT, без race-window. PK на `user_id` гарантирует. OK.
- **Статус**: [x] OK

### DB-25: `created_at`/`updated_at` хранятся как ISO TEXT — UTC consistency OK
- **Файл**: `bot/db.py:210, 228, 276, 327, 391, 478, 488`
- **Анализ**: везде `datetime.now(timezone.utc).isoformat()`. Корректно. Сортировка ISO работает лексикографически. OK.
- **Статус**: [x] OK

### DB-26: SQL injection — все запросы parameterized (OK)
- **Файл**: весь `bot/db.py`
- **Анализ**: я просмотрел всё — **нет ни одного f-string в SQL с user input**. Только `_set_schema_version` использует f-string, но коэрсит через `int(version)` (строка 160-161) — безопасно. OK.
- **Статус**: [x] OK

### DB-27: Connection management — single persistent connection (OK)
- **Файл**: `bot/db.py:32-71`
- **Анализ**: `_connection` создаётся один раз на startup, используется до shutdown. `_connect_lock` защищает init. aiosqlite сериализует операции внутри одной connection через свою очередь. Threadsafe, no leak. OK.
- **Статус**: [x] OK

### DB-28: Migration framework — есть, идемпотентно (OK, DB-01 closed)
- **Файл**: `bot/db.py:135-195`
- **Анализ**: `PRAGMA user_version`, версия 2 текущая (`details` колонка в `actions`). Покрыто `tests/test_db.py:267-293`. OK. Минор: при добавлении нового файла v3 нужно добавить `if version < 3:` в `_run_migrations` — есть docstring-инструкция.
- **Статус**: [x] OK

### DB-29: `searches.user_id` индекс есть, но composite `(user_id, created_at)` отсутствует
- **Файл**: `bot/db.py:126-128`
- **Проблема**: для гипотетического `/history`-запроса по конкретному user'у с сортировкой по дате — текущие индексы вынуждают планировщик делать filter→sort. На малых данных не больно. Но `get_user_actions` (418-430) делает `WHERE user_id=? ORDER BY created_at DESC LIMIT 20` — работает на `idx_actions_user`, но затем sort. Composite `idx_actions_user_created` дал бы O(log n).
- **Решение**: `CREATE INDEX idx_actions_user_created ON actions(user_id, created_at DESC)`. На текущем объёме (десятки тысяч строк) выгода ничтожна.
- **Статус**: [ ]

### DB-30: row_factory = aiosqlite.Row (OK)
- Файл: `bot/db.py:47`. OK.
- **Статус**: [x] OK

### DB-31: SearchSession сериализация — OK с лимитом 500
- **Файл**: `bot/db.py:328-329`, `models.py:276`
- **Анализ**: `Field(default_factory=list, max_length=500)` + ручной trim — две защиты. OK. (См. DB-14 для perf-аспекта.)
- **Статус**: [x] OK

---

## Информационные

### DB-32: `PRAGMA temp_store=MEMORY` и `PRAGMA cache_size` не настроены
- **Файл**: `bot/db.py:50-52`
- **Проблема**: дефолт SQLite — temp_store=DEFAULT (file), cache_size=-2000 (~2MB). На rpie4 8 GB RAM можно расщедриться.
- **Решение**: `PRAGMA temp_store=MEMORY`, `PRAGMA cache_size=-20000` (20 MB). Микро-optim.
- **Статус**: [ ]

### DB-33: `PRAGMA mmap_size` не настроен
- **Проблема**: mmap может ускорить read, но на SD-card может быть нестабилен.
- **Решение**: оставить дефолт. Не трогать.
- **Статус**: [x] OK

---

## Итог

**HIGH (3):** DB-13 (busy_timeout), DB-14 (save_session JSON spam), DB-15 (cleanup_loop)
**MED (5):** DB-16 (autocommit fsync), DB-17 (CASCADE), DB-18 (CASCADE), DB-19 (monolithic JSON), DB-20 (WAL checkpoint)
**LOW (4):** DB-21 (backup), DB-22 (corruption recovery), DB-23 (VACUUM), DB-29 (composite index)
**INFO (1):** DB-32 (cache tuning)
**OK (5):** DB-24, DB-25, DB-26, DB-27, DB-28, DB-30, DB-31, DB-33

### Приоритет исправлений (топ-5)

1. **DB-13** — одна строка `PRAGMA busy_timeout=5000`. Big win on SD-card. Делай первым.
2. **DB-15** — periodic cleanup_loop. ~10 строк в `main.py:on_startup`.
3. **DB-14** — refactor `save_session` чтобы pagination не переписывала весь JSON. Самый ощутимый perf-win для пользователя.
4. **DB-20** — `wal_autocheckpoint=200` + `wal_checkpoint(TRUNCATE)` на close.
5. **DB-21** — admin `/backup` — 30 минут работы.

### Связь с жалобой «бот плохо ищет»

БД сама по себе **не делает поиск медленным** — реальный bottleneck в Prowlarr/scoring (см. round2/06-performance). Но **DB-14 + DB-16 вместе** добавляют ~100-300 ms видимой latency на каждый клик пагинации/выбора релиза, что ощущается как «тормозит». **DB-13** уберёт спорадические `database is locked` исключения, которые маскируются под «поиск временно недоступен».
