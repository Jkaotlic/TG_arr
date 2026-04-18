# Deployment Audit — TG_arr

Дата: 2026-04-18.

## DEPLOY-01 — Dockerfile: non-root user ✅ (OK)

Файл: `Dockerfile:19-21` — `useradd -m -u 1000 botuser`, `USER botuser`. Корректно.

## DEPLOY-02 — Dockerfile: НЕТ multi-stage build (MED)

Файл: `Dockerfile:1-30`
Один stage `python:3.12-slim` с `gcc` установленным и не удалённым. Builder-stage + runtime-stage даст меньший image size. Плюс `.pyc`-файлы не pre-generated → cold-start дольше.

**Решение:**
```dockerfile
FROM python:3.12-slim AS builder
RUN pip install --prefix=/install -r requirements.txt

FROM python:3.12-slim
COPY --from=builder /install /usr/local
# copy bot/ ...
```

## DEPLOY-03 — `gcc` в финальном образе (DEAD-27, LOW)

Файл: `Dockerfile:6-8`
Если wheels достаточно (для Python 3.12 slim + наши deps — да), можно убрать. -150MB image.

## DEPLOY-04 — HEALTHCHECK не проверяет polling (SEC-14, HIGH)

Файл: `Dockerfile:27-28`
Дубликат из security. Проверяет только import settings, polling-deadlock не отлавливает. **Повторяю как deployment-issue.**

## DEPLOY-05 — `docker-compose.override.yml` монтируется в проде (HIGH)

Файл: `docker-compose.override.yml:1-17`
```yaml
volumes:
  - ./bot:/app/bot:ro
environment:
  - LOG_LEVEL=DEBUG
```
Docker Compose автоматически мёрджит override. На prod стеке (Portainer), если `./bot` присутствует — override применится, код в образе заменится host-volume'ом. При деплое через Portainer stack **override файл не используется**, т.к. Portainer deploy по имени stack обычно тянет из git/compose-inline без override.

Однако: **если** override случайно оказался в stack-deploy, это:
1. Перетирает код из образа host-volume'ом (которого нет в Portainer host'е).
2. Включает DEBUG логи → token-leak (см. SEC-03).

**Решение:** либо переименовать в `docker-compose.dev.yml` (не автоподхватывается), либо удалить.

## DEPLOY-06 — `drop_pending_updates=True` (BUG-20, MED)

См. `02-bugs.md:BUG-20`. Deploy-relevant, т.к. каждый redeploy теряет queued messages.

## DEPLOY-07 — Graceful shutdown: NotificationService cancel при SIGTERM (MED)

Файлы: `bot/main.py:86-108`, `bot/services/notification_service.py:78-90`
`on_shutdown` вызывает `notification_service.stop()`. Хорошо. Но aiogram `dp.shutdown.register(_on_shutdown)` срабатывает **только** при graceful stop (Ctrl+C / signal). Docker SIGTERM ловится, но если внутри `_monitor_loop` блокирует в `qbt.get_torrents()` на 60s — shutdown timeout (по умолчанию docker stop → 10s → SIGKILL). NotificationTask будет killed.

**Решение:** `docker-compose.yml` добавить `stop_grace_period: 30s`. И/или `_monitor_loop` использовать `asyncio.wait_for(..., timeout=10)` на каждой итерации.

## DEPLOY-08 — Resource limits (INFO, OK)

Файл: `docker-compose.yml:46-50`
```yaml
deploy:
  resources:
    limits:
      memory: 256M
      cpus: '0.5'
```
Только в Swarm-mode. В standalone compose игнорируется — но Portainer на Swarm у вас.
Комментарий в файле это объясняет. **OK.**

## DEPLOY-09 — `.dockerignore` покрывает нужное (OK, но есть один пробел)

Файл: `.dockerignore`
Покрывает `tests/`, `.env`, `.env.*` (кроме `.env.example`), `__pycache__`, `*.md`, `.pytest_cache`, `analysis/`, `.claude/`.

