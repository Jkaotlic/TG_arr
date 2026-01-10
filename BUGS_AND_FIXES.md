# 🐛 Анализ багов и недочётов проекта TG_arr

Дата анализа: 10 января 2026
**Статус: ✅ ВСЕ ОСНОВНЫЕ ИСПРАВЛЕНИЯ ВЫПОЛНЕНЫ**

## Критические ошибки (баги)

### 1. ✅ ИСПРАВЛЕНО — Отсутствуют свойства `radarr_enabled` и `sonarr_enabled` в конфигурации

**Файлы:**
- `bot/config.py`
- `bot/services/calendar_notification_service.py` (строки 141, 179)
- `bot/handlers/calendar.py` (строки 47, 86)

**Проблема:**
Код использует `settings.radarr_enabled` и `settings.sonarr_enabled`, но эти свойства не определены в классе `Settings`. Это приведёт к `AttributeError` при работе с календарём и уведомлениями.

**Решение:**
Добавить в `bot/config.py` свойства:
```python
@property
def radarr_enabled(self) -> bool:
    """Check if Radarr is properly configured."""
    return bool(self.radarr_url and self.radarr_api_key)

@property
def sonarr_enabled(self) -> bool:
    """Check if Sonarr is properly configured."""
    return bool(self.sonarr_url and self.sonarr_api_key)
```

---

### 2. ✅ ИСПРАВЛЕНО — Несоответствие сигнатуры методов в `calendar_notification_service.py`

**Файлы:**
- `bot/services/calendar_notification_service.py` (строка 121, 232)
- `bot/db.py` (строки 583-598, 600-618)

**Проблема:**
Метод `db.is_release_notified()` требует 3 аргумента (`user_id`, `event_type`, `content_id`), но вызывается с 2 аргументами:
```python
# Неправильно (текущий код):
if await db.is_release_notified(sub.user_id, event.content_id):

# Правильно:
if await db.is_release_notified(sub.user_id, event.event_type.value, event.content_id):
```

Аналогичная проблема с `db.mark_release_notified()` — требует 4 аргумента, вызывается с 2:
```python
# Неправильно (текущий код):
await db.mark_release_notified(user_id, event.content_id)

# Правильно:
await db.mark_release_notified(user_id, event.event_type.value, event.content_id, event.release_date)
```

**Решение:**
Исправить вызовы в `bot/services/calendar_notification_service.py`:

Строка ~121:
```python
if await db.is_release_notified(sub.user_id, event.event_type.value, event.content_id):
```

Строка ~232:
```python
await db.mark_release_notified(user_id, event.event_type.value, event.content_id, event.release_date)
```

---

### 3. ✅ ИСПРАВЛЕНО — Использование устаревшего `datetime.utcnow()` (deprecated в Python 3.12)

**Файлы:** (15 мест)
- `bot/db.py` (строки 162, 180, 207, 246, 308, 382, 394, 533, 608, 624)
- `bot/handlers/calendar.py` (строка 43)
- `bot/models.py` (строка 505)
- `bot/services/calendar_notification_service.py` (строки 99, 105, 137)

**Проблема:**
`datetime.utcnow()` устарел начиная с Python 3.12. Рекомендуется использовать `datetime.now(datetime.UTC)`.

**Решение:**
Заменить все вхождения:
```python
# Старый код:
datetime.utcnow()

# Новый код:
from datetime import datetime, UTC
datetime.now(UTC)
```

Также обновить все `Field(default_factory=datetime.utcnow)` в моделях на:
```python
Field(default_factory=lambda: datetime.now(UTC))
```

---

## Средние проблемы

### 4. ✅ ИСПРАВЛЕНО — Потенциальная утечка ресурсов в обработчиках

**Файлы:**
- `bot/handlers/settings.py` (функция `get_add_service`)
- `bot/handlers/status.py` (функция `cmd_status`)

**Проблема:**
Создаются новые клиенты API при каждом вызове без использования синглтонов из реестра. Хотя клиенты закрываются в `finally`, это неэффективно.

