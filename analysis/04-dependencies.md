# Dependencies TG_arr v1.0 (раунд 3)

Дата: 2026-05-08. Источник версий: `pip index versions <pkg>` (PyPI mirror).
Прошлый раунд: `analysis_round2/04-dependencies.md` (DEP-01..DEP-12).

## Обзор: текущее vs. актуальные версии

### Production (`requirements.txt`)

| Пакет              | Текущая  | pyproject.toml constraint | Latest на PyPI | Δ        | Совместимость с pydantic<2.13 |
|--------------------|----------|---------------------------|----------------|----------|--------------------------------|
| aiogram            | 3.27.0   | `>=3.20,<4`               | **3.27.0**     | актуально | требует pydantic<2.13          |
| httpx              | 0.28.1   | `>=0.28,<1`               | 0.28.1         | актуально | independent                    |
| tenacity           | 9.1.4    | `>=9.0,<10`               | 9.1.4          | актуально | independent                    |
| pydantic           | 2.12.5   | `>=2.9,<3`                | **2.13.4**     | minor (заблокировано) | блок aiogram 3.27 |
| pydantic-settings  | 2.13.1   | `>=2.6,<3`                | **2.14.1**     | minor (зависит от pydantic) | проверить совместимость |
| aiosqlite          | 0.22.1   | `>=0.20,<2`               | 0.22.1         | актуально | independent                    |
| structlog          | 25.5.0   | `>=24.4,<26`              | 25.5.0         | актуально | independent                    |

### Dev (`requirements-dev.txt`)

| Пакет           | Текущая | pyproject.toml | Latest    | Δ                     |
|-----------------|---------|----------------|-----------|-----------------------|
| pytest          | 9.0.2   | `>=8.3`        | **9.0.3** | patch                 |
| pytest-asyncio  | 1.3.0   | `>=0.24`       | 1.3.0     | актуально             |
| pytest-cov      | 7.1.0   | `>=5.0`        | 7.1.0     | актуально             |
| ruff            | 0.14.6  | `>=0.7`        | **0.15.12** | minor (актив. развит.) |
| mypy            | 1.18.2  | `>=1.13`       | **2.0.0** | major bump            |

## Обновления доступны

### DEP-01: aiogram 3.27.0 актуальна (INFO)
- **Файл**: `requirements.txt:6`, `pyproject.toml:16`
- **Статус**: latest. Constraint `<4` корректен.
- **Совместимость**: 3.27 требует `pydantic<2.13` — это и есть причина пинов pydantic.
- **Решение**: ничего не делать; следить за выходом 3.28+ (есть шанс снять блок pydantic 2.13).
- **Статус**: [x] актуально

### DEP-02: pydantic 2.12.5 → 2.13.4 ЗАБЛОКИРОВАНО (INFO, was DEP-05 R2)
- **Файл**: `requirements.txt:14`
- **Текущая**: 2.12.5
- **Доступная**: 2.13.4 (но 2.13.x несовместим с aiogram 3.27 — см. commit `2a51a2b`).
- **Совместимость**: ❌ нельзя обновить, пока `aiogram<3.28` (если 3.28 выйдет с поддержкой).
- **Решение**: оставить пин 2.12.5; добавить комментарий с датой ревью (есть, актуален).
- **Статус**: [x] заблокировано aiogram 3.27

### DEP-03: pydantic-settings 2.13.1 → 2.14.1 (LOW, требует валидации)
- **Файл**: `requirements.txt:15`
- **Текущая**: 2.13.1
- **Доступная**: 2.14.1
- **Совместимость**: pydantic-settings 2.14.x **тянет pydantic>=2.13** в качестве hard requirement (по changelog 2.14.0, "Bump min pydantic"). Если так — заблокировано вместе с pydantic.
- **Решение**: НЕ обновлять до проверки `pip install pydantic-settings==2.14.1 pydantic==2.12.5` в чистом venv (или чтения metadata.requires_dist на pypi). Если требование `pydantic>=2.13` — отложить до aiogram 3.28.
- **Статус**: [ ] требует ручной проверки совместимости

