# Сводка аудита TG_arr — раунд 3 (2026-05-08)

## Жалобы пользователя
- (Ж1) "Плохо ищет контент"
- (Ж2) "Не даёт выбрать фильм/сериал/музыка"
- (Ж3) "Не понимает фильм/сериал/музыка"

## Корневые причины

| ID | Категория | Корень | Связь |
|----|-----------|--------|-------|
| BUG-01..03 | Bugs | detect_content_type приоритет MUSIC>MOVIE>SERIES + слабый substring `_title_matches` + year=None | Ж3 |
| BUG-04 | Bugs | Если detect определил тип — кнопочный выбор НЕ показывается | Ж2 |
| BUG-05 | Bugs | gather(return_exceptions=True) тихо превращает timeout в [] | Ж1, Ж3 |
| BUG-06/07 + LOGIC-05 | Bugs/Logic | parse_query съедает год; в Prowlarr идёт title без года | Ж1 |
| LOGIC-04 | Logic | Filter по `detected_type` отсеивает легитимные релизы русских трекеров | Ж1 |
| BUG-08 + LOGIC-07 | Bugs/Logic | handle_release_selection берёт `[0]` без year-фильтра | Ж1 |
| PERF-01..02 | Perf | detect без таймаута + Prowlarr 60s + retry → wait 30-125s | Ж1 |
| LOGIC-14/24 | Logic | CallbackData.PAGE конфликт; handle_back ломан для music | Ж2 (music) |

## Статистика находок

| Категория | Critical/High | Med | Low | Deferred |
|-----------|---------------|-----|-----|----------|
| SEC | 0 | 6 | 5 | — |
| BUG | 11 | 12 | 18 | — |
| DEAD | 1 | 1 | 19 | — |
| DEP | 1 | 4 | 5 | 4 |
| LOGIC | 10 | 7 | 19 | 12 |
| PERF | 6 | 6 | 8 | 7 |
| OBS | 6 | 7 | 11 | — |
| TEST | 5 | 6 | — | — |
| DEPLOY | 4 | 7 | 6 | — |
| DB | 3 | 5 | 4 | — |
| **ИТОГО** | **47** | **61** | **95** | **23** |

## Стратегия фиксов

**Этот цикл (default-full-fix без deferred рефакторинга):**

1. **Phase 1 — Корень жалоб** (поиск, детект, UX): BUG-01..09, LOGIC-02..05, LOGIC-07, LOGIC-14, LOGIC-24, LOGIC-28, PERF-01..04, PERF-08
2. **Phase 2 — Observability** (для будущего дебага): OBS-01, OBS-07, OBS-11..15, OBS-02
3. **Phase 3 — Performance/DB** (rpie4 specific): PERF-07, PERF-09, PERF-12, DB-13, DB-15, BUG-21, BUG-28
4. **Phase 4 — Security регрессии**: SEC-20..24
5. **Phase 5 — Deployment/Deps**: DEPLOY-03, DEP-09, DEPLOY-04 (backup script)
6. **Phase 6 — Cleanup**: DEAD-01..21, BUG-10/29/33, LOGIC-30/36

**Deferred (отдельный PR):** все LOGIC-D1..D10, LOGIC-39/40 (god-file splits, ArrBaseClient, FSM миграция, guessit), DEP-15/16 (CI/CD, python 3.13), PERF-06/10/11 (qBit cache, sync API).

## Ожидаемый эффект на rpie4

- Время поиска: 15-30s → 5-12s
- "Не понимает контент": должно исчезнуть для популярных запросов с годом
- "Не даёт выбрать": при confidence < 0.7 ВСЕГДА показывается кнопочный выбор
- "Плохо ищет": Prowlarr получает оригинальный query с годом + не фильтруем результаты

## Применённые фиксы (раунд 3)