**Решение:**
Использовать функции из `bot/clients/registry.py` (`get_prowlarr()`, `get_radarr()`, etc.) вместо создания новых экземпляров.

---

### 5. ✅ ИСПРАВЛЕНО — Конфликт имён класса `ConnectionError`

**Файл:** `bot/clients/base.py` (строка 28)

**Проблема:**
Класс `ConnectionError` переопределяет встроенный `ConnectionError` Python. Это может привести к неожиданному поведению.

**Решение:**
Переименовать в `APIConnectionError`:
```python
class APIConnectionError(APIError):
    """Connection error to the service."""
    pass
```

---

### 6. ✅ ИСПРАВЛЕНО — Неопределённая переменная `tomorrow` используется, но не используется

**Файл:** `bot/services/calendar_notification_service.py` (строка 99)

**Проблема:**
Переменная `tomorrow` определяется, но никогда не используется:
```python
tomorrow = datetime.utcnow().date() + timedelta(days=1)  # Эта переменная не используется
```

**Решение:**
Удалить неиспользуемую переменную или использовать её по назначению.

---

### 7. ✅ ИСПРАВЛЕНО — Отсутствие проверки `callback.message` перед редактированием

**Файл:** `bot/handlers/calendar.py` (строка 148)

**Проблема:**
```python
async def callback_calendar_menu(callback: CallbackQuery) -> None:
    await callback.message.edit_text(...)  # callback.message может быть None
```

**Решение:**
Добавить проверку:
```python
if not callback.message:
    await callback.answer("Сообщение недоступно", show_alert=True)
    return
```

---

### 8. ⚠️ Ненужная опция `enable_decoding=False` в pydantic-settings

**Файл:** `bot/config.py` (строка 19)

**Проблема:**
Опция `enable_decoding` отсутствует в документации pydantic-settings v2. Возможно, это вызовет предупреждение или ошибку.

**Решение:**
Проверить документацию актуальной версии и удалить если не поддерживается. Валидатор `parse_comma_separated_ids` уже корректно обрабатывает строки.

---

## Мелкие недочёты

### 9. ✅ ИСПРАВЛЕНО — Неиспользуемый импорт `BufferedInputFile`, `FSInputFile`

**Файл:** `bot/handlers/trending.py` (строка 4)

**Проблема:**
```python
from aiogram.types import CallbackQuery, Message, BufferedInputFile, FSInputFile, URLInputFile
```
`BufferedInputFile` и `FSInputFile` импортируются, но не используются.

**Решение:**
Удалить неиспользуемые импорты:
```python
from aiogram.types import CallbackQuery, Message, URLInputFile
```

---

### 10. 💡 Дублирование кода для получения сервисов

**Файлы:**
- `bot/handlers/search.py` (функция `get_services`)
- `bot/handlers/trending.py` (похожий паттерн)

**Проблема:**
Создание сервисов дублируется в нескольких местах.

**Решение:**
Вынести в отдельный модуль `bot/services/__init__.py` или создать фабрику сервисов.

---

### 11. ✅ ИСПРАВЛЕНО — Отсутствие типизации `callable` в `notification_service.py`

**Файл:** `bot/services/notification_service.py` (строка 21)

**Проблема:**
```python
send_notification: callable,  # Устаревшая типизация
```

**Решение:**
Использовать современную типизацию:
```python
from typing import Callable, Awaitable

send_notification: Callable[[int, str], Awaitable[None]],
```

---

### 12. ✅ ИСПРАВЛЕНО — Глобальные кеши без очистки в `trending.py`

**Файл:** `bot/handlers/trending.py` (строки 20-21)

**Проблема:**
```python
_trending_movies_cache = {}
_trending_series_cache = {}
```
Глобальные кеши без TTL или ограничения размера могут привести к утечке памяти при длительной работе бота.

**Решение:**
Добавить TTL-кеширование или использовать `@lru_cache` с maxsize.

---

### 13. 💡 Хардкод констант