### DEP-04: ruff 0.14.6 → 0.15.12 (LOW)
- **Файл**: `requirements-dev.txt:7`
- **Текущая**: 0.14.6
- **Доступная**: 0.15.12 (стабильный релиз 0.15.x)
- **Совместимость**: dev-only, никак не влияет на runtime / pydantic. 0.15 содержит новые правила и stylechecks, может выявить новые предупреждения.
- **Решение**: обновить до 0.15.12; прогнать `ruff check`, зафиксировать новые ошибки (либо `ignore`, либо fix).
- **Статус**: [ ] safe to bump

### DEP-05: mypy 1.18.2 → 2.0.0 (MED, breaking)
- **Файл**: `requirements-dev.txt:8`
- **Текущая**: 1.18.2
- **Доступная**: 2.0.0 (major)
- **Совместимость**: dev-only; 2.0.0 имеет breaking changes (см. mypy 2.0 changelog — strict-mode по умолчанию изменения, deprecation legacy semantics). Реальный риск — много новых ошибок.
- **Решение**: НЕ торопиться. Можно остаться на 1.20.2 (latest 1.x) как safe minor bump, либо мигрировать на 2.0 отдельной задачей.
- **Статус**: [ ] остаться на 1.20.2 или отложить миграцию 2.0

### DEP-06: pytest 9.0.2 → 9.0.3 (INFO)
- **Файл**: `requirements-dev.txt:4`
- **Текущая**: 9.0.2 → 9.0.3 (patch)
- **Совместимость**: dev-only, безопасно.
- **Решение**: bump в плановом порядке.
- **Статус**: [ ] safe patch

### DEP-07: httpx / tenacity / aiosqlite / structlog — актуальны (INFO)
- httpx 0.28.1 — last release 2024; следующий мажор не анонсирован.
- tenacity 9.1.4 — last release актуальна.
- aiosqlite 0.22.1 — актуальна, upper-bound `<2` уже добавлен (DEP-07 R2 закрыт).
- structlog 25.5.0 — актуальна, upper-bound `<26` уже добавлен (DEP-06 R2 закрыт).
- **Статус**: [x] актуально

## Несоответствия pyproject ↔ requirements

### DEP-08: pyproject.toml `[dev]` extra ≠ `requirements-dev.txt` (MED, унаследовано из R2 как DEP-10)
- **Файлы**: `pyproject.toml:25-32` vs `requirements-dev.txt:4-8`
- **Расхождение**: `pyproject.toml [dev]` содержит расслабленные диапазоны (`pytest>=8.3`, `mypy>=1.13`); `requirements-dev.txt` — жёсткие пины. Два source-of-truth.
- **Влияние**: `pip install -e ".[dev]"` поставит другие версии, чем `pip install -r requirements-dev.txt`. Воспроизводимость dev-окружения нарушена.
- **Решение**: выбрать одно. Рекомендую оставить `requirements-dev.txt` как реальный пин и удалить `[project.optional-dependencies].dev` (или сделать его минимальным `extras_require` для документации).
- **Статус**: [ ] не закрыт с R2

### DEP-09: pyproject `pydantic>=2.9,<3` не отражает блокировку <2.13 (LOW)
- **Файл**: `pyproject.toml:19`
- **Проблема**: constraint позволяет 2.13.x, который сломает aiogram 3.27. Единственная защита — пин в `requirements.txt`. При установке через `pip install -e .` без `requirements.txt` пользователь получит broken setup.
- **Решение**: в `pyproject.toml` ужесточить: `"pydantic>=2.9,<2.13"`. Пометить TODO снять, когда aiogram 3.28 разрешит pydantic 2.13.
- **Статус**: [ ] новый

