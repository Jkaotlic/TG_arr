# Анализ безопасности TG_arr v1.0 (раунд 3)

Дата: 2026-05-08. Фокус: регрессии после round2 + новые находки. Прочитан весь `bot/`, `Dockerfile`, `docker-compose*.yml`, `.env.example`, `.dockerignore`, `requirements.txt`.

Контекст: бот работает в whitelist-режиме (ALLOWED_TG_IDS), порог критичности соответственно понижен — приоритет «лик внутренних данных», SSRF/инъекции в downstream-сервисы, утечки токенов в логах.

Закрыто (не повторяется): SEC-01 SSRF (множественные A/AAAA), SEC-04 url-masking, SEC-07, SEC-11 DNS async, SEC-14 healthcheck (теперь touch-file `/tmp/tgarr-alive` + `_liveness_watchdog` thread), SEC-16 SSRF до push_release, SEC-17 monitorNewItems.

## Критические

_Критических находок не выявлено._ SSRF-валидация работает до всех push/grab вызовов; токены маскируются в логах; bot.middleware.auth защищает все хендлеры; HEALTHCHECK видит зависание event loop. Все ранее закрытые HIGH-находки остаются закрытыми, регрессий нет.

## Средние

### SEC-20: Утечка `str(e)` в текст сообщения пользователю (handle_release_selection)
- **Файл**: `bot/handlers/search.py:467-473`
- **Проблема**:
  ```python
  except Exception as e:
      logger.warning("Failed to lookup content", error=str(e))
      await callback.message.edit_text(
          f"{text}\n\n⚠️ Ошибка загрузки информации: {str(e)}",
          ...
          parse_mode="HTML",
      )
  ```
  `str(e)` идёт в `edit_text` с parse_mode=HTML без `html.escape` и без `Formatters.format_error`. Если httpx бросит `ConnectError("Connection refused on http://10.0.x.y:7878 ...")` — внутренний адрес уйдёт в чат. Кроме того, `<` в сообщении исключения сломает HTML и приведёт к `TelegramBadRequest`. Это прямой аналог уже закрытой SEC-02, но регрессия в новом месте.
- **Риск**: Средний (whitelist бот, но и leak hostname, и потенциальный crash сообщения)
- **Решение**: заменить на `Formatters.format_error("Не удалось загрузить информацию о фильме/сериале")`, `str(e)` оставить только в `logger.warning`.
- **Статус**: [ ] Не исправлено

### SEC-21: Утечка raw exception в текст календаря
- **Файл**: `bot/handlers/calendar.py:46-66`
- **Проблема**:
  ```python
  except Exception as e:
      logger.error("Sonarr calendar error", error=str(e))
      errors.append(f"Sonarr: {e}")          # raw repr exception
  ...
  text = Formatters.format_calendar(...)
  if errors:
      text += "\n\n⚠️ " + " | ".join(errors)   # без html.escape, parse_mode=HTML
  ```
  `format_calendar` возвращает уже truncated/escaped строку, но `errors` дописываются после. Любое исключение от `RadarrClient/SonarrClient/LidarrClient.get_calendar` — например `APIError("Ошибка Radarr: 500")` или `ServiceConnectionError("Не удалось подключиться к Sonarr (http://10.0.0.x:8989)")` — попадает в чат как plain text без эскейпа. Если в сообщении есть `<` → `TelegramBadRequest` и пользователь не увидит календарь вовсе. Также leak внутренних URL.
- **Риск**: Средний
- **Решение**:
  - заменить на `errors.append(f"Sonarr: {type(e).__name__}")` или фиксированный `"Sonarr недоступен"`;
  - применить `html.escape` к итоговой строке ошибок перед склейкой с уже-escaped календарём.
- **Статус**: [ ] Не исправлено

### SEC-22: `error_msg` из ActionLog уходит без HTML-escape (trending)
- **Файл**: `bot/handlers/trending.py:362-364, 471-473`
- **Проблема**:
  ```python
  error_msg = action.error_message or "Неизвестная ошибка"
  await status_msg.edit_text(f"❌ Ошибка: {error_msg}")
  ```
  `action.error_message` приходит из `add_service.add_movie/add_series` как `str(e)` от `RadarrClient.add_movie`/`SonarrClient.add_series`. Может содержать internal URL/payload (например, `APIError("Ошибка Radarr: 400")` с `response_body`). Здесь `parse_mode` не указан — это спасает от HTML-инъекции, но не от leak'а. Тем не менее непоследовательно: те же ошибки в `search.py:_execute_grab` идут через `Formatters.format_error(msg)`.
