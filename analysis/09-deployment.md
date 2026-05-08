# Deployment TG_arr v1.0 (раунд 3)

Дата: 2026-05-08
Прошлый раунд: `analysis_round2/09-deployment.md` (16 находок DEPLOY-01..16, основные HIGH закрыты).
Целевая среда: rpie4 (Raspberry Pi 4, ARM64), Portainer stack #19, Docker Compose v2.

Пробежался по: `Dockerfile`, `docker-compose.yml`, `docker-compose.dev.yml`, `Makefile`, `.dockerignore`, `.env.example`, `README.md`, `bot/main.py`, `bot/clients/registry.py`, `bot/services/notification_service.py`, `pyproject.toml`, `requirements.txt`. CI/CD каталог `.github/` **отсутствует** — отдельная находка.

---

## Сводка

| Severity | Count | IDs |
|---|---|---|
| CRIT | 0 | — |
| HIGH | 4 | DEPLOY-01, DEPLOY-02, DEPLOY-03, DEPLOY-04 |
| MED  | 7 | DEPLOY-05..DEPLOY-11 |
| LOW  | 6 | DEPLOY-12..DEPLOY-17 |
| INFO | 3 | DEPLOY-18, DEPLOY-19, DEPLOY-20 |

Hot list (фиксить в первую очередь): **DEPLOY-01** (нет CI/CD), **DEPLOY-02** (multi-arch не зафиксирован), **DEPLOY-03** (нет TZ в образе → cron-like логика и логи дрейфуют), **DEPLOY-04** (`bot-data` named volume — ноль backup-pathway, риск потери `bot.db`).

---

## Высокие

### DEPLOY-01: Нет CI/CD (нет `.github/workflows/`)
- **Файл**: `.github/` отсутствует, README.md:107-112 ссылается на `https://github.com/Jkaotlic/TG_arr.git`
- **Проблема**: Нет автоматизированной сборки, lint, test, security-scan, multi-arch image push. Сборка идёт прямо на rpie4 через Portainer (`build: { context: . }`) — медленно (ARM64 Pi4 не та машина для билда), нет lockstep `image: ghcr.io/...:tag`, невозможно откатиться к предыдущему immutable образу. Любое изменение `requirements.txt` тянет полный rebuild без кеша слоёв (Pi теряет dockerd cache при перезапуске стека). Нет защиты от мёрджа сломанного `master` — текущий проект мёрджит прямо в master.
- **Решение**:
  1. `.github/workflows/ci.yml`: на push/PR — `ruff check`, `pytest`, `mypy`.
  2. `.github/workflows/release.yml`: на тег `v*` — `docker buildx build --platform linux/amd64,linux/arm64 --push -t ghcr.io/<owner>/tg-arr:<tag>`. В compose заменить `build:` → `image: ghcr.io/<owner>/tg-arr:${VERSION:-latest}`. Builder на GH Actions runner — секунды против минут на Pi.
  3. Добавить trivy/grype scan-step (security).
- **Статус**: [ ]

### DEPLOY-02: Multi-arch образ не верифицирован, ARM64 wheels не гарантированы
- **Файл**: `Dockerfile:1,6`
- **Проблема**: `python:3.12-slim` сам — multi-arch (manifest list). Но `pip install -r requirements.txt` на ARM64 пытается ставить wheels; для `pydantic-core` (Rust), `aiohttp`, `aiosqlite`, `httpx` они есть на PyPI, но **отсутствие** wheel приведёт к долгой сборке через `gcc` — а в slim runtime `gcc` нет, build упадёт. Builder-stage наследует `python:3.12-slim`, тоже без `gcc`. Сейчас ARM64 wheels всех этих пакетов есть, но это не зафиксировано: при апгрейде какой-нибудь будущей зависимости с C-extension сборка на Pi падёт без понятной диагностики. Также нет `piwheels` fallback'а.
- **Решение**: 
  1. В builder-stage временно ставить toolchain: `RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ libffi-dev && rm -rf /var/lib/apt/lists/*` — копировать только `/root/.local` в runtime (так уже сделано), gcc в финальный образ не попадает.
  2. Добавить в README раздел «Поддерживаемые платформы: linux/amd64, linux/arm64» с явным указанием.
  3. Вместе с DEPLOY-01: `docker buildx --platform linux/arm64` на CI как smoke-тест.
