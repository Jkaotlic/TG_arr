# Анализ деплоя TG_arr (раунд 5)

Прочитаны полностью: `Dockerfile`, `docker-compose.yml`, `docker-compose.dev.yml`, `Makefile`, `.dockerignore`, `.env.example`, `bot/main.py`, `bot/config.py` (+ выборочно `.gitignore`, grep по использованию timezone). Прод-факты (healthy, 117/256MiB, RestartCount=0) учтены.

## Критические

Не обнаружено. Прод-конфигурация в целом зрелая: пиненый digest, multi-stage, non-root, осмысленный healthcheck + watchdog, ротация логов, graceful shutdown, лимиты ресурсов.

## Средние

### DEPLOY-01: Compose-allowlist переменных дрейфует от config.py — webhook невозможно включить по документации
- **Файл**: `docker-compose.yml:8-47`, `bot/config.py:84-100`, `.env.example:67-73`
- **Проблема**: compose передаёт env через явный allowlist в `environment:`, а `.env` в образ не попадает (`.dockerignore:2`) и не монтируется — pydantic внутри контейнера видит только то, что проброшено. Не проброшены: `WEBHOOK_ENABLED`, `WEBHOOK_PORT`, `WEBHOOK_BIND`, `PROWLARR_SEARCH_TIMEOUT`, `PROWLARR_SEARCH_RETRIES`, `DATABASE_PATH`. Секции `ports:` в compose тоже нет (что при выключенном webhook правильно — наружу ничего не торчит, это подтверждено).
- **Риск**: `.env.example:67-73` инструктирует «раскомментируй `WEBHOOK_ENABLED=true`» — пользователь сделает это на Pi, и **молча ничего не произойдёт**: переменная не дойдёт до контейнера, порт не опубликован. Аналогично тюнинг `PROWLARR_SEARCH_TIMEOUT/RETRIES` — тихий no-op. Классическая ловушка «поменял конфиг — эффекта нет».
- **Решение**: добавить в `environment:` три `WEBHOOK_*` и два `PROWLARR_SEARCH_*` (по образцу `${VAR:-default}`); для webhook — закомментированный блок `ports: ["8090:8090"]` с пояснением. Либо перейти на `env_file: .env` + оставить `:?`-проверки только для обязательных.
- **Статус**: [ ] Не исправлено

### DEPLOY-02: В Makefile нет пути деплоя на Pi и нет отката
- **Файл**: `Makefile:49-63`
- **Проблема**: целей деплоя нет вообще — только локальные `docker-build` / `docker-up`. Деплой на Pi выполняется руками (ssh → git pull → build → up), нигде не кодифицирован. `docker-up` — это `docker compose up -d` **без** `--build`: забыл `make docker-build` — перезапустил старый образ и не заметил. Отката нет: `compose build` перетегивает образ, предыдущий становится dangling-безымянным — вернуться на него можно только раскопками `docker images -a`. Мелочь: `docker-restart` отсутствует в `.PHONY` (`Makefile:1`).
- **Риск**: недетерминированный деплой «по памяти»; при сломанной сборке/регрессии на Pi нет однокомандного возврата. Сборка на самом Pi (ARM, SD-карта) медленная — каждая ошибка стоит минут.
- **Решение**: цель `deploy`: `git pull && docker compose build && docker tag <img>:latest <img>:prev && docker compose up -d && docker compose ps` (build до up — образ проверяется до рестарта, `&&` даёт fail-fast); цель `rollback`: перетег `prev`→`latest` + `up -d`. Опционально: cross-сборка `buildx --platform linux/arm64` на десктопе + перенос образа, чтобы не собирать на Pi.
- **Статус**: [ ] Не исправлено