- **Риск**: Средний
- **Решение**: использовать `Formatters.format_error(error_msg)` (он применит `_e()` = `html.escape`) либо ограничить вывод дженериком «не удалось добавить».
- **Статус**: [ ] Не исправлено

### SEC-23: `e.message` в /pause /resume — leak qBit-URL и непоследовательная обработка
- **Файл**: `bot/handlers/downloads.py:137, 165`
- **Проблема**:
  ```python
  except QBittorrentError as e:
      await message.answer(f"❌ Ошибка: {e.message}")
  ```
  `QBittorrentError.message` для `ConnectError` создаётся как `f"Не удалось подключиться к qBittorrent ({self.base_url})"` — это включает внутренний URL `http://qbittorrent:8080`. Аналогично для timeout. Низкая критичность для whitelist, но непоследовательно: остальные хендлеры в downloads.py отдают дженерик.
- **Риск**: Средний (low-med — больше непоследовательность чем leak)
- **Решение**: дженерик `"❌ qBittorrent недоступен"`, оригинал — в logger.
- **Статус**: [ ] Не исправлено

### SEC-24: TMDb-клиент обращается к `self._settings.http_timeout` до его инициализации
- **Файл**: `bot/clients/tmdb.py:36`
- **Проблема**:
  ```python
  async def _get_client(self) -> httpx.AsyncClient:
      async with self._client_lock:
          if self._client is None or self._client.is_closed:
              self._client = httpx.AsyncClient(
                  ...
                  timeout=httpx.Timeout(self._settings.http_timeout),
                  proxy=self._proxy_url,
              )
  ```
  `BaseAPIClient.__init__` (см. base.py:67) теперь устанавливает `self._settings = None` и обращается к нему лениво через `_get_http_timeout()`. `TMDbClient._get_client` пере-определён и читает `self._settings.http_timeout` напрямую → `AttributeError: 'NoneType' object has no attribute 'http_timeout'` при первом вызове. Хендлер `handle_trending_movies` ловит исключение и пишет `Formatters.format_error("Не удалось загрузить популярные фильмы")` — пользователь видит ошибку, функционал недоступен. Это availability-баг с security-окраской: `LOG_LEVEL=DEBUG` зальёт стектрейс с путями.
- **Риск**: Средний (functional break + log noise)
- **Решение**: использовать `self._get_http_timeout()` в TMDb-клиенте, как в базовом классе.
- **Статус**: [ ] Не исправлено

### SEC-25: SSRF — TOCTOU между _validate_download_url и push_release
- **Файл**: `bot/services/add_service.py:343-356, 495-508, 696-709`
- **Проблема**: `_validate_download_url` резолвит DNS у бота, но реальный запрос делает Radarr/Sonarr/Lidarr (или qBittorrent) — между этими событиями злоумышленник-индексер может изменить запись (DNS rebinding) или вернуть разные ответы для разных клиентов. Также `getaddrinfo` без `AI_NUMERICHOST|ALL` не покрывает IPv6 mapped IPv4 (`::ffff:127.0.0.1` — частично покрыто, но не все варианты). Validate-then-use — известная гонка.
- **Риск**: Средний (требует контроля за DNS — обычно только сам tracker)
- **Решение**:
  - указать в комментарии, что это best-effort валидация;
  - дополнительно отрезать запрос по протоколу/whitelist'у host'ов; либо передавать в Radarr только `magnet:` ссылки и блокировать `http(s)://` для непривилегированных индексеров.
- **Статус**: [ ] Не исправлено (документировать как known limitation)

### SEC-26: Inheritage от round2 — рейт-лимит `_user_requests` сбрасывается при рестарте
- **Файл**: `bot/middleware/auth.py:142`
- **Проблема**: ранее заведено как SEC-05 (round2) и помечено «принять как ограничение». Регрессий не появилось. Помечаю как существующее ограничение.
- **Риск**: Средний (whitelist mitigates)
- **Решение**: вынести в SQLite-таблицу `rate_limit(user_id, ts)` или Redis. Опционально.
- **Статус**: [ ] Не исправлено (deferred из round2)