### DEP-10: Makefile `dev` target ставит лишь `ruff mypy`, без pytest (LOW, унаследовано из R2 как DEP-11)
- **Файл**: `Makefile:25-27`
- **Проблема**:
  ```
  dev:
      pip install -r requirements.txt
      pip install ruff mypy
  ```
  Не ставит pytest/pytest-asyncio/pytest-cov; `make test` падает на чистом окружении.
- **Решение**: заменить на `pip install -r requirements.txt -r requirements-dev.txt`.
- **Статус**: [ ] не закрыт с R2

## Лишние / неиспользуемые зависимости

### DEP-11: Все production-deps используются (INFO)
Грепом по `bot/` подтверждено, что каждая из 7 production-зависимостей фактически импортируется:
- `aiogram` — 14 файлов (все handlers, middleware, main).
- `httpx` — 4 клиента (`base.py`, `tmdb.py`, `emby.py`, `qbittorrent.py`).
- `tenacity` — 3 файла (`base.py`, `emby.py`, `qbittorrent.py`).
- `pydantic` — `models.py`, `config.py`.
- `pydantic_settings` — `config.py`.
- `aiosqlite` — `db.py`.
- `structlog` — 24 файла.

**Лишних production-зависимостей нет.**

### DEP-12: Транзитивные deps подтянутся как wheels (INFO)
- aiogram → `aiohttp`, `aiofiles`, `magic-filter`, `certifi`, `pydantic`, `typing-extensions`
- httpx → `httpcore`, `anyio`, `idna`, `certifi`, `sniffio`, `h11`
- pydantic → `pydantic-core` (Rust binary), `annotated-types`, `typing-extensions`
- aiosqlite → stdlib `sqlite3` C-extension (часть python:3.12-slim, не компилируется)

В `python:3.12-slim` все эти пакеты доступны как manylinux/musllinux wheels. **Компиляция из исходников не нужна** — на slim нет gcc, и pip упадёт с ошибкой, если бы это требовалось. Build-step в Dockerfile (`pip install --user`) проходит за ~30 сек, что подтверждает: всё ставится из wheels.

## Безопасность

### DEP-13: CVE-сканирование — проверить вручную (HIGH, process)
Без интернет-доступа к GitHub Advisory / OSV не могу проверить CVE. На 2026-05-08 рекомендуется выполнить:
```bash
pip install pip-audit
pip-audit -r requirements.txt -r requirements-dev.txt
# или
pip install safety
safety scan
```
Известные исторические CVE для текущих версий (по моим данным до cutoff):
- aiogram 3.27.0 — CVE отсутствуют (молодая, 2024-12).
- httpx 0.28.1 — CVE отсутствуют. (CVE-2021-41945 был для <0.23.0.)
- pydantic 2.12.5 — CVE отсутствуют в 2.x ветке.
- pydantic-settings 2.13.1 — CVE отсутствуют.
- structlog 25.5.0 — CVE отсутствуют.
- tenacity 9.1.4 — CVE отсутствуют.
- aiosqlite 0.22.1 — CVE отсутствуют.

**Решение**: добавить `pip-audit` в `make test` или CI, как было предложено в DEP-12 R2 (не закрыт).

### DEP-14: Hash-pinning отсутствует (MED, унаследовано из R2 как DEP-08)
- **Файл**: `Dockerfile:4`
- `pip install -r requirements.txt` ставит указанные версии, но не блокирует supply-chain атаку через подмену wheel'ов. Транзитивные deps вообще не пинятся.
- **Решение**: использовать `uv pip compile --generate-hashes` или `pip-compile --generate-hashes`, лочить + ставить с `--require-hashes`.
- **Статус**: [ ] не закрыт с R2

### DEP-15: Нет CI security-scan и Dependabot (HIGH process, унаследовано из R2 как DEP-12)
- **Файл**: отсутствует `.github/workflows/`
- **Проблема**: новые CVE в зависимостях не отслеживаются автоматически.
- **Решение**: создать `.github/workflows/security.yml` с `pip-audit` + `.github/dependabot.yml` для еженедельного pip-update PR.
- **Статус**: [ ] не закрыт с R2