- **Статус**: [ ]

### DEPLOY-03: Нет `ENV TZ` в образе → системное время UTC, log-timestamp не совпадает с `TIMEZONE`
- **Файл**: `Dockerfile` (отсутствует), `docker-compose.yml:37`
- **Проблема**: `python:3.12-slim` не имеет `tzdata`, `/etc/localtime` указывает на UTC. App-level `TIMEZONE=Europe/Moscow` (config) используется только там, где код делает `ZoneInfo(settings.timezone)` — но `structlog.processors.TimeStamper(fmt="iso")` (main.py:83) пишет timestamp от системного `time.time()` в UTC. Поэтому JSON-лог в `docker logs` идёт в UTC, а пользовательские сообщения и календарь — в MSK. Расхождение 3 часа при дебаге инцидентов.
- **Решение**:
  ```dockerfile
  RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
      && rm -rf /var/lib/apt/lists/*
  ENV TZ=Europe/Moscow
  ```
  Либо (lighter) пробросить `/etc/localtime:/etc/localtime:ro` в compose. И `TimeStamper(fmt="iso", utc=False)` в structlog.
- **Статус**: [ ]

### DEPLOY-04: Named volume `bot-data` не имеет backup-pathway
- **Файл**: `docker-compose.yml:42-43,58-60`
- **Проблема**: `bot-data` — `driver: local`, по умолчанию лежит в `/var/lib/docker/volumes/<stack>_bot-data/_data` на rpie4. Содержит `bot.db` (SQLite со всеми пользователями, сессиями, историей). При:
  - случайном `docker volume rm` через Portainer UI;
  - переезде на новую SD-карту;
  - повреждении карты Pi4 (типичная проблема rpi4 + SDXC) —
  данные теряются. Нет backup-стратегии в README. SD-карты Pi4 умирают регулярно.
  Альтернатива — bind mount на host path (например `/srv/tgarr/data`) с регулярным rsync на NAS.
- **Решение**:
  1. README.md: раздел «Backup» с примером `docker run --rm -v <stack>_bot-data:/data -v $(pwd):/backup alpine tar czf /backup/tgarr-$(date +%F).tgz -C /data .`. Cron на rpi4.
  2. Для Portainer-стека: переключить на bind mount: `- /srv/tgarr-data:/app/data` и описать backup через restic/rsync.
  3. Добавить в DB self-backup endpoint (опционально): `/admin_backup` → отправляет `bot.db` админу в Telegram.
- **Статус**: [ ]

---

## Средние

### DEPLOY-05: HEALTHCHECK ловит only event-loop stalls, но не network/Telegram API failure
- **Файл**: `Dockerfile:19-20`, `bot/main.py:246-253`
- **Проблема**: `_liveness_touch` обновляет файл каждые 30s — это значит loop жив. Но сам polling `dp.start_polling()` может уйти в backoff на Telegram 429/5xx и часами не получать сообщений — для healthcheck это «здорово», для пользователя — мертво. Нет проверки, что был хотя бы один успешный `getUpdates` за окно времени (например, 5 мин).
- **Решение**: дополнительный sentinel-файл `/tmp/tgarr-polling-ok` обновляемый из middleware на любом входящем update **или** из патченного aiogram poll-loop. Если сейчас — слишком инвазивно — хотя бы log warning при `getUpdates` retry > N.
- **Статус**: [ ]