## Низкие

### SEC-27: TMDb-прокси доверяется без валидации схемы/хоста
- **Файл**: `bot/config.py:65`, `bot/clients/tmdb.py:30-39`
- **Проблема**: `TMDB_PROXY_URL` читается из env и подставляется в `httpx.AsyncClient(proxy=self._proxy_url)` без какой-либо валидации. В deployment'е, где env-vars приходят из untrusted источника (например, чужой Portainer-stack), это может стать прокси-инъекцией с MITM на TMDb трафик. Применимо в основном в supply-chain-сценариях.
- **Риск**: Низкий (env-доверие = root-доверие)
- **Решение**: pydantic `field_validator` — проверить scheme∈{http,https,socks5} и netloc.
- **Статус**: [ ] Не исправлено

### SEC-28: docker-compose.dev.yml выставляет LOG_LEVEL=DEBUG без маскировки stdout
- **Файл**: `docker-compose.dev.yml:14-15`, `bot/main.py:22-25`
- **Проблема**: `_mask_tokens` в `setup_logging` маскирует только bot-token Telegram (`bot\d+:[A-Za-z0-9_-]{30,}`). API-ключи Prowlarr/Radarr/Sonarr/Lidarr/TMDb/Emby (типа 32-hex или произвольная строка) не маскируются. В DEBUG логах aiogram/httpx могут попадать заголовки или payload-snippets. Если `docker logs` доступен kubectl-/portainer-наблюдателям, ключи утекают. Регрессий относительно round2 нет, но ограничение фильтра остаётся.
- **Риск**: Низкий (dev-only, требует доступа к dev-логам)
- **Решение**:
  - расширить `_TOKEN_PATTERN` или добавить второй pass на 32+hex;
  - документировать «не запускать DEBUG в shared-окружении».
- **Статус**: [ ] Не исправлено

### SEC-29: Раздача данных через `bot/data` volume — права на хосте
- **Файл**: `Dockerfile:7-12`, `docker-compose.yml:42-43`
- **Проблема**: контейнер запускается как `botuser` UID 1000. `bot-data:/app/data` mount-ится как named volume — права создаёт docker, обычно `1000:1000`. Если хост-пользователь Anex имеет UID ≠ 1000, прямой доступ к sqlite-файлу может быть ограничен/расширен непредсказуемо. На Linux хосте без ACL bot.db (содержит сессии/историю) доступен любому, кто читает named-volume. Для homelab — ОК, но не documented.
- **Риск**: Низкий
- **Решение**: задокументировать модель прав в README; рассмотреть bind-mount с явным `:ro` после migration.
- **Статус**: [ ] Не исправлено

### SEC-30: HTML-инъекция через `Formatters.format_emby_status` — `lib.name`/`lib.collection_type`
- **Файл**: `bot/ui/formatters.py:723-765`
- **Проблема**: `lib.name` экранируется через `_e()` (ОК). Однако `lib.collection_type` сравнивается строкой и используется как ключ — не дисплеится. Безопасно. Дополнительная проверка: `_e(version)`, `_e(server_name)`, `_e(operating_system)` — всё через `_e()`. **Регрессий нет.** Помечаю проверку как пройденную.
- **Риск**: —
- **Решение**: —
- **Статус**: [x] OK (информационный пункт, не находка)

### SEC-31: ReDoS-риск в `prowlarr._parse_quality` и `parse_query`
- **Файл**: `bot/clients/prowlarr.py:201-306, 308-373`, `bot/services/search_service.py:233-256`
- **Проблема**: использованные регулярки относительно простые (`re.search(r"...")` без вложенных кванторов, без `(.+)+` и аналогов). Полиномиальная сложность отсутствует. Значимых alternations с overlapping тоже нет. Длина title ограничена API Prowlarr (обычно ≤ 500 символов). Запросы ограничены `MAX_QUERY_LENGTH=200` (search.py:132, music.py:99). Регрессий нет.
- **Риск**: —
- **Решение**: —
- **Статус**: [x] OK (информационный — проверено)