## Базовый образ Docker

### DEP-16: python:3.12-slim — мигрировать на 3.13? (LOW)
- **Файл**: `Dockerfile:1,6`
- **Текущая**: python:3.12-slim. На 2026-05 доступны:
  - python:3.13-slim (released 2024-10, latest 3.13.x).
  - python:3.14-slim (если уже released — на 2026-10 ожидался; уточнить).
- **Совместимость**: pyproject `requires-python = ">=3.12"`. aiogram 3.27, pydantic 2.12, httpx 0.28 поддерживают 3.13. **Прямой блокировщик: pydantic-core wheel для 3.13+ должен существовать (есть с pydantic 2.10+).**
- **Профит миграции**: ~10-15% perf на async коде, free-threaded mode (опционально), новые synthax features.
- **Риск**: aiosqlite, magic-filter, structlog — все Python-only, риск минимален. typing-extensions 4.12+ нужен для 3.13.
- **Решение**: миграция отдельной задачей. Прогнать тесты на python:3.13-slim в CI; если зелёные — обновить Dockerfile + `requires-python = ">=3.13"`.
- **Статус**: [ ] плановая задача

### DEP-17: Multi-stage Dockerfile корректен (INFO)
- builder + runtime stages: ✓
- non-root user (botuser uid=1000): ✓
- HEALTHCHECK по watchdog файлу: ✓ (см. commit `62eaf74`)
- `--no-cache-dir`: ✓
- **Замечаний нет.** Размер 176 MB (по заметке R2) — нормально для python:3.12-slim + deps.

## Dev deps: убрать ли верхнюю границу?

### DEP-18: Dev deps жёстко пинятся (INFO)
В `requirements-dev.txt` все 5 dev-tools пинятся `==`. Это **корректная практика** для воспроизводимости CI (одинаковый ruff/mypy на всех машинах = одинаковые ошибки). Снимать пины не рекомендую.

В `pyproject.toml [dev]` диапазоны (`>=`) — это для устаревших installer'ов и для документации; для разработки используется `requirements-dev.txt`. Расхождение двух source-of-truth — отдельная проблема (DEP-08).

## Итог

**Новых находок раунда 3 (R3)**: 6 (DEP-03, DEP-04, DEP-05, DEP-06, DEP-09, DEP-16).
**Унаследованных из R2 не закрытых**: 4 (DEP-08/R2.10, DEP-10/R2.11, DEP-14/R2.08, DEP-15/R2.12).
**Закрытых с R2**: DEP-06 R2 (structlog upper bound), DEP-07 R2 (aiosqlite upper bound), DEP-02 R2 (aiogram 3.27).

| Severity | Кол-во | IDs                                       |
|----------|--------|-------------------------------------------|
| HIGH     | 1      | DEP-15                                    |
| MED      | 3      | DEP-05, DEP-08, DEP-14                    |
| LOW      | 4      | DEP-03, DEP-04, DEP-09, DEP-10, DEP-16    |
| INFO     | 7      | DEP-01, DEP-02, DEP-06, DEP-07, DEP-11, DEP-12, DEP-17, DEP-18 |

**Топ приоритет**:
1. DEP-15 (CI security scan) + DEP-14 (hash-pinning) — supply chain.
2. DEP-08 (рассинхрон pyproject ↔ requirements-dev.txt) — воспроизводимость.
3. DEP-09 (ужесточить `pydantic<2.13` в pyproject) — защита от broken `pip install -e .`.
4. DEP-10 (Makefile `dev` target).

**Все патч/минор апдейты совместимы с pydantic<2.13** (aiogram 3.27 — already current; pydantic 2.13 — заблокирован; pydantic-settings 2.14 — требует verification). Dev-апдейты (ruff, mypy, pytest) безопасны для runtime.