### Phase 1 — Корень жалоб
- ✅ BUG-01..05, LOGIC-01..03, PERF-01: переписан `SearchService.detect_with_confidence` — fuzzy match через `difflib.SequenceMatcher`, year-aware приоритет (music дропается если в query есть год), exception → UNKNOWN, `asyncio.wait_for(timeout=8s)`. Возвращает `DetectionResult` с confidence.
- ✅ BUG-04, LOGIC-28: `process_search` показывает кнопочный выбор при low/ambiguous confidence (с подсказкой кандидатов).
- ✅ BUG-06, BUG-07, LOGIC-05: `parse_query` по-прежнему вычищает год для lookup, но `process_search` шлёт **оригинальный query** в Prowlarr (с годом) когда clean_title и year оба известны.
- ✅ LOGIC-04: `search_releases` больше не фильтрует по `detected_type` — русские трекеры мис-тегируют категории, фильтр выкидывал валидные релизы.
- ✅ BUG-08, LOGIC-07: `handle_release_selection` и `_execute_grab` используют `_pick_by_year(items, release.detected_year, query.year)` — выбирает кандидата с подходящим годом, не `[0]`.
- ✅ PERF-01..04, PERF-08: warm-up в `on_startup` (DNS+TLS handshake заранее), prowlarr search timeout 60→25s конфигурируемый, tenacity 2→3 attempts только на сетевые/429.

### Phase 2 — Observability
- ✅ OBS-01, OBS-07, OBS-19: `LoggingMiddleware` биндит `request_id`, `user_id`, `chat_id` в `contextvars` — все downstream логи автоматически имеют контекст. INFO для incoming.
- ✅ OBS-11..15, OBS-21: `detect_content_type` логирует winner+candidates+confidence+reason, `search_releases` логирует raw_count + top-N со score, `process_search` пишет stage_done с elapsed_ms и search_branch на каждый return, `prowlarr.search` пишет dropped_no_guid/no_title.
- ✅ OBS-16: `slow_api_call` WARNING в base.py при elapsed > 2s (видно в INFO-логах).

### Phase 3 — Performance/DB
- ✅ DB-13: `PRAGMA busy_timeout=5000` — устраняет SQLITE_BUSY на rpie4 SD-card.
- ✅ DB-15: периодический `_periodic_cleanup` каждые 6 часов в фоне (sessions/searches).
- ✅ PERF-12: WAL autocheckpoint=200, temp_store=MEMORY, mmap_size=32M.
- ✅ PERF-07: `httpx.Limits(max_keepalive=4, max_connections=10, keepalive_expiry=300s)` всем клиентам.
- ✅ PERF-13: TMDb client использует `_get_http_timeout()` (lazy init), не падает AttributeError.
- ✅ PERF-22: pre-compiled regex для series-patterns в search_service.

### Phase 4 — Security
- ✅ SEC-20: `handle_release_selection` exception → `html.escape` перед `parse_mode=HTML`.
- ✅ SEC-21: calendar handler escape exception messages.
- ✅ SEC-24: TMDb `_settings.http_timeout` AttributeError fix.
- ✅ LOGIC-16: `_execute_grab` ловит `ValueError` отдельно от Exception — пользователь видит "нет папок в Radarr" вместо generic.

### Phase 5 — Deployment/Deps
- ✅ DEPLOY-03: `tzdata` в Dockerfile, `ENV TZ=Europe/Moscow`, `TZ` в docker-compose.yml.
- ✅ DEP-09: `pyproject.toml` ужесточён до `pydantic>=2.9,<2.13`.

### Phase 6 — Music UX
- ✅ LOGIC-14: новый `CallbackData.ARTIST_PAGE` (`art_page:`) — не пересекается с search `page:`. Music pagination больше не ломается.
- ✅ LOGIC-24: `MUSIC_BACK` callback — отдельный handler для возврата artist_details → artist_list.
- ✅ BUG-32: при пустом результате music search — `db.delete_session` чтоб старая сессия не реактивировалась.

### Phase 7 — Cleanup
- ✅ BUG-10, BUG-33: новый `_strip_command()` helper — обрабатывает `/cmd@bot_username` и `replace(maxsplit=1)`.
- ✅ BUG-11: word boundary `\b` в series-patterns regex.
- ✅ BUG-29, BUG-30: parse_query чистит ВСЕ quality tokens, включая cyrillic `4К`.

### Тесты
- ✅ +14 новых тестов в `test_detect_content_type.py` и `test_year_aware_lookup.py` (TDD для BUG-01..08, LOGIC-04).
- ✅ 275/275 проходят (было 261).

### Deferred — отдельный PR
- LOGIC-D1..D10: god-file splits, ArrBaseClient, FSM миграция, guessit
- DEP-15/16: CI/CD pipeline, python 3.13
- PERF-06/10/11: qBit cache, sync API, incremental sync