### DEPLOY-06: Resource limits 256M / 0.5 CPU — слишком тесно при `RESULTS_PER_PAGE` × poster-fetch + httpx pool
- **Файл**: `docker-compose.yml:52-56`
- **Проблема**: На Pi4 (ARM64) Python 3.12 RSS на старте уже ~70-90 MB. Каждый httpx AsyncClient держит pool. При параллельных запросах (poster-fetch + Prowlarr search + qBittorrent monitor + notification service) пиковая memory может уйти за 200 MB; OOMKill приведёт к рестарту и потере in-memory FSM. CPU 0.5 — `pydantic` валидация ответов Prowlarr (большие JSON) на Pi4 ARM 1.5 GHz при пиковом запросе пользователя добавляет латентность 200-400 ms. Лимиты `deploy.resources` работают **только** в Swarm-mode, и в standalone compose молча игнорируются (это уже отмечено в DEPLOY-08 раунда 2 как OK, но конкретные значения сомнительны).
- **Решение**: поднять лимиты до 512M / 1.0 CPU. Профилировать `docker stats tg-arr-bot` сутки и подобрать. Альтернатива: `mem_reservation: 128M` (soft) + `mem_limit: 512M` (hard) в compose-spec для standalone.
- **Статус**: [ ]

### DEPLOY-07: Graceful shutdown — `_liveness_touch` task не закрывается чисто
- **Файл**: `bot/main.py:255,275-277`
- **Проблема**: `liveness_task = asyncio.create_task(_liveness_touch())` отменяется в `finally` после возврата из `dp.start_polling`. Но `dp.shutdown.register(_on_shutdown)` aiogram вызывает в graceful path. Между ними: `liveness_task.cancel()` без `await liveness_task` (следующая строка `await bot.session.close()` — это другой объект). Cancel без await оставляет CancelledError unhandled до GC — лог-warning. Также: при SIGTERM от Docker → если `dp.start_polling` зависнет в `getUpdates` (httpx timeout 30s), `stop_grace_period: 30s` едва хватает, и при SIGKILL `_on_shutdown` не отработает: `db.close()` не вызовется, `notification_service.stop()` не дождётся cancel.
- **Решение**:
  ```python
  finally:
      liveness_task.cancel()
      try:
          await liveness_task
      except asyncio.CancelledError:
          pass
      await bot.session.close()
  ```
  Зарегистрировать `signal.SIGTERM` handler на уровне asyncio loop, который сначала останавливает polling, потом shutdown-handlers.
- **Статус**: [ ]

### DEPLOY-08: Watchdog `_liveness_watchdog` использует `os._exit(1)` — обходит aiogram shutdown
- **Файл**: `bot/main.py:40-65`
- **Проблема**: При срабатывании watchdog (loop stall > 120s) — `os._exit(1)` без `db.close()`, без `bot.session.close()`. SQLite WAL может остаться в неконсистентном состоянии (хотя aiosqlite-WAL обычно auto-recovers при следующем open). Httpx AsyncClient не закрыт — сокеты остаются TIME_WAIT, при перезапуске может возникнуть `Address already in use` для health-port (нет, у нас polling, не webhook). В целом приемлемо, т.к. цель — escape от deadlock'а, но: dump traceback **до** exit делается, ОК.
- **Решение**: добавить comment'ом, что это намеренно, чтобы будущие maintainers не пытались «починить» через `sys.exit`. Дополнительно — попробовать `signal.alarm(5)` перед `os._exit` для финального flush. Не критично.
- **Статус**: [ ]

### DEPLOY-09: docker-compose.dev.yml — нет защиты от случайного применения в проде
- **Файл**: `docker-compose.dev.yml:1-18`
- **Проблема**: Файл `docker-compose.dev.yml` не подхватывается автоматически compose'ом (нужен `-f`), это правильно. Но в Portainer при копировании stack из git кто-то может случайно загрузить **оба** файла. `LOG_LEVEL=DEBUG` (DEPLOY-05 round2 уже зафиксил) — в DEBUG mode `structlog.dev.ConsoleRenderer` (main.py:84) ставит non-JSON output → ломает любые log-aggregator'ы (Loki/Grafana). И bind mount `./bot:/app/bot:ro` на rpie4 не существует, контейнер упадёт.
- **Решение**: добавить header-comment в файл «DO NOT use in Portainer prod stack». В Makefile `docker-dev` target: `docker compose -f docker-compose.yml -f docker-compose.dev.yml up`.
- **Статус**: [ ]

