<div align="center">

# TG_arr

### Telegram-бот для управления медиасервером

[![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)](https://python.org)
[![aiogram 3.x](https://img.shields.io/badge/aiogram-3.x-2CA5E0?logo=telegram&logoColor=white)](https://github.com/aiogram/aiogram)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](https://hub.docker.com)
[![Pydantic v2](https://img.shields.io/badge/pydantic-v2-E92063?logo=pydantic&logoColor=white)](https://docs.pydantic.dev)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-D7FF64?logo=ruff&logoColor=black)](https://docs.astral.sh/ruff/)

**Полноценный Telegram-бот для поиска, скачивания и управления фильмами и сериалами через Prowlarr + Radarr + Sonarr с поддержкой qBittorrent, Emby и TMDb.**

[Возможности](#-возможности) &bull; [Быстрый старт](#-быстрый-старт) &bull; [Настройка](#-настройка) &bull; [Команды](#-команды) &bull; [Скоринг](#-система-скоринга)

</div>

---

## Возможности

| | Функция | Описание |
|---|---------|----------|
| **Поиск** | Умный поиск | Автоматически определяет фильм или сериал по запросу |
| | Русские субтитры | Приоритизация релизов с RusSub, MVO, DVO, AVO |
| | Качество в деталях | Разрешение, кодек, HDR, аудио, субтитры — всё видно |
| **Скачивание** | One-click grab | Скачивание релиза одной кнопкой |
| | qBittorrent fallback | Автообход профильных ограничений Radarr/Sonarr |
| | Push release | Отправка релизов напрямую в *arr |
| **Трендинг** | Популярные фильмы | Топ недели из TMDb с постерами |
| | Популярные сериалы | Трендовые сериалы с детальной информацией |
| **Мониторинг** | Календарь релизов | Расписание выходов с индикатором дней |
| | Уведомления | Оповещения о завершении скачивания |
| | Статус сервисов | Проверка доступности Prowlarr/Radarr/Sonarr |
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
│   │   ├── prowlarr.py            # Prowlarr API + парсинг качества
│   │   ├── radarr.py              # Radarr API v3
│   │   ├── sonarr.py              # Sonarr API v3
│   │   ├── qbittorrent.py         # qBittorrent Web API
│   │   ├── emby.py                # Emby API
│   │   ├── tmdb.py                # TMDb API (трендинг)
│   │   └── registry.py            # Фабрика клиентов (singleton)
│   ├── services/
│   │   ├── search_service.py      # Оркестрация поиска
│   │   ├── add_service.py         # Добавление + grab + fallback
│   │   ├── scoring.py             # Скоринг релизов
│   │   └── notification_service.py # Уведомления
│   ├── handlers/
│   │   ├── start.py               # /start, /help, /cancel
│   │   ├── search.py              # Поиск и граб
│   │   ├── trending.py            # Популярное (TMDb)
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
- Работающие **Prowlarr**, **Radarr**, **Sonarr**
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
| `PROWLARR_URL` | URL Prowlarr (например `http://prowlarr:9696`) |
| `PROWLARR_API_KEY` | API-ключ Prowlarr |
| `RADARR_URL` | URL Radarr (например `http://radarr:7878`) |
| `RADARR_API_KEY` | API-ключ Radarr |
| `SONARR_URL` | URL Sonarr (например `http://sonarr:8989`) |
| `SONARR_API_KEY` | API-ключ Sonarr |

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
| `TMDB_API_KEY` | — | API-ключ TMDb (для трендинга) |
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

| Команда | Описание |
|---------|----------|
| `/start` | Приветствие и главное меню |
| `/help` | Список команд и справка |
| `/search <запрос>` | Умный поиск (автоопределение) |
| `/movie <запрос>` | Поиск фильмов |
| `/series <запрос>` | Поиск сериалов |
| `/settings` | Настройки профиля |
| `/status` | Статус Prowlarr/Radarr/Sonarr |
| `/history` | История действий |
| `/cancel` | Отмена текущей операции |

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
| [Prowlarr](https://prowlarr.com) | Поиск по индексерам |
| [Radarr](https://radarr.video) | Управление фильмами |
| [Sonarr](https://sonarr.tv) | Управление сериалами |

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
<summary><b>Не подключается к Prowlarr/Radarr/Sonarr</b></summary>

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

1. Проверьте наличие профилей качества в Radarr/Sonarr
2. Проверьте настройку root folders
3. Смотрите логи Radarr/Sonarr
</details>

---

## Стек технологий

| Компонент | Технология |
|-----------|------------|
| Язык | Python 3.12 |
| Telegram | aiogram 3.13 |
| HTTP | httpx + tenacity (retry) |
| Конфигурация | pydantic-settings v2 |
| БД | SQLite (aiosqlite) |
| Логирование | structlog |
| Сериализация | orjson |
| Контейнеризация | Docker (python:3.12-slim) |
| Тесты | pytest + pytest-asyncio |

---

## Лицензия

Проект распространяется под лицензией [MIT](LICENSE).

---

<div align="center">

**[Jkaotlic/TG_arr](https://github.com/Jkaotlic/TG_arr)**

</div>
