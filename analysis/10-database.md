# Анализ — База данных · TG_arr (раунд 4, 2026-06-30)

Подтверждено находок: **5** (critical=0, high=1, medium=1, low=3). Все прошли состязательную верификацию (CONFIRMED/PLAUSIBLE).

SQLite/aiosqlite, единое соединение, износ SD-карты.

## Высокие

### DB-01: Explicit BEGIN/commit transactions race on the shared autocommit connection across concurrent coroutines
- **Файл**: `bot/db.py:286`
- **Проблема**: The Database holds ONE shared aiosqlite connection opened with isolation_level=None (autocommit, line 46). save_search (lines 286-314) and cleanup_old_searches (lines 498-516) issue an explicit `await self.conn.execute("BEGIN")` and then `await` several more statements before `commit()`. aiogram's start_polling (main.py:317) processes updates concurrently AND _periodic_cleanup (main.py:305) runs as its own task, so multiple coroutines share this single connection. Concrete failure: user A's handler runs save_search and is suspended at the `await` after BEGIN; the event loop runs user B's handler which calls log_action -> `await self.conn.commit()` (line 423). That commit fires on the same connection and commits user A's still-incomplete transaction early (the search row is committed before/without its search_results row). Worse case: two BEGIN-using paths overlap (save_search during the periodic cleanup_old_searches) and the second `BEGIN` raises sqlite3.OperationalError: 'cannot start a transaction within a transaction', which bubbles up as a generic failure to the user. The rollback in the except block (lines 313, 515) can also roll back another coroutine's work. There is no application-level write lock serializing these multi-statement units.
- **Риск**: Premature commits causing partial/inconsistent writes, or OperationalError surfacing to users as a failed search/cleanup under concurrency.
- **Решение**: Serialize all multi-statement writes (and ideally all writes) under an asyncio.Lock held by the Database instance, e.g. add `self._write_lock = asyncio.Lock()` and wrap save_search / cleanup_old_searches / log_action / save_session bodies in `async with self._write_lock:`. Alternatively drop the manual BEGIN/commit and isolation_level=None, let aiosqlite manage deferred transactions, and still guard concurrent writers with the lock. The cleanest is a single lock so no two coroutines ever interleave statements on the shared connection.
- **Верификация**: CONFIRMED — Verified against current bot/db.py. There is exactly ONE shared aiosqlite connection opened with isolation_level=None (autocommit) at line 46; the `conn` property (lines 74-78) returns that single `_connection` — no pool. Both save_search (line 286) and cleanup_old_searches (line 498) issue an explicit `await self.conn.execute("BEGIN")` and then `await` further INSERT/DELETE statements before `await self.conn.commit()` (lines 311 / 513). Grep confirms the ONLY lock in db.py is `_connect_lock` (line 33), used solely inside connect(); there is no write lock serializing save_search / cleanup_old_
- **Статус**: [x] Исправлено (раунд 4, TDD)

## Средние