**Файлы:**
- `bot/handlers/downloads.py`: `TORRENTS_PER_PAGE = 5`
- `bot/handlers/calendar.py`: `CALENDAR_DAYS = 7`
- `bot/services/calendar_notification_service.py`: `CHECK_INTERVAL = 3600`

**Решение:**
Вынести в конфигурацию (`bot/config.py`) для гибкости настройки.

---

### 14. ✅ ИСПРАВЛЕНО — Смешанный язык в комментариях и строках

**Проблема:**
Код содержит комментарии на английском, но пользовательские сообщения на русском. Это нормально, но некоторые ошибки выводятся на английском:
- `"Could not grab release or trigger search"` (add_service.py)
- `"Movie search triggered"` (add_service.py)

**Решение:**
Унифицировать сообщения об ошибках на русский для пользовательского интерфейса.

---

### 15. 💡 Возможное исключение при работе с BaseAPIClient

**Файл:** `bot/clients/base.py` (метод `check_connection`)

**Проблема:**
Метод `check_connection` использует `/api/v1/system/status`, но Radarr/Sonarr используют `/api/v3/`. Prowlarr действительно использует v1, но для универсальности это может вызвать проблемы.

**Замечание:**
Это уже решено: `RadarrClient` и `SonarrClient` переопределяют `check_connection` и используют `/api/v3/system/status`.

---

## Рекомендации по улучшению

### R1. Добавить миграции базы данных
Сейчас схема создаётся в `_create_tables()`. При изменении схемы старые базы не будут обновлены.

### R2. Добавить health checks для Docker
В `Dockerfile` нет `HEALTHCHECK`. Рекомендуется добавить проверку работоспособности бота.

### R3. Улучшить обработку ошибок в middleware
`RateLimitMiddleware` использует глобальный словарь без очистки старых записей. При большом количестве пользователей это может привести к утечке памяти.

### R4. Добавить retry-логику для Telegram API
При отправке уведомлений нет повторных попыток в случае временных ошибок Telegram.

### R5. Добавить валидацию environment variables
При запуске неплохо проверять доступность всех сервисов (Prowlarr, Radarr, Sonarr) и выдавать понятные ошибки.

---

## Приоритет исправлений

| Приоритет | Номер | Описание | Статус |
|-----------|-------|----------|--------|
| 🔴 Высокий | #1 | Добавить `radarr_enabled`/`sonarr_enabled` | ✅ |
| 🔴 Высокий | #2 | Исправить вызовы `is_release_notified`/`mark_release_notified` | ✅ |
| 🟡 Средний | #3 | Заменить `datetime.utcnow()` | ✅ |
| 🟡 Средний | #4 | Использовать синглтоны вместо создания новых клиентов | ✅ |
| 🟡 Средний | #5 | Переименовать `ConnectionError` | ✅ |
| 🟡 Средний | #7 | Добавить проверку `callback.message` | ✅ |
| 🟢 Низкий | #6 | Удалить неиспользуемую переменную | ✅ |
| 🟢 Низкий | #9 | Удалить неиспользуемые импорты | ✅ |
| 🟢 Низкий | #11 | Улучшить типизацию | ✅ |
| 🟢 Низкий | #12 | Добавить TTL для кешей | ✅ |
| 🟢 Низкий | #14 | Унифицировать сообщения на русский | ✅ |

---

## Файлы, требующие изменений

1. `bot/config.py` — добавить свойства `radarr_enabled`, `sonarr_enabled`
2. `bot/services/calendar_notification_service.py` — исправить вызовы методов БД
3. `bot/clients/base.py` — переименовать `ConnectionError`
4. `bot/db.py` — заменить `datetime.utcnow()`
5. `bot/models.py` — заменить `datetime.utcnow()` в default_factory
6. `bot/handlers/calendar.py` — добавить проверку `callback.message`
7. `bot/handlers/trending.py` — удалить неиспользуемые импорты
8. `bot/handlers/settings.py` — использовать синглтоны клиентов
9. `bot/handlers/status.py` — использовать синглтоны клиентов
10. `bot/services/notification_service.py` — улучшить типизацию
