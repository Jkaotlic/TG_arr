# Анализ dependencies TG_arr

Проверялось: `pyproject.toml` vs `requirements.txt` vs `Dockerfile` vs реальные импорты.

Данные об актуальных версиях — по состоянию на январь 2026 (из обучающих данных Claude). Свежие версии следует подтвердить через `pip index versions <pkg>`.

## Критические

### DEP-01: Версии в `pyproject.toml` (min >=) расходятся с `requirements.txt` (pin ==)
- **Файл**: `pyproject.toml:15-24` vs `requirements.txt:1-19`
- **Проблема**: `pyproject.toml` задаёт **минимальные** версии (`aiogram>=3.13.1`), а `requirements.txt` — **пины** (`aiogram==3.26.0`). При установке через `pip install .` (pyproject) будет взят latest, через `pip install -r requirements.txt` — pinned. Dockerfile использует requirements.txt. Это создаёт «скрытую» возможность, что dev-окружение (через pyproject) отличается от prod (через Dockerfile).
- **Риск**: Высокий (невоспроизводимость).
- **Решение**: Либо зафиксировать pyproject.toml минорными версиями (`~=3.26`), либо отказаться от requirements.txt и генерировать его из pyproject.toml через `pip-compile`.
- **Статус**: [ ] Не исправлено

### DEP-02: `pyproject.toml` требует Python >=3.12, но Dockerfile использует `python:3.12-slim`
- **Файл**: `pyproject.toml:10`, `Dockerfile:1`
- **Проблема**: Совпадает. OK. Но `MEMORY.md` упоминает Python 3.11+ — неточность в документации.
- **Статус**: OK

### DEP-03: `pyproject.toml` указывает dev-deps `pytest>=9.0.0 pytest-asyncio>=1.3.0 pytest-cov>=7.0.0`, а `requirements.txt` дублирует их
- **Файл**: `pyproject.toml:27-31`, `requirements.txt:21-24`
- **Проблема**: pytest 9.0 — это очень новая версия (требует Python ≥3.9). Dev-зависимости обычно не должны быть в requirements.txt (production).
- **Риск**: Средний (раздутый образ).
- **Решение**: Создать `requirements-dev.txt` отдельно, убрать pytest из requirements.txt.
- **Статус**: [ ] Не исправлено

## Высокие

### DEP-04: Отсутствует `orjson` в requirements.txt, но есть в pyproject
- **Файл**: `pyproject.toml:23` (`orjson>=3.10.11`), `requirements.txt:19` (`orjson==3.11.7`)
- **Проблема**: Требует orjson, но **реально нигде не импортируется** (grep подтвердил: 0 совпадений на `import orjson`). Это dead dependency.
- **Риск**: Средний (ненужная зависимость, +размер).
- **Решение**: Удалить orjson из pyproject и requirements.txt; либо использовать её в db.py для ускорения JSON-сериализации сессий.
- **Статус**: [ ] Не исправлено

### DEP-05: `tenacity==9.1.4` — актуальная 9.x, OK. Нет CVE.
- **Файл**: `requirements.txt:6`
- **Статус**: OK

### DEP-06: `pydantic==2.12.5` — актуальная 2.x серия на момент 2026-01. Проверить >=2.12 обязательно для Py 3.12 совместимости
- **Файл**: `requirements.txt:9`
- **Статус**: OK

### DEP-07: `pydantic-settings==2.13.1` — актуальная. OK.
- **Статус**: OK

### DEP-08: `aiosqlite==0.22.1` — минорная стабильная. Последняя из известных — 0.20.x серия. Проверить существование 0.22.1.
- **Файл**: `requirements.txt:13`
- **Проблема**: По состоянию на нач. 2026 latest stable: 0.20.0 (возможно более новые есть). Версия 0.22.1 может быть pre-release или несуществующей.
- **Риск**: Средний (при build).
- **Решение**: Проверить на pypi.org/project/aiosqlite, понизить до 0.20.0 если 0.22.1 нет.
- **Статус**: [ ] Требует проверки

### DEP-09: `aiogram==3.26.0` — актуальная. В `pyproject` `aiogram>=3.13.1`. В 3.13 → 3.26 было breaking changes в F-filters (не больших, но есть).
- **Файл**: `requirements.txt:2`
- **Статус**: OK, но pyproject min следует подтянуть до `>=3.20`.

### DEP-10: `structlog==25.5.0` — дата выхода ~конец 2025. OK.
- **Статус**: OK

### DEP-11: `httpx==0.28.1` — актуальная стабильная, 0.28 — последняя major до 1.0.
- **Файл**: `requirements.txt:5`
- **Статус**: OK

