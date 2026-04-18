# Security Audit — TG_arr (Round 2)

Дата: 2026-04-18. Фокус: SSRF, secrets, injection, auth, HTML injection, callback signature, rate-limit, resource-exhaustion.

Закрыто в предыдущем аудите: SEC-01 (SSRF all addrinfo), SEC-04 (url masking), SEC-07, SEC-11 (DNS async).

## SEC-02 — Exception leak в тексте callback у trending (HIGH)
Файл: `bot/handlers/trending.py:110-111, 159-160, 189, 303, 361`
```python
await callback.message.edit_text(
    f"❌ Ошибка при загрузке популярных фильмов:\n{html.escape(str(e))}"
)
```
Любая внутренняя ошибка (API-ключ, URL backend, stacktrace-like сообщение, httpx ConnectError с hostname) показывается пользователю. В whitelist-сценарии критичность умеренная, но вытаскивает internal hostnames и пути.
**Решение:** логировать полный `str(e)`, а пользователю — дженерик через `Formatters.format_error(...)`.

## SEC-03 — Telegram token в aiogram DEBUG трейсах (MED)
Файл: `bot/main.py:78-83`, `docker-compose.override.yml:15`
При `bot.get_me()` aiogram в DEBUG пишет полный request URL с bot-token (`https://api.telegram.org/bot<token>/getMe`). Override выставляет `LOG_LEVEL=DEBUG` — в dev контейнере токен попадает в stdout (→ `docker logs`, Portainer UI, ELK).
**Решение:** в `main.py` `setup_logging` добавить `structlog.processors.add_log_level` + filter, маскирующий `/bot[0-9]+:[A-Za-z0-9_-]+/`. Либо переключить override на INFO.

## SEC-05 — Rate limit bypass across restart (MED)
Файл: `bot/middleware/auth.py:136-188`
`RateLimitMiddleware._user_requests` — in-memory dict, сброс при рестарте/редеплое. Злоумышленник-инсайдер может перезапустить контейнер для сброса лимита. Также лимит 30 req/min достаточно жёсткий: 1 search = 3 lookup + torrent fetch + session save.
**Решение:** принять как ограничение (whitelist модель) + задокументировать. Либо вынести в SQLite (`rate_limit` таблица `user_id, ts`).

## SEC-06 — Callback actions без HMAC-подписи (MED)
Файлы: `bot/handlers/trending.py:280-306`, `bot/handlers/search.py:*`, `bot/handlers/music.py:*`, `bot/handlers/downloads.py:346-408`
- `add_movie:<tmdb_id>`, `trend_m:<tmdb_id>` — пользователь подставляет произвольный `tmdb_id` → добавляется произвольный фильм в Radarr. В whitelist это «только свои», но нет audit-trail о том, кто изначально запросил, и любой участник allowlist может нажать кнопку под сообщением другого.
- `t_delete:<hash>`, `t_delf:<hash>` — проверка `is_admin` присутствует, но любой админ может деструктивно удалить любой торрент.

**Решение:** для whitelist-бота HMAC оверкилл; минимум — rate-limit для деструктивных операций (`t_delete`, `t_delf`, `emby_restart`, `emby_update`) отдельный от общего; логировать с `actor_id` + `origin_message_user_id`.

## SEC-08 — Unbounded user_id кеши (LOW)
- `bot/handlers/trending.py:27-28` — `_trending_movies_cache`, `_trending_series_cache` (есть защита `_MAX_CACHE_SIZE=200`)
- `bot/handlers/calendar.py:23` — `_user_period` (есть защита 100)
- `bot/handlers/music.py:35-36` — `_artist_candidates`, `_trending_artists_cache` — **защиты нет**
- `bot/middleware/auth.py:142` — `_user_requests` (есть slot-cleanup при 10000)

**Решение:** добавить LRU/TTL на music dict'ы; или использовать `bot.db.sessions` для артистов (сохранить список кандидатов в session).

## SEC-13 — HTML-escape не полный в ошибках trending (MED)
Файл: `bot/handlers/trending.py` — использует inline `html.escape(str(e))` вместо `_e()` обёртки из `formatters.py`. Для обычных `str(e)` — ок, но стиль непоследовательный. Минор.
**Решение:** унифицировать через `Formatters.format_error` (см. SEC-02).

