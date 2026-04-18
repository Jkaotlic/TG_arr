# Dependencies Audit — TG_arr (Round 2)

Дата: 2026-04-18. Сверка через WebFetch pypi.org.

## Текущее vs. актуальные версии (production)

| Пакет              | Текущая (requirements.txt) | pyproject.toml constraint | Актуальная (pypi) | Δ |
|--------------------|----------------------------|---------------------------|-------------------|----|
| aiogram            | 3.26.0                     | `>=3.20,<4`               | **3.27.0** (2024-12-19) | minor update |
| httpx              | 0.28.1                     | `>=0.28,<1`               | 0.28.1            | актуально |
| tenacity           | 9.1.4                      | `>=9.0,<10`               | 9.1.4             | актуально |
| pydantic           | 2.12.5                     | `>=2.9,<3`                | **2.13.2** (2026-04-17) | minor update, `ValidationInfo.field_name` fix |
| pydantic-settings  | 2.13.1                     | `>=2.6,<3`                | 2.13.1            | актуально |
| aiosqlite          | 0.22.1                     | `>=0.20`                  | 0.22.1            | актуально |
| structlog          | 25.5.0                     | `>=24.4`                  | 25.5.0            | актуально |

### Dev

| Пакет              | Текущая | pypi | Статус |
|--------------------|---------|------|--------|
| pytest             | 9.0.2   | ~9.0+ | ок |
| pytest-asyncio     | 1.3.0   | ~1.x | ок |
| pytest-cov         | 7.1.0   | ~7.x | ок |
| ruff               | 0.14.6  | ~0.14+ | ок |
| mypy               | 1.18.2  | ~1.18+ | ок |

## DEP-02 — aiogram 3.27.0 доступен (LOW)

Файл: `requirements.txt:7`
3.26 → 3.27: minor, в основном bugfixes и типизация. Breaking не ожидается (pyproject `<4` разрешает). Рекомендую обновить.

## DEP-05 (НОВЫЙ) — pydantic 2.13.2 доступен (LOW)

Файл: `requirements.txt:12`
Bugfix для `ValidationInfo.field_name` в `model_validate_json`. Проект использует `SearchSession.model_validate`. Минор, но полезно.

## DEP-06 (НОВЫЙ) — Нет ни одного ref на `structlog>=25` специфично — подвержено silent drift (LOW)

Файл: `pyproject.toml:22`
`structlog>=24.4` — не ограничен сверху. Будущий 26.x может сломать API. Следует `structlog>=24.4,<26`.

## DEP-07 (НОВЫЙ) — `aiosqlite>=0.20` без upper bound (LOW)

Файл: `pyproject.toml:21`
То же — отсутствует upper bound. `aiosqlite>=0.20,<2` безопаснее.

## DEP-08 (НОВЫЙ) — Нет `--constraint`-пинов в Dockerfile (MED)

Файл: `Dockerfile:12`
`pip install --no-cache-dir -r requirements.txt` — использует точные версии, но не блокирует transitive deps (httpcore, anyio, etc.) от drift. Reproducible-build страдает.
**Решение:** использовать `pip install --require-hashes` + hashed requirements, либо `uv pip compile` с lock-файлом.

## DEP-09 (НОВЫЙ) — Unused transitive — `certifi`, `h11`, `httpcore` явно не пинятся (INFO)

Это ожидаемо, но для supply-chain безопасности рекомендуется hash-pinning (см. DEP-08).

## DEP-10 (НОВЫЙ) — Dev deps в `pyproject.toml [dev]` ≠ `requirements-dev.txt` (MED)

Файлы: `pyproject.toml:25-32` vs `requirements-dev.txt:4-8`
`pyproject.toml` указывает `pytest>=8.3`, а `requirements-dev.txt` — `pytest==9.0.2`. Десинхрон source-of-truth.
**Решение:** убрать `[dev]` из pyproject и оставить только requirements-dev.txt (или наоборот).

## DEP-11 (НОВЫЙ) — `Makefile: dev` игнорирует `requirements-dev.txt` (LOW)

Файл: `Makefile:25-27`
```
dev:
    pip install -r requirements.txt
    pip install ruff mypy
```
Не устанавливает pytest, pytest-asyncio, pytest-cov. Нужно `pip install -r requirements-dev.txt`.

## DEP-12 (НОВЫЙ) — Нет security-scanner шага в CI (HIGH, но это CI-level)

Нет `.github/workflows/`, нет `pip-audit` / `safety` / Dependabot config. Новые CVE в зависимостях не отслеживаются.
**Решение:** добавить `pip-audit` в `make test` или GitHub Action.

## Итого

LOW: DEP-02, DEP-05, DEP-06, DEP-07, DEP-09, DEP-11
MED: DEP-08, DEP-10
HIGH (process): DEP-12
Всего: 9 замечаний. Все патчи совместимы (minor-level), breaking changes нет.