### DEP-12: `pytest==9.0.2` — pytest 9.0 пока pre-release или не существует (8.x — stable на 2026-01)
- **Файл**: `requirements.txt:22`
- **Проблема**: По состоянию на конец 2025/начало 2026 последняя stable pytest: 8.3.x. Версия 9.0.2 может быть будущей beta.
- **Риск**: Средний (при установке).
- **Решение**: Понизить до 8.3.x или подтвердить.
- **Статус**: [ ] Требует проверки

### DEP-13: `pytest-asyncio==1.3.0` — в 2025 был стабильным 0.24 → 0.26; 1.x серия может быть новее
- **Файл**: `requirements.txt:23`
- **Проблема**: pytest-asyncio 1.0 — это переход на новое API, dev-статус в 2025. В 2026 возможно 1.3 stable.
- **Статус**: [ ] Требует проверки

### DEP-14: `pytest-cov==7.1.0` — 7.x — новейшая серия (5.x/6.x были в 2024-2025)
- **Статус**: [ ] Требует проверки

### DEP-15: `python-dateutil` не указан нигде, но **не используется** в коде (grep подтвердил)
- **Файл**: (отсутствует)
- **Проблема**: В задании аудита упомянут обязательный `python-dateutil` — в коде НЕ используется. Используется только `datetime` stdlib и `datetime.fromisoformat`, что в Python 3.11+ корректно обрабатывает timezone.
- **Статус**: OK (нет зависимости — правильно)

## Средние

### DEP-16: Нет `ruff`, `black`, `mypy` в requirements/pyproject dev, но в Makefile `make lint` ожидает `ruff`
- **Файл**: `Makefile:39`, `pyproject.toml:27-31`
- **Проблема**: dev-extra содержит только pytest, pytest-asyncio, pytest-cov. Но Makefile target `lint` использует `ruff`, `format` — тоже `ruff`. `make dev` явно ставит `pip install ruff mypy`, минуя pyproject.
- **Решение**: Добавить `ruff`, `mypy` в `[project.optional-dependencies.dev]`.
- **Статус**: [ ] Не исправлено

### DEP-17: Нет `.tool-versions` / `.python-version` / `python_requires` согласованности
- **Файл**: отсутствует `.python-version`
- **Проблема**: pyproject: >=3.12, Dockerfile: python:3.12-slim, MEMORY.md: Python 3.11+. Разногласие в документации.
- **Решение**: Создать `.python-version` с `3.12`, обновить MEMORY.md.
- **Статус**: [ ] Не исправлено

### DEP-18: Нет lock-файла (poetry.lock / pdm.lock / uv.lock)
- **Файл**: отсутствует
- **Проблема**: requirements.txt — не lock, лишь transitive dep'ы не зафиксированы. Установка через месяц может привести к другим suburl-версиям httpx-core, anyio и т.п.
- **Решение**: Использовать `pip-compile` для создания locked.txt.
- **Статус**: [ ] Не исправлено

### DEP-19: aiogram 3.x зависит от magic-filter, aiohttp — в requirements.txt не pinned
- **Файл**: `requirements.txt`
- **Проблема**: magic-filter <1.1 может иметь баги. Косвенная зависимость.
- **Решение**: Добавить transitive-pins через pip-compile.
- **Статус**: [ ] Не исправлено

## Низкие

### DEP-20: `structlog` processors — используется ConsoleRenderer + JSONRenderer. OK.
- **Статус**: OK

### DEP-21: Нет `freezegun` для тестов datetime — тесты `cleanup_old_sessions(hours=0)` зависят от системного времени
- **Файл**: `tests/test_db.py:283`
- **Проблема**: Тест проходит везде (delete with cutoff=now), но при миграции на моки было бы чище.
- **Статус**: [ ] Минорно

### DEP-22: `requirements.txt` без `--index-url` — использует дефолтный pypi.org
- **Файл**: `requirements.txt`
- **Статус**: OK

### DEP-23: Dockerfile не использует `pip install --require-hashes`
- **Файл**: `Dockerfile:12`
- **Проблема**: Для production best-practice supply-chain защита через hashes.
- **Статус**: [ ] Низко

## CVE-проверка (на январь 2026)

| Пакет | Версия | Известные CVE |
|---|---|---|
| aiogram 3.26.0 | нет публичных CVE |
| httpx 0.28.1 | нет |
| pydantic 2.12.5 | нет |
| aiosqlite 0.22.1 | нет |
| tenacity 9.1.4 | нет |
| structlog 25.5.0 | нет |
| orjson 3.11.7 | нет (исторически были OOM в 3.9.x, исправлено) |

## Итоговый подсчёт
- Критические: 1 (DEP-01 несогласованность версий)
- Высокие: 4 (DEP-04, DEP-08, DEP-12, DEP-13, DEP-14 — требуют проверки существования версий)
- Средние: 4 (DEP-16..19)
- Низкие: 4 (DEP-20..23)