### DEPLOY-10: Logging driver `json-file` без `compress: true`
- **Файл**: `docker-compose.yml:44-48`
- **Проблема**: `max-size: 10m × 3 = 30M` — OK, но `json-file` на Pi4 SD-card I/O — заметная нагрузка, плюс несжатые ротированные файлы. Можно добавить `compress: "true"` для ротированных.
- **Решение**:
  ```yaml
  logging:
    driver: "json-file"
    options:
      max-size: "10m"
      max-file: "3"
      compress: "true"
  ```
  Или перейти на `local` driver (binary, faster). Рассмотреть journald-driver для централизации.
- **Статус**: [ ]

### DEPLOY-11: `.dockerignore` пропускает `analysis_round2/`, `data/`
- **Файл**: `.dockerignore:15`
- **Проблема**: Строка `analysis/` исключает `analysis/`, но не `analysis_round2/`. При `COPY . .` (если когда-нибудь будет) — попадёт в образ. Сейчас Dockerfile делает только `COPY bot/ ./bot/` так что неактуально, но `.dockerignore` всё равно отправляет полный `build context` на Docker daemon — на Pi4 через сеть к Portainer это лишние мегабайты. Также: `data/` не в ignore, и `data/.gitkeep` копируется (DEPLOY-10 round2 — отмечен), но если в `data/` появятся реальные файлы (например, локальная БД при разработке) — они попадут в build context.
- **Решение**:
  ```
  analysis*/
  data/
  ```
  Плюс явно исключить `requirements-dev.txt`, `pyproject.toml` (если в образе не нужен — а сейчас не копируется), `*.log`, `Makefile`.
- **Статус**: [ ]

---

## Низкие

### DEPLOY-12: Нет `cap_drop: [ALL]` и `security_opt: [no-new-privileges:true]`
- **Файл**: `docker-compose.yml`
- **Проблема**: Контейнер запускается с default Linux caps (NET_BIND_SERVICE, SETUID, SETGID, и т.д.). Бот не нуждается ни в одной. Нет `no-new-privileges` — потенциальный sudo-escape если bug в зависимости.
- **Решение**:
  ```yaml
  cap_drop: [ALL]
  security_opt:
    - no-new-privileges:true
  ```
- **Статус**: [ ]

### DEPLOY-13: Нет `read_only: true` для rootfs + `tmpfs: /tmp`
- **Файл**: `docker-compose.yml`
- **Проблема**: Дубль из round2 DEPLOY-13. Liveness-файл `/tmp/tgarr-alive` требует writable `/tmp`. Решается через `tmpfs`. Не критично — в проекте всё равно non-root user.
- **Решение**:
  ```yaml
  read_only: true
  tmpfs:
    - /tmp:size=64m
  volumes:
    - bot-data:/app/data
  ```
- **Статус**: [ ]

### DEPLOY-14: `restart: unless-stopped` — нет limit на crash-loop
- **Файл**: `docker-compose.yml:7`
- **Проблема**: При фатальной ошибке (bad token, all *arr unreachable) бот будет циклически рестартовать каждые секунды, сжигая ресурсы Pi4 и забивая логи. Дубль DEPLOY-15 round2.
- **Решение**: standalone compose не поддерживает `max_attempts`. Можно `restart: on-failure:5`, но потеряем auto-restart по разлогину Pi4. Альтернатива — мониторинг через portainer + ручной alert. Оставить как есть, но описать в README.
- **Статус**: [ ]

### DEPLOY-15: `image:` тэг отсутствует — связан с DEPLOY-01 (CI/CD)
- **Файл**: `docker-compose.yml:2-6`
- **Проблема**: Дубль DEPLOY-11 round2. Без `image:` каждый build — анонимный layer-set с именем `<stack>-tg-arr-bot:latest`. Невозможно сделать `docker pull` previous-version при rollback. Сейчас «откат» = ручной `git checkout <prev-commit> && portainer redeploy`.
- **Решение**: связать с DEPLOY-01 — добавить `image: ghcr.io/<owner>/tg-arr:${VERSION:-latest}` после введения registry push.
- **Статус**: [ ]