## SEC-14 — Dockerfile HEALTHCHECK не проверяет liveness polling (HIGH)
Файл: `Dockerfile:27-28`
HEALTHCHECK только проверяет, что settings импортируются. Если polling-loop умер из-за deadlock в aiogram / Telegram timeout / исчерпания httpx-пула, контейнер `healthy`, Portainer/Swarm не перезапустит.
**Решение:** `bot/main.py` touch-ит sentinel `/tmp/alive` каждые N секунд (асинк-таска); HEALTHCHECK проверяет `find /tmp/alive -mmin -2`.

## SEC-15 — qBittorrent `follow_redirects=True` для sessions-cookie (LOW)
Файл: `bot/clients/qbittorrent.py:72-81`
Если qBit-URL редиректит на внешний хост (очень маловероятно, но возможно при misconfig reverse-proxy), сессионная cookie SID уходит наружу.
**Решение:** `follow_redirects=False` для prod; логировать 30x явно.

## SEC-16 — push_release отправляет download_url в Radarr/Sonarr/Lidarr без SSRF-валидации (HIGH)
Файлы: `bot/services/add_service.py:343-348, 487-492, 680-685`
`_validate_download_url` вызывается **только** перед `qbittorrent.add_torrent_url`. `radarr.push_release(download_url=...)` и аналоги отправляют URL напрямую *arr-сервисам, которые потом скачают его с их credentials внутри private-сети. Классический SSRF-via-downstream: `http://192.168.1.1/admin/api/...?apikey=...` из Radarr.
**Решение:** валидировать `download_url` до любого `push_release`/`grab_release`, не только перед qBit fallback.

## SEC-17 — Lidarr `add_artist` захардкоживает `monitorNewItems="all"` (MED)
Файл: `bot/clients/lidarr.py:114`
Даже если пользователь выбрал `monitor="none"`, бот всё равно ставит `monitorNewItems: "all"` → будущие альбомы автомониторятся. Нарушение намерения пользователя (не security в узком смысле, но access violation).
**Решение:** использовать `monitor` из параметра или preferences.

## SEC-18 — `DATABASE_PATH` без path-validation (LOW)
Файл: `bot/db.py:42-44`, `bot/config.py:74`
`Path(db_dir).mkdir(parents=True, exist_ok=True)` создаст директорию по любому пути env'а, включая `../../etc/bot.db`. В контейнере под `botuser` ограничено правами, но validator не помешает.
**Решение:** в `config.py` `@field_validator("database_path")` — reject `..` и abspath вне `/app/data`.

## SEC-19 (НОВЫЙ) — Session JSON не имеет схема-versioning (MED)
Файл: `bot/db.py:235-289`, `bot/models.py:270-283`
`SearchSession` хранится как JSON, десериализуется через `SearchSession.model_validate`. При изменении структуры модели (например, новое обязательное поле в UserPreferences или SearchSession) старая сессия развалится. Pydantic `ValidationError` ловится, сессия стирается (ОК), но потеряется UX и user-flow. Плюс если `selected_content` содержит один из 4 discriminated types (MovieInfo/SeriesInfo/ArtistInfo/AlbumInfo), добавление нового варианта сломает старые сессии.
**Решение:** добавить `schema_version` в JSON payload; при mismatch — `delete_session`.

## Итого

| ID      | Severity | Fix complexity |
|---------|----------|----------------|
| SEC-02  | HIGH     | S (4 места) |
| SEC-03  | MED      | S |
| SEC-05  | MED      | M |
| SEC-06  | MED      | M (rate-limit) |
| SEC-08  | LOW      | S |
| SEC-13  | MED      | S |
| SEC-14  | HIGH     | M |
| SEC-15  | LOW      | S |
| SEC-16  | HIGH     | S |
| SEC-17  | MED      | S |
| SEC-18  | LOW      | S |
| SEC-19  | MED      | S |

HIGH: 3 (SEC-02, SEC-14, SEC-16), MED: 6, LOW: 3.