### DEPLOY-03: docker-compose.dev.yml — ложный комментарий про автозагрузку, standalone-режим неработоспособен
- **Файл**: `docker-compose.dev.yml:2, 14-15, 19-24`
- **Проблема**: комментарий «This file is automatically loaded by docker compose» неверен — автоматически подхватывается только `docker-compose.override.yml`; этот файл требует явного `-f docker-compose.yml -f docker-compose.dev.yml`. Хуже: комментарий DEPLOY-05 (строки 19-21) обещает, что standalone `docker compose -f docker-compose.dev.yml up` работает благодаря объявленному volume — но в standalone-режиме из `environment` задан только `LOG_LEVEL=DEBUG`, обязательные `TELEGRAM_BOT_TOKEN`/`PROWLARR_*`/… не проброшены, `.env` внутрь не попадает → pydantic `ValidationError` и crash-loop на старте.
- **Риск**: только dev-воркфлоу (прод не задет), но файл активно вводит в заблуждение двумя способами сразу.
- **Решение**: либо переименовать в `docker-compose.override.yml` (тогда первый комментарий станет правдой — но тогда он начнёт грузиться и на Pi, осторожно), либо исправить комментарии на честное `docker compose -f docker-compose.yml -f docker-compose.dev.yml up` и убрать претензию на standalone (или добавить полный проброс env).
- **Статус**: [ ] Не исправлено

## Низкие

### DEPLOY-04: Паттерны .dockerignore не матчат вложенные __pycache__/*.pyc
- **Файл**: `.dockerignore:6-7`
- **Проблема**: в `.dockerignore` (в отличие от `.gitignore`) паттерн без `**/` матчится только относительно корня контекста. `__pycache__` и `*.pyc` не исключают `bot/__pycache__/` — а он **реально существует** в рабочей копии (38 файлов `*.cpython-314.pyc` от локального Python 3.14).
- **Риск**: при сборке из грязной рабочей копии (на десктопе) мусорные .pyc попадут в образ через `COPY bot/ ./bot/`. На Pi при сборке из git-клона чисто (`.gitignore:2-3` покрывает), поэтому severity низкая — но это скрытая мина для buildx-сборки с десктопа (см. DEPLOY-02).
- **Решение**: заменить на `**/__pycache__` и `**/*.pyc` (заодно `**/.pytest_cache` и т.п.).
- **Статус**: [ ] Не исправлено

### DEPLOY-05: read_only rootfs возможен, но не включён; код в образе принадлежит runtime-пользователю
- **Файл**: `docker-compose.yml:48-64`, `Dockerfile:21-22`
- **Проблема**: контейнеру на запись нужны только `/app/data` (уже volume) и `/tmp/tgarr-alive` (liveness-файл, `bot/main.py:294-303`). Условия для `read_only: true` + `tmpfs: [/tmp]` выполнены, но не включено. Дополнительно: `chown -R botuser:botuser /app` + `COPY --chown=botuser` делают код `/app/bot` записываемым для runtime-пользователя. Нет `cap_drop: [ALL]` / `security_opt: [no-new-privileges:true]`.
- **Риск**: defense-in-depth: при компрометации бота процесс может модифицировать собственный код в работающем контейнере. Практическая эксплуатируемость низкая (LAN, allowlist пользователей).
- **Решение**: в compose — `read_only: true`, `tmpfs: [/tmp]`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`; в Dockerfile — `COPY bot/` без `--chown` (root-owned код), chown оставить только на `/app/data`.
- **Статус**: [ ] Не исправлено

### DEPLOY-06: .env.example неполон — нет PROWLARR_SEARCH_TIMEOUT / PROWLARR_SEARCH_RETRIES
- **Файл**: `.env.example`, `bot/config.py:84-91`
- **Проблема**: сверка с полями `Settings`: отсутствуют только `PROWLARR_SEARCH_TIMEOUT` и `PROWLARR_SEARCH_RETRIES` (тюнинг, специально задокументированный в config.py под rpie4). Всё остальное на месте; реальных секретов в файле нет — только плейсхолдеры.
- **Риск**: пользователь не узнает о тюнинге без чтения кода. (Учесть вместе с DEPLOY-01 — их же надо и в compose.)
- **Решение**: добавить закомментированные строки с дефолтами `# PROWLARR_SEARCH_TIMEOUT=25.0`, `# PROWLARR_SEARCH_RETRIES=1`.
- **Статус**: [ ] Не исправлено

