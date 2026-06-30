# Сводка аудита TG_arr — раунд 4 (2026-06-30)

Метод: 11 finder-агентов (по категориям) → состязательная верификация **каждой** находки независимым
скептиком (refute-by-default). Из 68 заявленных находок выжило **61** (55 CONFIRMED + 6 PLAUSIBLE),
**7 отклонено** как false positives и зафиксировано в отчётах (чтобы будущие раунды их не переоткрывали).

## Статистика (после верификации)

| Категория | Файл | crit | high | med | low | rejected |
|-----------|------|:----:|:----:|:---:|:---:|:--------:|
| Security | 01 | 0 | 2 | 1 | 0 | 0 |
| Bugs + Race | 02 | 1 | 2 | 2 | 4 | 2 |
| Dead code | 03 | 0 | 0 | 1 | 12 | 0 |
| Dependencies | 04 | 0 | 0 | 1 | 3 | 0 |
| Logic | 05 | 0 | 0 | 1 | 3 | 2 |
| Performance | 06 | 0 | 0 | 4 | 3 | 0 |
| Observability | 07 | 0 | 0 | 2 | 3 | 0 |
| Testing | 08 | 0 | 0 | 4 | 3 | 0 |
| Deployment | 09 | 0 | 0 | 1 | 3 | 1 |
| Database | 10 | 0 | 1 | 1 | 3 | 2 |
| **ИТОГО** | | **1** | **5** | **18** | **37** | **7** |

## Топ проблем (что чинить первым)

| ID | Sev | Суть | Эффект для пользователя |
|----|-----|------|--------------------------|
| **RACE-01** | crit | Нет защиты от двойного grab: двойной тап «Подтвердить/Лучший/Force» = двойной add/grab/download | Дубликаты в Radarr/qBittorrent, две записи в истории |
| **RACE-02 / DB-01** | high | Гонка транзакций на **одном** соединении SQLite: `BEGIN…commit` одной корутины обрывает транзакцию другой | Молча теряется сохранённый поиск, иногда `OperationalError` |
| **SEC-01** | high | `torrent.name` без `html.escape` в `/pause` `/resume` (HTML parse_mode) | `&`/`<` в имени релиза → 400, нет подтверждения; инъекция разметки |
| **SEC-02** | high | TMDB-тайтл без escape в подтверждении add из «🔥 Топ» | `Fast & Furious` → «не удалось добавить», хотя реально добавлено |
| **BUG-01** | high | Кнопка «◀️ Назад» в «🔥 Топ» перехватывается `search.handle_back` | Всегда «Сессия истекла» вместо возврата в меню |
| **SEC-03** | med | Passkey приватного трекера утекает в логи (`result=` сырого push-ответа) | Кто читает логи — получает переиспользуемый credential |

**Дубли-корни (один фикс закрывает оба):** RACE-02 = DB-01; LOGIC-05 = PERF-03 (календарь последовательно).

## Сквозные темы раунда 4

1. **Конкуренция — главная незакрытая зона.** Прошлые раунды чинили детект/поиск/UX; гонки на сессии и
   соединении БД остались. Бот реально многозадачный (aiogram дёргает каждый callback отдельной task),
   а сериализации общего состояния нет нигде, кроме singleton-локов клиентов.
2. **HTML-escape применён непоследовательно.** `search.py`/`music.py`/`calendar.py` экранируют, а
   `downloads.py` и `trending.py` — нет. Это конвенция, нарушенная в 2 модулях.
3. **N+1 round-trips на Pi.** Календарь, Emby-статус, qBit-статус, pause/resume/delete — по 2–4
   последовательных HTTP-запроса там, где хватило бы `asyncio.gather` или одного списка.
4. **Мёртвый код накопился.** `constants.py` целиком осиротел (и дублируется магическими числами),
   плюс целая album-ветка Lidarr (lookup_album/format_album_info) недостижима.
5. **Тесты не покрывают критичный путь.** Auth/RateLimit, grab happy-path, qBit re-auth (403),
   poller уведомлений — ноль тестов.

## Отклонённые false positives (7)

BUG-03 (📺 для music — недостижимо), RACE-06 (гонки RateLimit нет — секция без await),
LOGIC-02 (size-penalty для music — ветка недостижима), LOGIC-04 (Prowlarr.grab_release «неверный» — на самом деле корректный),
DEPLOY-01 (нет SIGTERM-хендлера — aiogram 3.27 его ставит сам), DB-05 (FK без CASCADE — нет DELETE родителей),
DB-06 (мутация сессии в save_session — caller её больше не использует). Детали — в соответствующих отчётах.

## Стратегия исправления — см. `12-fix-plan.md`

По правилу default-full-fix чинится **всё** (любой severity), кроме чисто архитектурного рефакторинга,
который вынесен в отдельный отложенный раздел плана.

## Применённые фиксы (раунд 4) — Critical + High + Security

Закрыто по согласованию с пользователем, все через TDD (RED→GREEN), без архитектурного рефакторинга:

- ✅ **RACE-01** (critical): per-user guard `_claim_grab`/`_release_grab` в `handle_grab_best`/`handle_confirm_grab`/`handle_force_grab` + music `handle_confirm_music_add`. Двойной тап → второй вызов отбивается «уже обрабатываю».
- ✅ **RACE-02 / DB-01** (high): `Database._write_lock` сериализует все методы-писатели. Тест на конкурентные `save_search`+`cleanup` (падал `cannot start a transaction within a transaction`) теперь зелёный.
- ✅ **SEC-01** (high): `html.escape(torrent.name)` в `/pause` `/resume`.
- ✅ **SEC-02** (high): `html.escape(...)` тайтлов в add из трендов (movie+series).
- ✅ **BUG-01** (high): новый `CallbackData.TRENDING_BACK` + `handle_trending_back` — «Назад» в трендах больше не перехватывается `search.handle_back`.
- ✅ **SEC-03** (med): `_safe_push_result()` — в логи идут только `approved`/`rejections`, passkey из `downloadUrl` не утекает.

**Проверка:** `pytest` — **291 passed** (было 280; +11 новых TDD-тестов в `tests/test_audit_r4_fixes.py`), `ruff check` — clean. (mypy в окружении аудита не установлен — не прогонялся.)

Остальные находки (medium/low: dead code, perf, deps, observability, тесты, RACE-04/05) — в плане, в этот цикл не вносились.
