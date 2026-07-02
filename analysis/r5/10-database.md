# Анализ базы данных TG_arr (раунд 5)

Контекст: единственная точка доступа к SQLite — класс `Database` в `bot/db.py` (прямых `conn.execute` вне db.py нет — проверено grep'ом). Notification loop (60с) в БД **не ходит** вообще — состояние торрентов держится в памяти `NotificationService`, нагрузка на БД от него нулевая.

## Критические

Критических проблем не обнаружено. Прошлые раунды (DB-01/02/04/07/13, RACE-02/04, PERF-12) реально закрыты в коде, а не только в комментариях.

## Средние

### DB-01: Бэкапов нет вообще — БД живёт только в named volume на SD-карте
- **Файл**: `docker-compose.yml` (volume `bot-data:/app/data`), `bot/db.py:71-86`
- **Проблема**: Единственная копия `bot.db` — docker named volume на SD/USB-хранилище rpie4. Нет ни `VACUUM INTO`, ни `sqlite3 .backup`, ни cron-копирования на другой носитель; в Makefile и README механизм бэкапа отсутствует.
- **Риск**: Смерть SD-карты (типовой сценарий для Pi) = безвозвратная потеря runtime-allowlist (`allowed_users`), пользовательских настроек (`users.preferences`) и истории действий.
- **Решение**: Периодический `VACUUM INTO '/app/data/backup/bot-YYYYMMDD.db'` (атомарен, работает на живой WAL-базе) из `_periodic_cleanup`, либо cron на хосте: `docker run --rm -v bot-data:/d alpine cp /d/bot.db /backup/`. Хранить 2–3 ротации вне SD.
- **Статус**: [ ] Не исправлено

### DB-02: Lost update при read-modify-write сессий в конкурентных callback'ах
- **Файл**: `bot/handlers/search.py:465` → `:484` (также `:355→374`, `:613→621`, `:851→863`; `bot/handlers/music.py:264→268`)
- **Проблема**: Паттерн `get_session → мутация → save_session` не атомарен. `update_session` (RACE-04) защищает только от воскрешения удалённой сессии, но не от перезаписи: два параллельных callback'а одного юзера (double-tap по кнопкам) оба читают сессию, мутируют разные поля и последний `save_session` затирает изменения первого.
- **Риск**: `selected_result`/`selected_content` из «проигравшего» обновления теряются → grab не того релиза или «Сессия истекла» на валидном шаге. Для бота на 1–3 юзеров проявляется редко (быстрый double-tap), но недетерминированно.
- **Решение**: Optimistic locking — колонка `version INTEGER` в `sessions`, `UPDATE ... WHERE user_id=? AND version=?`; либо per-user `asyncio.Lock` в хэндлерах на цикл «read-modify-write».
- **Статус**: [ ] Не исправлено

### DB-03: `searches`/`search_results` — write-only таблицы: двойная запись JSON на каждый поиск впустую
- **Файл**: `bot/db.py:335-382`, единственный прод-вызов `bot/handlers/search.py:281`
- **Проблема**: `save_search` пишет полный JSON результатов (до 100 релизов от Prowlarr, ~100–200 КБ) в `search_results` на **каждый** поиск, и тут же почти те же данные вторично пишутся в `sessions` (`save_session`, search.py:290). При этом `get_search_results` в проде не вызывается нигде (только `tests/test_db.py`) — данные никто никогда не читает. Вдобавок: кап в 500 результатов есть только в `save_session`, в `save_search` капа нет; `get_search_results` (db.py:380-381) парсит JSON без try/except — единственный не защищённый от битого JSON reader.
- **Риск**: Удвоенный объём записи на SD-карту (wear + латентность fsync под `_write_lock`) ради мёртвых данных; рост WAL между чекпоинтами.
- **Решение**: Либо убрать `save_search` из хот-пасса поиска (историю запросов покрывает `actions` c `ActionType.SEARCH`), либо писать только метаданные поиска без `results_json`.
- **Статус**: [ ] Не исправлено

### DB-04: Таблица `allowed_users` не интегрирована с рассылкой уведомлений
- **Файл**: `bot/main.py:117` (подписка), `main.py:319` (webhook-рассылка), `bot/handlers/users.py:63` (/adduser)
- **Проблема**: `on_startup` подписывает на уведомления только `settings.allowed_tg_ids | admin_tg_ids` (env), а `_webhook_notify` рассылает по тому же env-списку. Юзеры, добавленные через `/adduser` в DB-allowlist, не подписываются ни при добавлении, ни при рестарте — `db.list_allowed_users()` в этих местах не вызывается.
- **Риск**: Runtime-юзеры полноценно пользуются ботом, но **никогда** не получают уведомлений о завершении загрузок и webhook-импортах. Тихое расхождение между двумя allowlist'ами.
- **Решение**: В `on_startup` добавить `for uid in await db.list_allowed_users(): notification_service.subscribe_user(uid)`; в `cmd_adduser`/`cmd_deluser` — subscribe/unsubscribe; в `_webhook_notify` — объединять env-список с DB-списком.
- **Статус**: [ ] Не исправлено

### DB-05: Lost update настроек — перезапись всего JSON preferences из снапшота middleware
- **Файл**: `bot/handlers/settings.py:143-144` (и ещё 8 аналогичных мест: 196, 247, 298, 341, 381, 421, 458, 499)
- **Проблема**: `db_user` фиксируется в `AuthMiddleware` на момент прихода события; хэндлер мутирует одно поле и пишет **весь** `preferences` JSON (`db.py:256-266` — UPDATE целиком). Два конкурентных изменения настроек (быстрые тапы по разным пунктам) затирают друг друга снапшотами.
- **Риск**: Пропавшее изменение профиля качества/папки — юзер уверен, что настроил, а grab уйдёт со старым профилем. Низкая частота, но молчаливая порча.
- **Решение**: Точечный UPDATE через `json_set()` (SQLite JSON1): `UPDATE users SET preferences = json_set(preferences, '$.radarr_quality_profile_id', ?)`, либо re-fetch юзера непосредственно перед мутацией.
- **Статус**: [ ] Не исправлено

## Низкие

### DB-06: Нет композитного индекса под `get_user_actions`
- **Файл**: `bot/db.py:516-528`, индексы `db.py:158-159`
- **Проблема**: `WHERE user_id = ? ORDER BY created_at DESC LIMIT 20` использует `idx_actions_user`, затем сортирует все действия юзера за 90 дней в памяти. Композитного `(user_id, created_at DESC)` нет.
- **Риск**: Минимальный — таблица ограничена 90 днями и парой юзеров.
- **Решение**: `CREATE INDEX idx_actions_user_created ON actions(user_id, created_at DESC)` миграцией v3 (заодно можно дропнуть ставший избыточным `idx_actions_user`).
- **Статус**: [ ] Не исправлено

### DB-07: Читатели не сериализованы с явными транзакциями на общем соединении
- **Файл**: `bot/db.py:341-371` (save_search), `:601-625` (cleanup_old_searches) vs любой read-метод без `_write_lock`
- **Проблема**: `_write_lock` покрывает всех писателей, но читатели (`get_session`, `get_user` и т.д.) свободно выполняются **внутри** чужого `BEGIN..COMMIT` на том же соединении и видят незакоммиченные изменения (same-connection dirty read); при rollback читатель мог увидеть фантомные данные.
- **Риск**: Практически нулевой на текущей схеме: обе транзакции оперируют таблицами, которые конкурентные читатели либо не читают, либо читают целостные строки.
- **Решение**: Осознанно принять (задокументировать), либо переводить читателей критичных таблиц под тот же lock при появлении новых многошаговых транзакций.
- **Статус**: [ ] Не исправлено

### DB-08: VACUUM/optimize никогда не выполняются
- **Файл**: `bot/db.py` (отсутствует), `bot/main.py:161-181`
- **Проблема**: После массовых DELETE в `_periodic_cleanup` файл не сжимается (страницы лишь переиспользуются), `PRAGMA optimize` не вызывается.
- **Риск**: Размер файла выходит на плато и не растёт бесконечно — реальный вред мал.
- **Решение**: `PRAGMA optimize` в конце periodic_cleanup; `VACUUM INTO` естественно совместить с бэкапом из DB-01.
- **Статус**: [ ] Не исправлено

### DB-09: `/deluser` не чистит данные отозванного пользователя
- **Файл**: `bot/db.py:314-318`, `bot/handlers/users.py:78`
- **Проблема**: Отзыв доступа удаляет только строку из `allowed_users`; строка в `users` (с preferences), сессия и история actions остаются навсегда (actions хоть чистятся по 90 дням).
- **Риск**: Мусор/приватность; авторизация при этом корректно закрыта — middleware не пустит.
- **Решение**: В `remove_allowed_user` дополнительно `DELETE FROM sessions WHERE user_id = ?` (users можно оставить для истории).
- **Статус**: [ ] Не исправлено

### DB-10: `DATABASE_PATH` не прокинут в docker-compose
- **Файл**: `docker-compose.yml` (блок environment), `bot/config.py:74`
- **Проблема**: Настройка `database_path` существует, но в compose не пробрасывается, а `.env` в образ не копируется — путь в контейнере фактически захардкожен на дефолт `data/bot.db`.
- **Риск**: Нулевой сейчас (дефолт указывает ровно в volume `/app/data`); лишь негибкость при будущем переносе БД.
- **Решение**: Добавить `DATABASE_PATH: ${DATABASE_PATH:-data/bot.db}` в environment для симметрии (пересекается с DEPLOY-01).
- **Статус**: [ ] Не исправлено

## Проверено — проблем нет

- **Режимы SQLite** (`bot/db.py:55-65`): WAL + `synchronous=NORMAL` + `busy_timeout=5000` + `foreign_keys=ON` + `wal_autocheckpoint=200` + `temp_store=MEMORY` + `mmap_size=32MB` — корректный набор для SD-карты; NORMAL в WAL не делает fsync на каждый commit.
- **Миграции**: версионирование через `PRAGMA user_version` (`db.py:177-224`), миграции идемпотентны, v2 проверяет наличие колонки перед ALTER; крэш между ALTER и bump'ом версии переживается повторным прогоном.
- **Соединения**: одно долгоживущее aiosqlite-соединение (все запросы сериализуются его worker-потоком), прямого доступа к `conn` вне db.py нет; «database is locked» внутри процесса исключён, внешний доступ покрыт busy_timeout.
- **Транзакции**: оба многошаговых пути (`save_search`, `cleanup_old_searches`) — explicit `BEGIN` + rollback в except, под `_write_lock`; все одиночные записи тоже под lock'ом (проверены все 11 писателей). Есть конкурентный тест `tests/test_audit_r4_fixes.py:168`.
- **kill -9 / crash-safety**: WAL-журнал восстанавливается автоматически; `wal_checkpoint(TRUNCATE)` на graceful close (`db.py:71-86`). Потеря возможна только при power-loss на лгущей про fsync SD (последние транзакции с последнего чекпоинта), без коррупции.
- **Datetime**: везде timezone-aware UTC ISO (`models._utcnow`, `datetime.now(timezone.utc)` во всех записях db.py) — `TZ=Europe/Moscow` влияет только на логи; строковые сравнения `created_at < cutoff` в cleanup корректны при едином формате.
- **JSON-чтение защищено** (кроме мёртвого `get_search_results`, см. DB-03): `_row_to_user` — fallback на дефолтные preferences (`db.py:268-300`), `get_session` — удаление битой сессии (`db.py:462-471`), enum'ы в `_row_to_action` — fallback значения.
- **Рост таблиц ограничен**: `_periodic_cleanup` каждые 6ч + на старте (sessions 24ч, searches 7д, actions 90д); под каждый DELETE/ORDER BY есть индекс.
- **Дубликаты**: `sessions` — UPSERT по PK; `allowed_users` — `ON CONFLICT DO NOTHING`; гонка создания юзера обработана re-fetch'ем (`bot/middleware/auth.py:92-100`).
- **FK-целостность**: FK объявлены и включены; `cleanup_old_searches` удаляет детей до родителей — CASCADE не нужен.
- **`PRAGMA user_version = {v}`** через f-string (`db.py:190`) — принудительная коэрция в int, инъекция невозможна.
- **Notification loop / NOTIFY_CHECK_INTERVAL=60с** — БД не трогает вовсе (in-memory dict + qBittorrent API).

Итог: слой БД в хорошем состоянии после 4 раундов аудита — критики нет. Главное: бэкапы (DB-01), мёртвые write-only таблицы, изнашивающие SD (DB-03), и разрыв между `allowed_users` и подпиской на уведомления (DB-04).