### DEPLOY-07: Комментарий в Dockerfile про structlog-таймстемпы неточен (логи — в UTC)
- **Файл**: `Dockerfile:11-12`, `bot/main.py:83`
- **Проблема**: комментарий утверждает, что tzdata заставит «structlog timestamps match TIMEZONE=Europe/Moscow». Фактически `structlog.processors.TimeStamper(fmt="iso")` по умолчанию `utc=True` — таймстемпы логов идут в UTC независимо от TZ. Сам tzdata при этом **действительно нужен** — для `ZoneInfo(settings.timezone)` в `bot/ui/formatters.py:968-972` (показ времени пользователю).
- **Риск**: только когнитивный — при чтении `docker logs` время «отстаёт» на 3 часа от ожиданий по комментарию. UTC в логах — это, вообще-то, хорошо.
- **Решение**: поправить комментарий (tzdata — ради ZoneInfo/formatters и `datetime.now()`), либо, если хочется MSK в логах, `TimeStamper(fmt="iso", utc=False)`.
- **Статус**: [ ] Не исправлено

## Проверено — проблем нет

- **Dockerfile, структура**: multi-stage (builder + runtime); `requirements.txt` копируется до кода — кэш слоёв работает (`Dockerfile:7-8, 22`); `pip install --no-cache-dir` есть; база запинена по multi-arch digest (python 3.12.13-slim-trixie — совпадает с прод-фактом Python 3.12.13), рядом инструкция по обновлению digest.
- **HEALTHCHECK осмысленный, не «процесс жив»**: проверяет свежесть `/tmp/tgarr-alive`, который обновляет asyncio-таск каждые 30с (`bot/main.py:294-303`) — детектит зависший event loop. Важный нюанс (не баг): unhealthy сам по себе контейнер не перезапускает — реальное самолечение обеспечивает независимый watchdog-тред с `os._exit(1)` (`bot/main.py:40-65`) + `restart: unless-stopped`. Связка спроектирована корректно.
- **Graceful shutdown — полный**: exec-form `CMD` → python получает SIGTERM как PID 1; aiogram `start_polling` ловит SIGTERM/SIGINT по умолчанию (`handle_signals=True`); `on_shutdown` (`bot/main.py:184-203`) останавливает notification service, закрывает **все** httpx-клиенты через registry (`close_all_clients`, single-close гарантирован — RACE-05) и БД; `finally` (`bot/main.py:344-349`) гасит liveness/cleanup-таски, webhook runner и `bot.session`. `stop_grace_period: 30s` в compose (`docker-compose.yml:55`) — запас достаточный. (Микронюанс: `cancel()` без `await`, добирает `asyncio.run` — не проблема.)
- **Логи не съедят SD-карту**: json-file `max-size: 10m`, `max-file: 3` → потолок ~30MB (`docker-compose.yml:50-54`).
- **Ресурсы**: `deploy.resources.limits` 256M / 0.5 CPU — реально enforced под Compose V2, подтверждено прод-фактом (117/256MiB).
- **Webhook-порт не торчит наружу**: секции `ports:` в compose нет вообще — при выключенном webhook опубликованных портов нет (сам проброс при включении — см. DEPLOY-01).
- **Секреты**: `.env.example` — только плейсхолдеры; `.env`/`.env.*` исключены и из образа (`.dockerignore:2-3`), и из git (`.gitignore:54`); обязательные переменные fail-fast через `${VAR:?}` (`docker-compose.yml:10-16`).
- **TZ vs TIMEZONE — обе используются, рассинхрона нет**: `TZ` → glibc/`datetime.now()` (локальное время процесса), `TIMEZONE` → `settings.timezone` → `ZoneInfo` в `bot/ui/formatters.py:968`. В compose обе выводятся из одной переменной `${TIMEZONE:-Europe/Moscow}` (`docker-compose.yml:41-43`) — единый источник истины; `ENV TZ` в Dockerfile — лишь дефолт, compose его переопределяет.
- **Non-root**: `useradd botuser` + `USER botuser`, PATH на `~/.local/bin` — совпадает с прод-фактом.
- **Контекст сборки**: `.git`, `tests/`, `analysis/`, `.claude/` (включая тяжёлые worktree-копии репо) исключены из контекста; в образ попадает только `bot/` + зависимости.

**Итог**: критических проблем нет, прод стабилен. Главные точки роста — проброс env-переменных в compose синхронно с config.py (DEPLOY-01, иначе webhook «не включается») и кодификация деплоя/отката в Makefile (DEPLOY-02).