### DEPLOY-16: Python 3.12 vs 3.13 — миграция целесообразна, но не срочно
- **Файл**: `Dockerfile:1,6`, `pyproject.toml:10`
- **Проблема**: Python 3.13 (released Oct 2024, текущая stable 3.13.x) даёт ~5-10% speed-up за счёт experimental JIT (--enable-experimental-jit), free-threaded (PEP 703). Для bot'а на Pi4 это заметно. `aiogram>=3.20` в pyproject.toml совместим с 3.13. `pydantic` 2.12.x — да. `aiosqlite` 0.22 — да. Риски: `structlog 25.x` — проверить, `tenacity` 9 — да.
- **Решение**: на следующий major release — `FROM python:3.13-slim`, `requires-python = ">=3.13"`. До этого: ничего.
- **Статус**: [ ]

### DEPLOY-17: Нет SIGUSR1-handler для py-spy / runtime profiling
- **Файл**: `bot/main.py`
- **Проблема**: При зависании в проде нет способа дампа стэка по запросу (только watchdog, который убивает). Хорошо бы `signal.SIGUSR1` → `faulthandler.dump_traceback(file=sys.stderr, all_threads=True)` без exit. py-spy на Pi4 ARM64 можно ставить через `pip`, но требует `--cap-add SYS_PTRACE` для контейнера.
- **Решение**:
  ```python
  faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True)
  ```
  Использование: `docker exec tg-arr-bot kill -SIGUSR1 1`.
- **Статус**: [ ]

---

## Info / OK

### DEPLOY-18 (OK): Multi-stage build корректный
`Dockerfile:1-3` builder-stage с `pip install --user`, копирование `/root/.local` → `/home/botuser/.local` в runtime. PATH правильный. ✅

### DEPLOY-19 (OK): non-root user, /app/data ownership
`useradd -m -u 1000 botuser`, `mkdir /app/data && chown -R botuser:botuser /app`, `COPY --chown`. UID 1000 совпадает с типичным host-user на Pi4 — bind mount будет работать. ✅

### DEPLOY-20 (OK): Env-var hygiene в compose
`${TELEGRAM_BOT_TOKEN:?Set TELEGRAM_BOT_TOKEN}` для required, `${VAR:-default}` для optional. ✅ Хорошая дисциплина.

### Network (info)
В compose **нет** `networks:` секции — стек присоединяется к default bridge. Чтобы достучаться до radarr/sonarr/prowlarr/qbit/emby по DNS-именам, они должны быть в **той же** docker network. На rpie4 это, вероятно, общая `media` или `arr` сеть, поднимается отдельным стеком. **Нужно явно указать в compose**:
```yaml
services:
  tg-arr-bot:
    networks: [arr-net]
networks:
  arr-net:
    external: true
    name: arr-network  # имя реальной сети на rpie4
```
Иначе после redeploy стек может попасть на default bridge, и `http://prowlarr:9696` не зарезолвится. Это **MED** на самом деле, но без знания имени сети на rpie4 — выношу в info. **Нужно проверить вручную в Portainer**.

### Portainer stack — env через UI
README.md:174-176 описывает: «В Portainer создайте Stack, вставьте содержимое docker-compose.yml и добавьте переменные окружения в секции **Environment variables**». OK. `${VAR:?...}` валидируется compose-парсером Portainer на deploy-step, ошибки понятны. ✅

---

## Приоритизация (рекомендуемый порядок фиксации)

1. **DEPLOY-04** — backup-стратегия для `bot-data` (1 час, критично из-за SD-card на Pi4)
2. **DEPLOY-01** — CI/CD `.github/workflows/` + GHCR push (полдня)
3. **DEPLOY-03** — TZ в Dockerfile (15 мин)
4. **DEPLOY-02** — multi-arch verification + buildx (после DEPLOY-01)
5. **DEPLOY-07** — graceful shutdown corrections (30 мин)
6. **DEPLOY-12, DEPLOY-13** — security hardening (15 мин, batch)
7. **DEPLOY-09, DEPLOY-10, DEPLOY-11** — компоновочные мелочи
8. Network section в compose — ручная проверка на rpie4 + явное декларирование

Не фиксить: DEPLOY-14 (`unless-stopped` устраивает), DEPLOY-16 (3.13 преждевременно), DEPLOY-17 (nice-to-have).