**Пропуски:**
- `*.db`, `*.sqlite` — нет (могут попасть в image при `COPY data/.gitkeep ./data/`)
- `Dockerfile*.bak`, `.git`

`.git` есть — ok. `*.db` не явно, но т.к. мы копируем только `bot/` и `data/.gitkeep`, не проблема.

## DEPLOY-10 (НОВЫЙ) — `data/.gitkeep` копируется, но `data/` потом примонтируется volume'ом (LOW)

Файл: `Dockerfile:15-16`
```
COPY bot/ ./bot/
COPY data/.gitkeep ./data/
```
В `docker-compose.yml`:
```
volumes:
  - bot-data:/app/data
```
Volume `bot-data` **замещает** `/app/data` из образа. `.gitkeep` никогда не видно на prod. Не критично, но COPY избыточен.

## DEPLOY-11 (НОВЫЙ) — Нет `image:` тэгирования (LOW)

Файл: `docker-compose.yml:2-6`
```yaml
tg-arr-bot:
  build:
    context: .
    dockerfile: Dockerfile
  container_name: tg-arr-bot
```
Нет `image: tg-arr-bot:1.0.0` — каждый build — local image, нельзя pull с registry. Для Portainer-стэка на rpi4 builder tooling нужен. А если использовать pre-built, то `image: ghcr.io/user/tg-arr-bot:latest`.
**Решение:** `image: tg-arr-bot:${VERSION:-latest}`.

## DEPLOY-12 (НОВЫЙ) — Нет `docker logs`-ротации (MED)

`docker-compose.yml` не задаёт `logging` driver/opts. По умолчанию json-file без rotation → на долго-живущих контейнерах логи заполняют диск.
**Решение:**
```yaml
logging:
  driver: "json-file"
  options:
    max-size: "10m"
    max-file: "3"
```

## DEPLOY-13 (НОВЫЙ) — Нет read_only для container rootfs (LOW)

Docker best-practice: `read_only: true` с `tmpfs` для `/tmp`. Код в `/app` не меняется после build. Защищает от runtime-mutation.
**Решение:**
```yaml
read_only: true
tmpfs:
  - /tmp
volumes:
  - bot-data:/app/data
```

## DEPLOY-14 (НОВЫЙ) — Нет `security_opt` / `cap_drop` (LOW)

Контейнер запускается с default caps. Для bot'а без privileged-operations можно `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`.

## DEPLOY-15 (НОВЫЙ) — Нет restart-on-failure-limit (LOW)

Файл: `docker-compose.yml:7`
`restart: unless-stopped` — OK, но при crash-loop непрерывно рестартует. Лучше `restart: on-failure:5` или в Swarm `deploy.restart_policy.condition: on-failure; max_attempts: 5`.

## DEPLOY-16 (НОВЫЙ) — `TMDB_PROXY_URL` не в `.env.example` секциях явно выделен (INFO)

Файл: `.env.example:45`
`# TMDB_PROXY_URL=http://your-vps:8899  # HTTP proxy for TMDb (if geo-blocked)` — закомментирован. OK.

## Итого

HIGH: DEPLOY-04 (дубл. SEC-14), DEPLOY-05
MED: DEPLOY-02, DEPLOY-06 (дубл. BUG-20), DEPLOY-07, DEPLOY-12
LOW: DEPLOY-03, DEPLOY-10, DEPLOY-11, DEPLOY-13, DEPLOY-14, DEPLOY-15
INFO: DEPLOY-01, DEPLOY-08, DEPLOY-09, DEPLOY-16

Приоритет:
1. DEPLOY-05 — удалить или переименовать override в `.dev.yml` (prod safety)
2. DEPLOY-04 — реальный liveness healthcheck
3. DEPLOY-02 — multi-stage (performance, image size)
4. DEPLOY-12 — log rotation (disk safety)
