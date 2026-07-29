<div align="center">

# TG_arr

### Telegram-бот для управления медиасервером

[![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)](https://python.org)
[![aiogram 3.x](https://img.shields.io/badge/aiogram-3.x-2CA5E0?logo=telegram&logoColor=white)](https://github.com/aiogram/aiogram)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](https://hub.docker.com)
[![Pydantic v2](https://img.shields.io/badge/pydantic-v2-E92063?logo=pydantic&logoColor=white)](https://docs.pydantic.dev)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-D7FF64?logo=ruff&logoColor=black)](https://docs.astral.sh/ruff/)

**Полноценный Telegram-бот для поиска, скачивания и управления фильмами, сериалами, аниме и музыкой через Scryer (+ Lidarr/slskd для музыки) с поддержкой qBittorrent, Emby, Navidrome, TMDb и Deezer.**

[Возможности](#-возможности) &bull; [Быстрый старт](#-быстрый-старт) &bull; [Настройка](#-настройка) &bull; [Команды](#-команды) &bull; [Скоринг](#-система-скоринга)

</div>

---

## Возможности

| | Функция | Описание |
|---|---------|----------|
| **Поиск** | Умный поиск | Автоматически определяет фильм, сериал, аниме или музыку по запросу |
| | Русские субтитры | Приоритизация релизов с RusSub, MVO, DVO, AVO |
| | Качество в деталях | Разрешение, кодек, HDR, аудио, субтитры — всё видно |
| | Вердикт Scryer | Профиль качества и Rego-правила решают, что первое в списке |
| **Аниме** | Отдельный фасет | Своя библиотека и профиль `1080p`, команда `/anime` |
| **Музыка** | Поиск артистов | `/music <artist>` — поиск через MusicBrainz (Lidarr) |
| | Альбомы и треки | Прямой поиск в Soulseek через slskd |
| | Уже в библиотеке | Navidrome-проверка, чтобы не качать дубли |
| | Добавление в Lidarr | Артист + все альбомы (мониторинг) |
| | Календарь релизов | Грядущие альбомы в /calendar |
| | Топ артистов | Deezer chart — популярные артисты недели |
| **Скачивание** | One-click grab | Скачивание релиза одной кнопкой |
| | qBittorrent fallback | Явный обход ограничений профиля Scryer (кнопка «Скачать всё равно») |
| | Очередь Scryer | Постановка выбранного релиза по candidate-токену |
| **Трендинг** | Популярные фильмы | Топ недели из TMDb с постерами |
| | Популярные сериалы | Трендовые сериалы с детальной информацией |
| **Мониторинг** | Календарь релизов | Расписание выходов с индикатором дней |
| | Уведомления | Оповещения о завершении скачивания |
| | Статус сервисов | Доступность Scryer/Lidarr/slskd/Navidrome + статистика индексеров |
| **Emby** | Библиотека | Просмотр последних добавлений в Emby |
| | Сканирование | Запуск сканирования библиотек |
| **Управление** | Настройки | Профили качества, папки, разрешение — на пользователя |
| | История | Лог всех действий с фильтрами |
| | Доступ | Whitelist по Telegram ID + роли админов |

---

## Архитектура

```
TG_arr
├── bot/
│   ├── main.py                    # Точка входа
│   ├── config.py                  # Pydantic Settings из ENV
│   ├── db.py                      # SQLite (aiosqlite)
│   ├── models.py                  # Датаклассы и Pydantic-модели
│   ├── clients/
│   │   ├── base.py                # HTTP-клиент (httpx + tenacity)
│   │   ├── scryer.py              # Scryer GraphQL (кино/сериалы/аниме)
│   │   ├── lidarr.py              # Lidarr API v1 (музыка)
│   │   ├── slskd.py               # slskd / Soulseek (альбомы и треки)
│   │   ├── navidrome.py           # Navidrome (Subsonic API, read-only)
│   │   ├── qbittorrent.py         # qBittorrent Web API
│   │   ├── emby.py                # Emby API
│   │   ├── tmdb.py                # TMDb API (трендинг кино/ТВ)
│   │   ├── deezer.py              # Deezer public API (трендинг музыки)
│   │   └── registry.py            # Фабрика клиентов (singleton)
│   ├── services/
│   │   ├── search_service.py      # Оркестрация поиска
│   │   ├── add_service.py         # Добавление + grab + fallback
│   │   ├── scoring.py             # Скоринг релизов
│   │   └── notification_service.py # Уведомления
│   ├── handlers/
│   │   ├── start.py               # /start, /help, /cancel
│   │   ├── search.py              # Поиск и граб
│   │   ├── music.py               # /music, добавление артистов в Lidarr
│   │   ├── trending.py            # Популярное (TMDb + Deezer)
│   │   ├── calendar.py            # Календарь релизов
│   │   ├── downloads.py           # Активные загрузки
│   │   ├── emby.py                # Emby-интеграция
│   │   ├── settings.py            # Настройки пользователя
│   │   ├── status.py              # Здоровье сервисов
│   │   └── history.py             # История действий
│   ├── ui/
│   │   ├── keyboards.py           # Inline-клавиатуры
│   │   └── formatters.py          # HTML-форматирование
│   └── middleware/
│       └── auth.py                # Авторизация (whitelist)
├── tests/                         # pytest + pytest-asyncio
├── Dockerfile                     # Python 3.12-slim, non-root
├── docker-compose.yml             # Portainer-ready
└── .env.example                   # Все переменные с описанием
```

---

## Быстрый старт

### Требования

- **Docker** и **Docker Compose** (или Portainer)
- Работающий **Scryer** (он сам ходит в Prowlarr за индексерами)
- Telegram-бот от [@BotFather](https://t.me/BotFather)
- Ваш Telegram ID (узнать: [@userinfobot](https://t.me/userinfobot))

### 1. Клонирование

```bash
git clone https://github.com/Jkaotlic/TG_arr.git
cd TG_arr
```

### 2. Конфигурация

```bash
cp .env.example .env
nano .env  # Заполнить обязательные переменные
```

<details>
<summary><b>Обязательные переменные</b></summary>

| Переменная | Описание |
|-----------|----------|
| `TELEGRAM_BOT_TOKEN` | Токен бота из @BotFather |
| `ALLOWED_TG_IDS` | Telegram ID пользователей (через запятую) |
| `SCRYER_URL` | URL Scryer (например `http://scryer:8088`) |
| `SCRYER_USERNAME` | Логин Scryer |
| `SCRYER_PASSWORD` | Пароль Scryer |

</details>

<details>
<summary><b>Опциональные переменные</b></summary>

| Переменная | По умолчанию | Описание |
|-----------|-------------|----------|
| `ADMIN_TG_IDS` | — | ID админов (через запятую) |
| `QBITTORRENT_URL` | — | URL qBittorrent Web UI |
| `QBITTORRENT_USERNAME` | `admin` | Логин qBittorrent |
| `QBITTORRENT_PASSWORD` | — | Пароль qBittorrent |
| `EMBY_URL` | — | URL Emby Server |
| `EMBY_API_KEY` | — | API-ключ Emby |
| `LIDARR_URL` | — | URL Lidarr (для музыки) |
| `LIDARR_API_KEY` | — | API-ключ Lidarr |
| `DEEZER_ENABLED` | `true` | Deezer public API для трендинга музыки |
| `TMDB_API_KEY` | — | API-ключ TMDb (для трендинга кино/ТВ) |
| `TMDB_LANGUAGE` | `ru-RU` | Язык TMDb-ответов |
| `TIMEZONE` | `Europe/Moscow` | Часовой пояс |
| `LOG_LEVEL` | `INFO` | Уровень логирования |
| `AUTO_GRAB_SCORE_THRESHOLD` | `80` | Порог автозахвата |
| `NOTIFY_DOWNLOAD_COMPLETE` | `true` | Уведомлять о скачивании |
| `NOTIFY_CHECK_INTERVAL` | `60` | Интервал проверки (сек) |
| `RESULTS_PER_PAGE` | `5` | Результатов на страницу |

</details>

### 3. Запуск

```bash
docker compose up -d
```

Проверка логов:

```bash
docker compose logs -f tg-arr-bot
```

### Portainer

В Portainer создайте Stack, вставьте содержимое `docker-compose.yml` и добавьте переменные окружения в секции **Environment variables** (не нужно загружать `.env` файл). Нажмите **Deploy the stack**.

---

## Команды

Меню за кнопкой «/» в Telegram и текст `/help` собираются из одного каталога —
[`bot/ui/commands.py`](bot/ui/commands.py). Таблица ниже покрывает его целиком плюс
несколько команд, которые в меню сознательно не публикуются.

| Команда | Описание |
|---------|----------|
| `/start` | Приветствие и главное меню |
| `/menu` | Главное меню |
| `/help` | Список команд и справка |
| `/search <запрос>` | Умный поиск (автоопределение фильм/сериал/аниме) |
| `/movie <запрос>` | Поиск фильмов |
| `/series <запрос>` | Поиск сериалов |
| `/anime <запрос>` | Поиск аниме (отдельный фасет Scryer) |
| `/music <артист>` | Поиск исполнителя |
| `/album <альбом>` | Поиск альбома |
| `/track <трек>` | Поиск трека |
| `/title <название>` | Тайтл в каталоге: мониторинг, удаление |
| `/wanted` | Что ищется и пока не находится |
| `/calendar` | Календарь выходов |
| `/emby` | Что нового в библиотеке |
| `/downloads` | Активные загрузки (qBittorrent + slskd) |
| `/history` | История действий |
| `/status` | Статус Scryer/Lidarr/slskd/Navidrome |
| `/health` | Диагностика проблем (индексеры, очередь поиска) |
| `/settings` | Настройки профиля |
| `/cancel` | Отмена текущей операции |

Не публикуются в меню, но работают: `/qstatus`, `/pause <hash>`, `/resume <hash>`
(детали qBittorrent) и админские `/users`, `/adduser`, `/deluser`.

Также можно просто отправить текстовое сообщение как поисковый запрос.

### Примеры поиска

```
Dune 2021              # Фильм с годом
Breaking Bad S02       # Сериал, 2-й сезон
The Office 1080p       # С предпочтением качества
Andor S01E05           # Конкретный эпизод
```

---

## Система скоринга

Каждый релиз оценивается по множеству факторов. Базовый балл — **50**.

### Бонусы

| Категория | Фактор | Баллы |
|-----------|--------|-------|
| Разрешение | 2160p / 1080p / 720p | +25 / +20 / +10 |
| Источник | REMUX / BluRay / WEB-DL / WEBRip | +30 / +20 / +15 / +10 |
| Кодек | AV1 / x265 (HEVC) / x264 | +15 / +10 / +5 |
| HDR | Dolby Vision / HDR10+ / HDR10 | +15 / +12 / +10 |
| Аудио | Atmos / TrueHD / DTS-HD / DTS | +10 / +8 / +7 / +5 |
| Субтитры | RusSub / MVO / DVO / AVO | +15 |
| Сиды | За каждые 10 сидов (макс. +20) | +2 |
| Качество | REPACK / PROPER | +5 |

### Штрафы

| Фактор | Баллы |
|--------|-------|
| CAM / TS / TC | -50 / -40 / -30 |
| `sample` / `trailer` в названии | -200 |
| Слишком маленький файл | -20 |
| Слишком большой файл | -10 |

---

## Интеграции

<table>
<tr>
<td width="50%" valign="top">

### Обязательные

| Сервис | Для чего |
|--------|----------|
| [Scryer](https://github.com/scryer-media/scryer) | Каталог кино/сериалов/аниме, индексеры, скоринг, загрузки |
| [Prowlarr](https://prowlarr.com) | Индексеры (бот обращается к ним только через Scryer) |

</td>
<td width="50%" valign="top">

### Опциональные

| Сервис | Для чего |
|--------|----------|
| [qBittorrent](https://qbittorrent.org) | Fallback-загрузка |
| [Emby](https://emby.media) | Медиабиблиотека |
| [TMDb](https://themoviedb.org) | Трендинг и постеры |

</td>
</tr>
</table>

---

## Разработка

```bash
# Виртуальное окружение
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Установка зависимостей
pip install -r requirements.txt

# Конфигурация
cp .env.example .env

# Запуск
python -m bot.main

# Тесты
pytest -x -q

# Тесты с покрытием
pytest --cov=bot --cov-report=html
```

Дев-инструменты (ruff, mypy, pytest-cov, PyYAML) ставятся одной командой:

```bash
make dev          # pip install -e . -r requirements-dev.txt
make lint         # ruff check
make typecheck    # mypy bot/ (не входит в make lint — ошибки известны, чинятся отдельно)
```

### Деплой и откат на Pi

```bash
make deploy       # build → tag prev → up -d → ps
make rollback     # вернуть :prev как :latest → up -d
```

### Обновление базового образа

`Dockerfile` пинит `python:3.12-slim` по digest для воспроизводимости сборки — это
значит, что security-патчи Debian-слоя не приезжают автоматически. Обновляйте
digest вручную не реже раза в месяц (или сразу после объявления CVE в базовом
образе):

```bash
make check-base-image   # docker buildx imagetools inspect python:3.12-slim
```

Сравните digest с зафиксированным в `Dockerfile` (обе строки `FROM`) и обновите
при расхождении.

---

## Безопасность

- Доступ только для пользователей из `ALLOWED_TG_IDS`
- API-ключи никогда не попадают в сообщения
- Non-root пользователь в Docker-контейнере
- Health check для мониторинга состояния
- SQLite хранит только метаданные и настройки

---

## Устранение неполадок

<details>
<summary><b>Бот не отвечает</b></summary>

1. Проверьте `ALLOWED_TG_IDS` — ваш Telegram ID должен быть в списке
2. Проверьте токен бота
3. Смотрите логи: `docker compose logs tg-arr-bot`
</details>

<details>
<summary><b>Не подключается к Scryer</b></summary>

1. Убедитесь что сервисы запущены
2. Проверьте URL-адреса из контейнера бота
3. Проверьте API-ключи
4. Используйте команду `/status` для диагностики
</details>

<details>
<summary><b>Не находит релизы</b></summary>

1. Проверьте индексеры в Prowlarr
2. Попробуйте более точный запрос
3. Проверьте поиск через UI Prowlarr
</details>

<details>
<summary><b>Ошибка добавления фильма/сериала</b></summary>

1. Проверьте профили качества в Scryer
2. Проверьте настройку root folders
3. Смотрите логи Scryer
</details>

---

## Стек технологий

| Компонент | Технология |
|-----------|------------|
| Язык | Python 3.12 |
| Telegram | aiogram 3.29.1 |
| HTTP | httpx + tenacity (retry) |
| Конфигурация | pydantic-settings v2 |
| БД | SQLite (aiosqlite) |
| Логирование | structlog |
| Контейнеризация | Docker (python:3.12-slim) |
| Тесты | pytest + pytest-asyncio |

---

## Лицензия

Проект распространяется под лицензией [MIT](LICENSE).

---

<div align="center">

**[Jkaotlic/TG_arr](https://github.com/Jkaotlic/TG_arr)**

</div>
