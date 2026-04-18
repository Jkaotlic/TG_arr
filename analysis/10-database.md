# Database Audit — TG_arr (SQLite / aiosqlite)

Дата: 2026-04-18.

## DB-01 — Нет миграций (HIGH)

Файл: `bot/db.py:67-126`
Используется только `CREATE TABLE IF NOT EXISTS`. При изменении схемы:
- Новая колонка → `IF NOT EXISTS` не добавит её к существующей таблице, код упадёт при `INSERT`.
- Удалённая колонка в модели → SELECT вернёт её, но pydantic проигнорирует.
- Переименование → runtime break.

Пример вектора: добавили `lidarr_quality_profile_id`/`lidarr_metadata_profile_id`/`lidarr_root_folder_id` в `UserPreferences` (models.py:239). Но `users.preferences` — TEXT поле JSON, поэтому схема БД не страдает. **Спас JSON-storage для preferences.**

Однако для других таблиц (`actions`, `searches`, `sessions`), если в будущем добавится колонка (например `request_id` в OBS-07), потребуется миграция.

**Решение:** minimal migration framework:
- `user_version` PRAGMA
- `_create_tables` проверяет `PRAGMA user_version` и применяет ALTER TABLE'ы
- Либо **Alembic** / **yoyo-migrations** (overkill для SQLite)

## DB-02 — SQLite без WAL mode (HIGH)

Файл: `bot/db.py:46`
```python
self._connection = await aiosqlite.connect(self.db_path, isolation_level=None)
```
Default journal mode — DELETE. При одновременном чтении и записи читатель блокируется. Для single-connection это не критично, **но**:
- `NotificationService` в background task делает writes (не напрямую, но save_session / log_action могут)
- Handler'ы делают writes параллельно (конкурентные callback'и)

Хотя в коде один `_connection`, aiosqlite сериализует операции через одну сокет-очередь → фактически single-writer.

**Но** WAL даст ускорение + повышенную durability.

**Решение:** после connect — `await self.conn.execute("PRAGMA journal_mode=WAL")`; также `PRAGMA synchronous=NORMAL` (умолчание FULL слишком медленно на SD-карте).

## DB-03 — Индексы на `action_log.user_id`, `sessions.user_id` — есть (OK)

Файл: `bot/db.py:119-124`
```sql
CREATE INDEX IF NOT EXISTS idx_searches_user ON searches(user_id);
CREATE INDEX IF NOT EXISTS idx_searches_created ON searches(created_at);
CREATE INDEX IF NOT EXISTS idx_search_results_search ON search_results(search_id);
CREATE INDEX IF NOT EXISTS idx_actions_user ON actions(user_id);
CREATE INDEX IF NOT EXISTS idx_actions_created ON actions(created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at);
```
Отлично. `sessions.user_id` — PRIMARY KEY, индекс есть. `actions.user_id` — индекс есть. `searches.user_id` — индекс есть. 👍

## DB-04 — FK в `actions.user_id → users.tg_id` присутствует, но не enforced (MED)

Файл: `bot/db.py:108`
```sql
FOREIGN KEY (user_id) REFERENCES users(tg_id)
```
SQLite по умолчанию не enforces FK. Нужно `PRAGMA foreign_keys = ON` после connect.

**Решение:** после connect — `await self.conn.execute("PRAGMA foreign_keys=ON")`.

## DB-05 — `isolation_level=None` (autocommit) (MED)

Файл: `bot/db.py:46`
aiosqlite autocommit mode. Это означает каждый `execute()` немедленно коммитится (кроме если есть явный `BEGIN`/`COMMIT`). Код использует `BEGIN` + `commit()/rollback()` вручную в `save_search` и `cleanup_old_searches`.

Проблема: `isolation_level=None` отключает sqlite3's автоматическую транзакционность. Каждый `execute` = отдельная транзакция на дисковом уровне.

На prod на SD-карте это **много** fsync'ов → медленно.

**Решение:** вернуть `isolation_level=""` (default, deferred) или использовать context-manager `async with conn.executescript(...)` для пакетных операций.

## DB-06 — Сессии не имеют TTL-index для автоочистки (LOW)

Файл: `bot/db.py:377-385`
`cleanup_old_sessions(hours=24)` вызывается только при startup. Если бот работает >24 часа без рестарта, сессии продолжают копиться. Cron'а в бот нет.

**Решение:** периодический asyncio-task `cleanup_loop` раз в час.

## DB-07 — `search_results.search_id` FK есть, но cascade delete нет (MED)

Файл: `bot/db.py:93`
```sql
FOREIGN KEY (search_id) REFERENCES searches(id)
```
Нет `ON DELETE CASCADE`. В `cleanup_old_searches` вручную удаляются результаты сначала. Если кто-то забудет, orphan'ы.

**Решение:** `ON DELETE CASCADE` + `PRAGMA foreign_keys=ON` (DB-04).

## DB-08 (НОВЫЙ) — Нет `unique(tg_id, ...)` где нужно (LOW)

`sessions` — PK user_id, **но** user может иметь только одну активную сессию. Если перезапустить `/search`, она перезаписывается (ON CONFLICT → UPDATE). ОК.

`searches` — PK id (AUTOINCREMENT). Можно искать дубликаты одного query от одного user'а, но это фича (история). OK.

## DB-09 (НОВЫЙ) — `created_at`/`updated_at` хранятся как TEXT (ISO string) (LOW)

SQLite не имеет native datetime. TEXT ok, но сортировка работает только при ISO-format (не local format). Код использует `datetime.now(timezone.utc).isoformat()` — ok.

Возможная проблема: сохранение fromisoformat в pydantic datetime field (в models.py) возвращает tz-aware — корректно.

## DB-10 (НОВЫЙ) — Нет VACUUM в shutdown (LOW)

После cleanup старых строк SQLite файл не уменьшается. Не критично, но при нехватке места полезно `VACUUM`.

## DB-11 (НОВЫЙ) — `save_search` сохраняет `results_json` как monolith (MED)

Файл: `bot/db.py:208-215`
`json.dumps([r.model_dump(mode="json") for r in results])` → для 500 результатов это ~500KB JSON в одной строке. `get_search_results` читает всё, парсит всё. Для history-view не нужно — достаточно метаданных.

**Решение:** либо нормализовать в `search_results` (row per result), либо хранить только top-20.

## DB-12 (НОВЫЙ) — Нет database backup strategy (INFO)

Docker volume `bot-data` хранит bot.db. Нет backup → при отказе SD-card потеря истории + preferences.

**Решение:** cron в host'е копирует `data/bot.db` раз в день; либо добавить admin-команду `/backup` которая отправляет файл.

## DB-13 (НОВЫЙ) — `row_factory = aiosqlite.Row` — ok (OK)

Файл: `bot/db.py:47`. Позволяет доступ по имени колонки. Хорошо.

## Итого

HIGH: DB-01, DB-02
MED: DB-04, DB-05, DB-07, DB-11
LOW: DB-06, DB-08, DB-09, DB-10, DB-13 (OK)
INFO: DB-12

Приоритет:
1. DB-02 — WAL mode (одна строка, big win на SD-card)
2. DB-04 — `PRAGMA foreign_keys=ON`
3. DB-01 — minimal migration via `PRAGMA user_version`
4. DB-11 — trim saved search results
