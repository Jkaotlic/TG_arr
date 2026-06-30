# Анализ — Зависимости · TG_arr (раунд 4, 2026-06-30)

Подтверждено находок: **4** (critical=0, high=0, medium=1, low=3). Все прошли состязательную верификацию (CONFIRMED/PLAUSIBLE).

## Средние

### DEP-01: `make dev` skips test dependencies and ignores requirements-dev.txt pins
- **Файл**: `Makefile:25`
- **Проблема**: The `dev` target runs `pip install -r requirements.txt` then `pip install ruff mypy` (lines 26-27). It never installs pytest, pytest-asyncio, or pytest-cov, and never reads requirements-dev.txt. On a fresh checkout, `make dev && make test` fails with `ModuleNotFoundError: No module named 'pytest'` (and tests import `pytest_asyncio` in tests/test_db.py:4, also missing). Additionally `ruff`/`mypy` are installed unpinned, so they drift from the pinned ruff==0.14.6 / mypy==1.18.2 in requirements-dev.txt (locally observed ruff 0.15.8, mypy entirely absent), which can change lint/type results vs CI expectations.
- **Риск**: Broken developer onboarding; lint/type drift from pinned tooling.
- **Решение**: Change the `dev` target to `pip install -r requirements.txt -r requirements-dev.txt` (matching the documented command in requirements-dev.txt header), and delete the separate `pip install ruff mypy` line so the pinned dev versions are honored.
- **Верификация**: CONFIRMED — Verified against the actual files. Makefile `dev` target (lines 25-27) runs only `pip install -r requirements.txt` (line 26) and `pip install ruff mypy` (line 27); it never installs pytest/pytest-asyncio/pytest-cov and never references requirements-dev.txt. `make test` (line 31) is `pytest tests/ -v`, so on a fresh checkout `make dev && make test` fails with ModuleNotFoundError: No module named 'pytest'. tests/test_db.py:3-4 imports both `pytest` and `pytest_asyncio` (all 12 test files import pytest), neither provided by `make dev`. requirements-dev.txt pins pytest==9.0.2, pytest-asyncio==1.3.
- **Статус**: [ ] Не исправлено

## Низкие

### DEP-02: README lists orjson as a dependency though it was removed and is not declared/imported
- **Файл**: `README.md:353`
- **Проблема**: The tech-stack table states `| Сериализация | orjson |`, but orjson is not in requirements.txt, requirements-dev.txt, pyproject.toml, or the Dockerfile, and is not imported anywhere in bot/ or tests/ (grep for `orjson` only matches README.md and the analysis notes; code uses stdlib `json` in bot/db.py, bot/clients/base.py, bot/clients/qbittorrent.py). orjson was intentionally dropped in round 2 (analysis/06-performance.md PERF-25). A reader following the README will believe orjson is a runtime requirement that does not exist.
- **Риск**: Documentation misleads about the actual dependency set.
- **Решение**: Remove the orjson row from the README tech-stack table (or replace with `pydantic-core (model_dump_json)` to reflect what actually does serialization).
- **Верификация**: CONFIRMED — README.md line 353 contains the row `| Сериализация | orjson |` in the tech-stack table (verified by reading lines 345-355). A repo-wide case-insensitive grep for `orjson` matches ONLY README.md:353 and analysis/06-performance.md (the audit notes documenting its removal) — there are zero matches anywhere in bot/ or tests/. I confirmed orjson is absent from requirements.txt (which lists only aiogram, httpx, tenacity, pydantic, pydantic-settings, aiosqlite, structlog), and absent from requirements-dev.txt, pyproject.toml, and Dockerfile (grep returned no orjson in any). analysis/06-performance.m
- **Статус**: [ ] Не исправлено

### DEP-03: README pins aiogram 3.26.0 while requirements.txt installs 3.27.0
- **Файл**: `README.md:348`
- **Проблема**: README tech-stack table line 348 says `| Telegram | aiogram 3.26.0 |`, but requirements.txt:6 pins `aiogram==3.27.0` (and the Docker image builds from requirements.txt, so 3.27.0 is what actually ships). The documented version is stale by one minor release, causing confusion when verifying the deployed version vs docs.
- **Риск**: Minor documentation drift.
- **Решение**: Update README.md:348 to `aiogram 3.27.0` (or change it to `aiogram 3.x` to avoid future drift, matching the badge on line 8).
- **Верификация**: CONFIRMED — Verified by direct reads of the current master tree. README.md:348 literally contains `| Telegram | aiogram 3.26.0 |`, while requirements.txt:6 pins `aiogram==3.27.0`. Dockerfile:3-4 (`COPY requirements.txt .` then `pip install -r requirements.txt`) confirms the built image installs 3.27.0, so the README tech-stack table is stale by one minor release versus what actually ships. The badge on README.md:8 is version-agnostic (`aiogram-3.x`), which matches the proposed alternate fix and shows the same file already uses a drift-proof convention elsewhere. The mismatch is real and not yet fixed. Sev
- **Статус**: [ ] Не исправлено

### DEP-04: Dev dependencies declared in two places (pyproject `dev` extra vs requirements-dev.txt) that disagree on versions
- **Файл**: `pyproject.toml:28`
- **Проблема**: pyproject.toml [project.optional-dependencies].dev (lines 28-34) declares loose floors: pytest>=8.3, pytest-asyncio>=0.24, pytest-cov>=5.0, ruff>=0.7, mypy>=1.13. requirements-dev.txt independently exact-pins pytest==9.0.2, pytest-asyncio==1.3.0, pytest-cov==7.1.0, ruff==0.14.6, mypy==1.18.2. The two are not contradictory (pins satisfy the floors) but they are two unsynchronized sources of truth: `pip install -e ".[dev]"` resolves to latest-allowed versions while requirements-dev.txt installs the pins, so two contributors get different toolchains. The requirements.txt header even tells developers to use the `[dev]` extra, while requirements-dev.txt tells them to use `-r requirements-dev.txt` — contradictory guidance.
- **Риск**: Non-reproducible dev toolchain; conflicting install instructions.
- **Решение**: Pick one source of truth for dev tools. Either (a) delete requirements-dev.txt and tighten the pyproject `dev` extra to the intended pins, or (b) keep requirements-dev.txt as the pinned source and remove/align the `dev` extra. Make the Makefile and README reference the chosen single path.
- **Верификация**: CONFIRMED — Opened all referenced files in current state. pyproject.toml:28-34 declares the `dev` extra with loose floors exactly as claimed: pytest>=8.3, pytest-asyncio>=0.24, pytest-cov>=5.0, ruff>=0.7, mypy>=1.13. requirements-dev.txt:4-8 independently exact-pins pytest==9.0.2, pytest-asyncio==1.3.0, pytest-cov==7.1.0, ruff==0.14.6, mypy==1.18.2. The pins satisfy the floors (not contradictory), but they are two unsynchronized sources of truth that resolve differently: `pip install -e ".[dev]"` pulls latest-allowed, `pip install -r requirements-dev.txt` pulls the pins.

The contradictory-guidance claim 
- **Статус**: [ ] Не исправлено