### DB-02: actions table grows without bound — no cleanup task exists
- **Файл**: `bot/db.py:397`
- **Проблема**: log_action (line 397) inserts a row into `actions` on every search, add, grab and error (called from search.py:309/691/740, music.py:154/323, trending.py:354/462). There is NO DELETE/cleanup for `actions` anywhere: _periodic_cleanup (main.py:161-178) and on_startup only call cleanup_old_sessions and cleanup_old_searches; a grep for 'DELETE FROM actions' finds nothing. On a long-lived bot (the project explicitly notes it 'не рестартится неделями') the table accumulates one row per user action forever, growing the DB file and the index idx_actions_created indefinitely on the rpie4 SD-card.
- **Риск**: Unbounded DB growth / SD-card wear and slowly degrading query/index performance over weeks of uptime.
- **Решение**: Add `async def cleanup_old_actions(self, days: int = 90)` doing `DELETE FROM actions WHERE created_at < ?` and call it from _periodic_cleanup alongside the session/search cleanup. Choose a retention window that still satisfies the /history admin view.
- **Верификация**: CONFIRMED — Independently verified in current code. bot/db.py:397 log_action() unconditionally INSERTs into the `actions` table with no cap, trigger, or row-count limit, and commits. The table and its idx_actions_created index are defined at bot/db.py:110/137-138. log_action is called on 7 common user-action paths: music.py:154, music.py:323, search.py:309, search.py:691, search.py:740, trending.py:354, trending.py:462 (searches, adds/grabs, and errors). The ONLY cleanup methods in db.py are cleanup_old_sessions (484) and cleanup_old_searches (494) — there is no cleanup_old_actions and no `DELETE FROM act
- **Статус**: [x] Исправлено (раунд 4, мультиагент)

## Низкие

### DB-03: search_results stores large monolithic JSON blobs that production never reads (write-only table)
- **Файл**: `bot/db.py:303`
- **Проблема**: save_search (lines 303-309) inserts the full results list as a single JSON blob into search_results (up to 500 SearchResult objects, hundreds of KB). The only reader, get_search_results (line 317), is invoked nowhere in the application — a grep finds it only in tests/test_db.py:119. So this table is write-only in production: every search writes a large blob that is kept for 7 days (cleanup_old_searches) and never consumed, wasting SD-card writes (wear) and space.
- **Риск**: Excessive SD-card write amplification and dead storage for data no code path ever reads.
- **Решение**: Either stop persisting full results — drop the search_results INSERT from save_search and keep only the lightweight `searches` metadata row — or, if a history-with-snapshot feature is planned, cap the serialized list (e.g. results[:20]) before json.dumps to bound blob size.
- **Верификация**: CONFIRMED — Reproduced the exact write-only path in current code. WRITE: save_search (bot/db.py:280-315) is invoked on every successful search (bot/handlers/search.py:259) and at line 302 serializes the full results list via json.dumps([r.model_dump(mode="json") for r in results]) into search_results.results_json (INSERT at lines 303-309). The searches metadata row is a separate lightweight insert. READER DEAD: the sole consumer get_search_results (bot/db.py:317-326) returns from a project-wide grep restricted to bot/ as ONLY its own definition line (bot/db.py:317); an all-repo grep finds an actual call o
- **Статус**: [ ] Не исправлено

### DB-04: close() never checkpoints/truncates the WAL — bot.db-wal can persist and grow on SD-card ⚠️PLAUSIBLE
- **Файл**: `bot/db.py:66`
- **Проблема**: WAL mode is enabled (PRAGMA journal_mode=WAL, line 50) with wal_autocheckpoint=200 (line 58). close() (lines 66-71) only calls `await self._connection.close()` without `PRAGMA wal_checkpoint(TRUNCATE)`. On an unclean or even clean shutdown the -wal file is not truncated, so bot.db-wal can remain at hundreds of KB to several MB across restarts on the rpie4 SD-card, and the synchronous=NORMAL setting means a checkpoint may be deferred. There is also no PRAGMA integrity_check on connect, so a corrupt file (SD bitrot / unclean power loss) just crashes connect() with no recovery path.
- **Риск**: WAL file bloat and no graceful handling of a corrupt DB file on a wear-prone SD card.
- **Решение**: In close(), before connection.close(), run `try: await self._connection.execute('PRAGMA wal_checkpoint(TRUNCATE)') except Exception: pass`. Optionally add a `PRAGMA quick_check` in connect() and, on failure, rename the file to bot.db.corrupt-<ts> and start fresh while logging at error level.
- **Верификация**: PLAUSIBLE — I opened bot/db.py and confirmed the literal code claims: WAL is enabled (line 50 `PRAGMA journal_mode=WAL`), synchronous=NORMAL (line 52), wal_autocheckpoint=200 (line 58), and close() (lines 66-71) only does `await self._connection.close()` with no explicit checkpoint. A repo-wide grep shows NO `wal_checkpoint` and NO `integrity_check`/`quick_check` anywhere. So the structural gaps the finding names are factually present in the current code.

However, the finding's core impact claim is overstated/inaccurate, so it does not rise to a medium defect:

1) Clean shutdown: bot/main.py on_shutdown 
- **Статус**: [x] Исправлено (раунд 4, мультиагент)

### DB-07: log_action declares -> int but returns cursor.lastrowid which can be None ⚠️PLAUSIBLE
- **Файл**: `bot/db.py:424`
- **Проблема**: log_action is annotated `-> int` (line 397) and returns `cursor.lastrowid` (line 424). aiosqlite's lastrowid is Optional[int] and is None when no rowid was produced; callers/type checkers treating the result as a non-optional int could later index/format a None as an action id. Although the INSERT normally yields a rowid, the type contract is violated and a None would silently propagate.
- **Риск**: A None action id propagating to callers/logging, violating the declared return type.
- **Решение**: Guard the result: `if cursor.lastrowid is None: raise RuntimeError('Failed to insert action record')` then `return cursor.lastrowid`, mirroring the lastrowid check already done in save_search (lines 296-299).
- **Верификация**: PLAUSIBLE — The static facts in the finding are all accurate in the current code: bot/db.py:397 declares `async def log_action(self, action: ActionLog) -> int:`, bot/db.py:424 does `return cursor.lastrowid` with no None guard, and aiosqlite's `cursor.lastrowid` is typed `Optional[int]`, so the `-> int` annotation is violated. The comparison anchor is also real: save_search at bot/db.py:296-299 already does `search_id = cursor.lastrowid; if search_id is None: await self.conn.rollback(); raise RuntimeError("Failed to insert search record")`, so the proposed fix matches an existing convention and is consiste
- **Статус**: [x] Исправлено (раунд 4, мультиагент)

## Отклонено верификацией (false positives — не чинить)

- **DB-05** FKs declared without ON DELETE CASCADE while foreign_keys=ON makes any future DELETE on parent rows fail — _The schema facts are accurate but no current code path triggers the claimed failure. I read bot/db.py in full and grepped the whole repo for DELETE statements and user-management handlers.

Confirmed true: foreign_keys=ON is set (bot/db.py:51); and the four FKs have no ON DELETE _
- **DB-06** save_session mutates the caller's SearchSession object as a side effect when truncating results — _The finding correctly quotes the in-place mutation at bot/db.py:336-337 (`if session.results and len(session.results) > 500: session.results = session.results[:500]`), but its claimed concrete failure (truncation silently shortens the handler's in-memory results, breaking paginat_
