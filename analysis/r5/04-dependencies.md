# Анализ зависимостей TG_arr (раунд 5)

**Метод**: чтение файлов зависимостей, grep импортов по `bot/` и `tests/`, живые запросы `pip index versions <pkg>` и PyPI JSON API (метаданные aiogram) — сеть доступна, актуальность версий проверена на 2026-07-02, а не по памяти модели.

## Критические

Не обнаружено. Все пины взаимно совместимы, известных CVE в запиненных версиях нет, сборка на arm64 работоспособна.

## Средние

### DEP-01: `aiohttp` используется в prod-коде, но не объявлен как зависимость
- **Файл**: `bot/webhook.py:17` (`from aiohttp import web`), также `tests/test_feat_webhook.py:51` (`aiohttp.test_utils`)
- **Проблема**: aiohttp нет ни в `requirements.txt`, ни в `pyproject.toml:15-25` — он приезжает транзитивно через aiogram. Метаданные aiogram 3.27.0 (проверено по PyPI): `aiohttp<3.14,>=3.9.0` — т.е. версия network-facing HTTP-сервера (webhook, фича #8) вообще не контролируется проектом и зависит от даты сборки (сегодня резолвится в 3.13.4).
- **Риск**: (1) невоспроизводимая версия компонента, слушающего сеть; (2) если aiogram 4.x сменит транспорт — импорт упадёт в рантайме при `WEBHOOK_ENABLED=true` (import ленивый, `bot/main.py:316`, так что бот стартует, а фича сломается молча при включении).
- **Решение**: добавить `aiohttp==3.13.4` в `requirements.txt` и `"aiohttp>=3.9,<4"` в `pyproject.toml [project].dependencies`.
- **Статус**: [ ] Не исправлено

### DEP-02: нет lock-механизма — транзитивные зависимости свободные
- **Файл**: `requirements.txt:1-22`, `Dockerfile:7-8`
- **Проблема**: пинятся только 7 top-level пакетов. Транзитивные (aiohttp, aiofiles, magic-filter, pydantic-core, typing-extensions, anyio, httpcore, h11, certifi, python-dotenv и др.) резолвятся заново при каждой сборке. Digest-pin базового образа (`Dockerfile:5`) фиксирует только слой ОС/Python, но не pip-слой. Нет ни pip-compile/uv-lock, ни `--require-hashes`.
- **Риск**: две сборки в разные дни ≠ одинаковые образы; на arm64 дополнительно — если у свежей транзитивной версии не окажется aarch64-wheel, `python:3.12-slim` без gcc упадёт на этапе `pip install` прямо на Pi/buildx.
- **Решение**: `uv pip compile requirements.txt -o requirements.lock --generate-hashes` (или pip-tools), в Dockerfile ставить из lock с `--require-hashes`; requirements.txt оставить как human-readable источник.
- **Статус**: [ ] Не исправлено

### DEP-03: mypy объявлен, но нигде не запускается и не сконфигурирован
- **Файл**: `requirements-dev.txt:12`, `pyproject.toml:36`, `Makefile:1-73`
- **Проблема**: `mypy==1.18.2` есть в dev-зависимостях, но: нет make-цели (`make lint` = только ruff, `Makefile:38`), нет CI (`.github/` отсутствует в репо), нет секции `[tool.mypy]`. Grep по репо: mypy упоминается только в файлах зависимостей и `.dockerignore:12`.
- **Риск**: мёртвая dev-зависимость создаёт иллюзию типовой проверки; код с аннотациями (`from __future__ import annotations` используется) никем не проверяется.
- **Решение**: либо добавить `make typecheck: mypy bot/` + минимальный `[tool.mypy]` (python_version = "3.12", strict-ish), либо убрать mypy из обоих файлов.
- **Статус**: [ ] Не исправлено

### DEP-04: конфиг ruff отсутствует — линт работает на дефолтах
- **Файл**: `pyproject.toml` (нет `[tool.ruff]`), нет `ruff.toml`/`.ruff.toml`; `Makefile:38,42-43`
- **Проблема**: версия ruff жёстко запинена (`requirements-dev.txt:11` → 0.14.6), но правила не зафиксированы: дефолтный набор — только `E4/E7/E9/F`, без isort (I), bugbear (B), pyupgrade (UP); line-length дефолтный. При этом pyproject-диапазон `ruff>=0.14,<1` (`pyproject.toml:35`) допускает 0.15.20 (текущий на PyPI), где дефолты/формат могут отличаться → `make dev` и `pip install -r requirements-dev.txt` дадут разный вывод `ruff format`.
- **Риск**: линт даёт ложное чувство покрытия; расхождения формата между окружениями.
- **Решение**: добавить `[tool.ruff]` с `target-version = "py312"`, `line-length`, явным `lint.select`.
- **Статус**: [ ] Не исправлено

## Низкие

### DEP-05: отставания версий (живая проверка PyPI, 2026-07-02)
- **Файл**: `requirements.txt:6,14,15,21`, `requirements-dev.txt:8-12`
- **Проблема** (пин → актуальная):
  - aiogram 3.27.0 → **3.29.1** (2 минора);
  - pydantic 2.12.5 → 2.13.4 — заблокирован aiogram 3.27 (`pydantic<2.13`), **но aiogram 3.29.1 уже разрешает `pydantic<2.14`** — апгрейд aiogram снимает cap; после этого обновить комментарий DEP-09 и границу в `pyproject.toml:19-21`;
  - structlog 25.5.0 → **26.1.0** — мажор заблокирован cap `<26` в `pyproject.toml:24`;
  - pydantic-settings 2.13.1 → 2.14.2; pytest 9.0.2 → 9.1.1; pytest-asyncio 1.3.0 → 1.4.0; ruff 0.14.6 → 0.15.20;
  - mypy 1.18.2 → **2.1.0** (вышла мажорная линейка 2.x; cap `<2` в `pyproject.toml:36` блокирует сознательно — приемлемо, но решение стоит принять явно).
  - Актуальны: httpx 0.28.1, tenacity 9.1.4, aiosqlite 0.22.1, pytest-cov 7.1.0.
- **Риск**: накопление отставания; pydantic-cap устареет тихо.
- **Решение**: бандл-апгрейд aiogram 3.29.1 + pydantic 2.13.4 + structlog 26.x (поднять cap) одним PR с прогоном тестов на Pi.
- **Статус**: [ ] Не исправлено

### DEP-06: `make dev` и requirements-dev.txt дают разный toolchain во времени
- **Файл**: `Makefile:26`, `pyproject.toml:27-37`, `requirements-dev.txt:1-12`
- **Проблема**: `make dev` = `pip install -e ".[dev]"` ставит по диапазонам (сегодня: ruff 0.15.20, pytest 9.1.1), а requirements-dev.txt пинит 0.14.6/9.0.2. Комментарий в `pyproject.toml:27-29` («resolve to the same toolchain») верен только в момент пиновки — guard-тест `tests/test_r4_C1-deploy-docs.py:85` проверяет лишь вхождение пинов в диапазоны, не равенство результата.
- **Решение**: `make dev` → `pip install -e . -r requirements-dev.txt`.
- **Статус**: [ ] Не исправлено

### DEP-07: PyYAML нужен тестам, но не объявлен — тесты молча скипаются
- **Файл**: `tests/test_r4_C1-deploy-docs.py:16-19`, skipif на строках `112`, `145`
- **Проблема**: два guard-теста compose-конфига требуют `yaml`, которого нет ни в dev-extra, ни в requirements-dev.txt → у всех, кто ставит по инструкции, они скипаются всегда.
- **Решение**: добавить `PyYAML>=6,<7` в dev-зависимости (обоих файлов) либо парсить compose без yaml.
- **Статус**: [ ] Не исправлено

### DEP-08: digest-pin базового образа требует регламента обновления
- **Файл**: `Dockerfile:5,10`
- **Проблема**: digest зафиксирован 2026-06-30 (python 3.12.13-slim-trixie) — правильно для воспроизводимости, но security-патчи Debian-слоя теперь приезжают только при ручном refresh; автоматики (renovate/dependabot) в репо нет.
- **Решение**: завести напоминание/renovate на ежемесячный `docker buildx imagetools inspect python:3.12-slim` + bump.
- **Статус**: [ ] Не исправлено

## Проверено — проблем нет

- **Согласованность пинов** (задача 1): все `==` из requirements.txt входят в диапазоны pyproject — aiogram 3.27.0 ∈ `>=3.20,<4`; httpx 0.28.1 ∈ `>=0.28,<1`; tenacity 9.1.4 ∈ `>=9.0,<10`; pydantic 2.12.5 ∈ `>=2.9,<2.13`; pydantic-settings 2.13.1 ∈ `>=2.6,<3`; aiosqlite 0.22.1 ∈ `>=0.20,<2`; structlog 25.5.0 ∈ `>=24.4,<26`. Dev-пины ∈ dev-диапазонов, защищено guard-тестом `tests/test_r4_C1-deploy-docs.py:85`. Dockerfile ставит ровно `requirements.txt` — расхождений нет.
- **pydantic-cap корректен**: aiogram 3.27.0 действительно требует `pydantic<2.13` (проверено по PyPI-метаданным) — откат до 2.12.5 (`2a51a2b`) был правильным.
- **Неиспользуемых зависимостей нет** (задача 2): все 7 runtime-пакетов импортируются в `bot/` (grep: 36 вхождений в 27 файлах). Все прочие импорты — stdlib (tomllib, zoneinfo, ipaddress, faulthandler, …). Единственные недекларированные — aiohttp (DEP-01) и yaml (DEP-07).
- **CVE** (задача 4, уверенность: средняя — cutoff знаний 2026-01, свежие advisories мог не видеть): известных CVE нет в aiogram 3.27.0, httpx 0.28.1 (CVE-2021-41945 — древний, закрыт), tenacity, pydantic 2.12.5 (CVE-2024-3772 закрыт с 2.4), pydantic-settings, aiosqlite, structlog, pytest/ruff/mypy. Транзитивный aiohttp резолвится в 3.13.4 — все известные aiohttp CVE (2024-23334, 2024-30251, 2024-52304, 2024-27306) закрыты задолго до 3.13. Рекомендуется разово прогнать `pip-audit` для сверки с базой 2026 года.
- **Dockerfile/arm64**: multi-arch digest (amd64+arm64), Python 3.12.13 совпадает с прод-требованием, multi-stage, non-root, только runtime-deps — ок.
- **Python-версии**: `requires-python = ">=3.12"` соответствует образу 3.12.13.

**Выполненные команды**: `pip index versions` для всех 13 пакетов (вывод LATEST приведён в DEP-05); `curl pypi.org/pypi/aiogram/{3.27.0,3.29.1}/json` — constraints `aiohttp<3.14 / pydantic<2.13` (3.27.0) и `aiohttp<3.15 / pydantic<2.14` (3.29.1).