### SEC-32: SQL-инъекции в db.py
- **Файл**: `bot/db.py` целиком
- **Проблема**: все параметры передаются через `?`-плейсхолдеры (sqlite параметризованные запросы). Единственное место с f-string в SQL — `_set_schema_version` (line 161) `f"PRAGMA user_version = {v}"`, но `v` явно coerced в `int(version)` на строке 160. Регрессий нет.
- **Риск**: —
- **Решение**: —
- **Статус**: [x] OK

### SEC-33: Auth bypass / Callback injection
- **Файл**: `bot/middleware/auth.py`, `bot/handlers/*.py`
- **Проблема**: middleware-цепочка LoggingMiddleware → RateLimitMiddleware → AuthMiddleware применена и к `dp.message` и к `dp.callback_query` (main.py:219-225). `is_user_allowed` сверяет `user_id` с allowed/admin списком — корректно. CallbackQuery `data` парсится через `int(callback.data.removeprefix(...))` или `re/strip`-логику; некорректные значения отлавливаются `ValueError`/`show_alert`. Для админ-операций (`t_delete`, `t_delf`, `EMBY_RESTART_CONFIRM`, `EMBY_UPDATE_CONFIRM`) проверяется `is_admin=False` default. Регрессий нет.
- **Риск**: —
- **Решение**: —
- **Статус**: [x] OK

### SEC-34: Path traversal через DATABASE_PATH
- **Файл**: `bot/db.py:42-44`, `bot/config.py:74`
- **Проблема**: `database_path` без валидации (старый SEC-18). `Path(db_dir).mkdir(parents=True, exist_ok=True)` с пользовательским path. В контейнере `botuser` ограничен правами; за пределами контейнера env обычно trusted. Регрессий нет.
- **Риск**: Низкий (deferred из round2)
- **Решение**: pydantic-валидатор: запретить `..`, разрешить только относительные путь под `data/`.
- **Статус**: [ ] Не исправлено (deferred из round2)

### SEC-35: SearchSession schema-versioning
- **Файл**: `bot/db.py:349-381`, `bot/models.py:270-283`
- **Проблема**: SEC-19 из round2. Сейчас `get_session` ловит `Exception` при `model_validate` и удаляет сессию (line 379). Это безопасный fallback, не security-issue. Регрессий нет.
- **Риск**: —
- **Решение**: можно добавить `schema_version` для прозрачности, но не критично.
- **Статус**: [x] OK (мягко закрыт fallback'ом)

## Сводка

| ID      | Severity | Регрессия?           | Fix complexity |
|---------|----------|----------------------|----------------|
| SEC-20  | MED      | да (новое место SEC-02-pattern) | S |
| SEC-21  | MED      | новое                | S |
| SEC-22  | MED      | новое (непоследовательность) | S |
| SEC-23  | MED      | новое (непоследовательность) | S |
| SEC-24  | MED      | новое (functional + log)        | S |
| SEC-25  | MED      | known limitation     | M |
| SEC-26  | MED      | deferred из round2 (SEC-05) | M |
| SEC-27  | LOW      | новое                | S |
| SEC-28  | LOW      | partially из round2 (SEC-03) | S |
| SEC-29  | LOW      | новое (доку)         | S |
| SEC-34  | LOW      | deferred из round2 (SEC-18) | S |

- Всего находок (новых + deferred): **8 новых + 3 deferred = 11**
- Критических: **0**
- HIGH: **0** (все HIGH из round2 закрыты)
- MED: **6** (SEC-20..SEC-25, SEC-26 = deferred)
- LOW: **4** (SEC-27, SEC-28, SEC-29, SEC-34)
- Регрессий из round2: **0** (но 3 deferred остаются: SEC-05→SEC-26, SEC-03→SEC-28, SEC-18→SEC-34)
- Информационные/проверено OK: SEC-30, SEC-31, SEC-32, SEC-33, SEC-35

### Рекомендуемый приоритет фиксов

1. **SEC-24** (TMDb client breakage) — функциональный регресс, тривиальный фикс.
2. **SEC-20, SEC-21** — leak hostnames + потенциальный crash сообщения через HTML.
3. **SEC-22, SEC-23** — унификация error-handling через `Formatters.format_error`.
4. **SEC-25** — задокументировать TOCTOU как известное ограничение SSRF-защиты.
5. **SEC-27, SEC-28** — упрочнение боковых поверхностей.
