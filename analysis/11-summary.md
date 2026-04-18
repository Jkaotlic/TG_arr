# Audit Summary — TG_arr (Round 2)

Дата: 2026-04-18. Проект: Python 3.12, aiogram 3.26, ~8500 LoC, production on Raspberry Pi 4 / Portainer.

## Прогресс vs. прошлый аудит

**Закрыто:** BUG-01, 04, 05, 17, 19, 23; SEC-01, 04, 07, 11; LOGIC-07; DEP-01, 03, 04; DEAD-01, 02, 03, 16, 17, 25. Плюс добавлена Lidarr/Deezer интеграция, SSRF stronger (getaddrinfo), tenacity shortened, URL masking в логах, lazy settings в BaseAPIClient.

**Всё ещё открыто (из прошлого):** BUG-10, 11, 12, 14, 15, 20, 27; SEC-02, 03, 05, 06, 08, 13, 14; DEAD-04..08; LOGIC-01..05, 08; DEP-02; DEAD-27.

## Счётчик нового цикла

| Категория | HIGH | MED | LOW/INFO | Всего |
|-----------|------|-----|----------|-------|
| Security  | 3    | 6   | 3        | 12    |
| Bugs      | 3    | 8   | 5        | 16    |
| Dead code | 1*   | 2   | 16       | 19    |
| Deps      | 1    | 2   | 6        | 9     |
| Logic     | 3    | 7   | 10       | 20    |
| Perf      | 4    | 6   | 4        | 14    |
| Observ.   | 2    | 4   | 4        | 10    |
| Testing   | 3    | 6   | 5        | 14    |
| Deploy    | 2    | 4   | 6        | 12    |
| DB        | 2    | 4   | 6        | 12    |
| **Итого** | **24** | **49** | **65** | **138** |

*DEAD-31 crash-if-invoked помечен HIGH, хотя текущий коллбек не вызывается.

Архитектурно-deferred: LOGIC-01, 02, 03, 04, 05 (god-files, ArrBase, unified grab).

## Топ-10 критических

1. **BUG-15** — recursive callback `handle_torrent_details` после `callback.answer()` вызывает `TelegramBadRequest` при pause/resume/delete.
2. **BUG-27** — music CONFIRM_GRAB router не делает fall-through; у movie/series тихо ломается кнопка "Скачать".
3. **SEC-16** — `push_release(download_url=...)` не валидирует URL перед отправкой в Radarr/Sonarr/Lidarr → SSRF через downstream клиент с private-network credentials.
4. **SEC-14 / DEPLOY-04** — HEALTHCHECK не детектит polling-deadlock; контейнер `healthy` при мёртвом боте.
5. **DEPLOY-05** — `docker-compose.override.yml` автоподхватывается и может включить DEBUG + host-volume на prod (token-leak SEC-03).
6. **BUG-32** — single-episode grab ставит `monitor_type="none"` → Sonarr не мониторит будущие серии.
7. **DB-02** — нет WAL mode, на SD-карте rpi4 writes медленные.
8. **DB-04** — `PRAGMA foreign_keys` не включена — FK декларативны.
9. **SEC-02** — exception messages утекают пользователю в trending handler'ах (5 мест).
10. **PERF-02 / PERF-08 / PERF-09 / PERF-10** — N+1 на каждом torrent callback + session round-trip без кэша.

## Критичные focus-area

- **Handler layer 0% coverage** (TEST-01): любая регрессия проходит. Music/trending hacks не тестированы.
- **Observability слабое**: middleware не кладёт user_id в contextvars, request_id нет, метрик нет (OBS-01, OBS-07).
- **Download-flow race**: BUG-15 + BUG-27 — два независимых баги в callback-handling.
- **qBittorrent overhead**: каждый callback = full `get_torrents()` (PERF-02, PERF-08, PERF-09).

## Готовность Lidarr/Deezer/music (новый код)

- **Unit tests есть** — parsing, lookup, add_artist payload, Deezer trending, URL masking, SSRF.
- **Callback flow не тестирован** — `process_music_search`, `_handle_confirm_music_add`, trending artist click.
- **BUG-27 блокирует общий CONFIRM_GRAB** — music router "захватывает" callback, movie/series теряют обработчик.
- **SEC-17** — `monitorNewItems="all"` хардкод в `add_artist`, игнорирует пользовательский `monitor`.

## Distribution: Fixes vs. Deferred

- **Fixes (этот цикл):** 133 из 138 — все bugs, sec, perf, obs, test, deploy, db, deps + мелкий logic (LOGIC-06, 08-20).
- **Deferred (отдельный PR):** 5 — LOGIC-01, 02, 03, 04, 05 (god-file splits, ArrBaseClient, unified grab_release).

## Рекомендация

Начать с Phase 1 (quick wins, параллельные fixes) в `12-fix-plan.md`: HIGH security (SEC-02, SEC-14, SEC-16), BUG-15/27, DB-02/04. Архитектурный рефакторинг (LOGIC-01..05) — после стабилизации регрессий через middleware/handler тесты.
