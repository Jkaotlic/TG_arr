# Предложения по улучшению TG_arr (функционал + UI)

Источник: deep-research по похожим проектам (Searcharr, Addarr/Cyneric-Addarr, Searcharr-Plus,
Doplarr, Overseerr/Jellyseerr/Seerr) + документация aiogram 3, сведённый со знанием кода TG_arr.
Все пункты с оглядкой на Raspberry Pi 4 и single-household деплой (один доверенный круг пользователей).

> ✅ **В master (TDD, коммиты c292e96/e44f81d):** #5 внешние ссылки, #7 `/health`,
> #2 пресеты мониторинга сезонов, #4 pre-grab проверка «уже в Emby».
>
> ✅ **На ветке `feat/arch-improvements` (PR):** #6 `/users` runtime (b1bcdb7), #8 webhook-уведомления
> config-gated (e7b4811), #1 типизированный CallbackData — **down-payment**: мигрирована пагинация
> (`PageCB`), остальные семейства колбэков — механический follow-up (a430ad3). Итог ветки: **396 passed**.
>
> ⏸️ **#3 FSM-миграция — отложено осознанно.** Текущее состояние сессий (SQLite) рабочее и только что
> укреплено аудитом (RACE-02 write-lock, RACE-04 update_session). Полная замена на aiogram FSM — это
> переписывание рабочего кода ради архитектуры, с реальным риском регрессий и **без конкретной пользы
> сейчас**: мастер выбора (#2) и все интерактивные флоу уже работают на текущей системе. Делать имеет
> смысл только если появится флоу, который на SQLite-сессиях реализовать нельзя. Пока — не трогаем.

## Похожие проекты (ландшафт)

| Проект | Что делает хорошо |
|--------|-------------------|
| **Searcharr** (toddrob99) | Telegram-бот: результат-карточки с пагинацией ◀▶, поп-ауты в TMDB/IMDb/TVDB, runtime `/users` grant/revoke admin |
| **Addarr / Cyneric-Addarr** | Пошаговый мастер добавления (поиск→выбор→профиль/папка→подтверждение), **выбор сезонов**, admin-gate на download-client |
| **Searcharr-Plus** | Роли Admin/Friend, квоты (3 запроса/день), Accept/Decline-флоу, pre-grab availability check |
| **Doplarr** (Discord) | Полностью на нативных компонентах; спрашивает недостающие параметры (профиль/папка) в момент запроса |
| **Overseerr / Seerr** | Эталон request/approval-UX, гранулярные права (битмаска ~27 флагов), season-level requests, Emby/Jellyfin/Plex user-import |

---

## Рекомендую сделать (high impact, вписывается в Pi/household)

### 1. Типизированный `CallbackData` (aiogram 3 factory) — **архитектура, наивысший ROI**
**Зачем:** сейчас в [keyboards.py](../bot/ui/keyboards.py) колбэки — строковые префиксы (`"back"`, `"page:"`,
`"trending_back"`), а хендлеры парсят их `F.data.startswith(...)` + `removeprefix`. Ровно из этого выросли
баги аудита: **BUG-01** (`BACK` перехватывал тренды), **LOGIC-14** (`PAGE` ловил music-сессии). aiogram-фабрика
(`CallbackData` subclass с `prefix`, `.pack()/.unpack()`, `.filter(F.action==...)`) делает такие коллизии
**структурно невозможными** и инжектит типизированный объект в хендлер.
**Где:** `bot/ui/keyboards.py` (классы-фабрики вместо строк) + все `@router.callback_query` в handlers.
**Эффект/усилия:** очень высокий / средние (рефакторинг). **Pi:** бесплатно. Лимит Telegram 64 байта — учесть.
Источник: aiogram docs (CallbackData factory).

### 2. Выбор сезонов при добавлении сериала — **фича + закрывает спор BUG-04/BUG-32**
**Зачем:** сейчас `monitor_type` вычисляется автоматически ([search.py `_decide_monitor_type`](../bot/handlers/search.py)),
и мы спорили «existing монит всё» vs «none». Дать пользователю **явный выбор сезонов** (toggle-кнопки +
пресеты «Все / Будущие / Только этот») решает дилемму правильно. Sonarr `add_series` уже принимает
`seasons:[{seasonNumber,monitored}]` ([sonarr.py](../bot/clients/sonarr.py)) — нужен только UI-шаг.
**Где:** новый шаг-клавиатура в `bot/ui/keyboards.py` + ветка в `bot/handlers/search.py` перед grab сериала.
**Эффект/усилия:** высокий / средние. **Pi:** бесплатно. Источник: Cyneric-Addarr `SEASON_SELECT`, Overseerr.
⚠️ **Только сезоны, не эпизоды** — поэпизодный выбор не сделал даже Overseerr (issue #342), это non-goal.

### 3. Мастер выбора профиля/папки в момент grab (FSM)
**Зачем:** сейчас если у юзера не задан `radarr_quality_profile_id`/root folder — бот **молча берёт первый**
(`profiles[0]`/`folders[0]` в [search.py](../bot/handlers/search.py)). Doplarr/Addarr спрашивают недостающее
прямо в чате. Дать кнопочный выбор профиля/папки на лету (с пропуском шага, если вариант один).
**Где:** aiogram FSM (`set_state` + `update_data`) — заодно нативная замена самописным SQLite-сессиям; `search.py`, `settings.py`.
**Эффект/усилия:** высокий / средне-высокие (вводит FSM). **Pi:** бесплатно. Источник: Doplarr, Addarr, aiogram FSM docs.

### 4. Pre-grab проверка «уже в библиотеке Emby»
**Зачем:** add_service уже проверяет наличие в Radarr/Sonarr (`get_movie_by_tmdb`), но **не в Emby**. Дешёвый
dedup: перед grab спросить Emby «есть ли тайтл» → предупредить «уже доступно, всё равно качать?». TG_arr уже
ходит в Emby ([emby.py](../bot/clients/emby.py)).
**Где:** новый метод поиска по библиотеке в `EmbyClient` + проверка в `add_service.grab_*_release`.
**Эффект/усилия:** средний / низкие. **Pi:** дёшево (Emby локальный). Источник: Searcharr-Plus availability check.
❌ Проверку стриминговых сервисов (Plex/Netflix-availability) — **не делать**: внешние API, не для Pi/household.

### 5. Кнопки-ссылки TMDB/IMDb/TVDB + постер на карточке релиза
**Зачем:** Searcharr на каждой карточке даёт поп-ауты в метаданные. У TG_arr есть `imdb_id`/`tmdb_id`/`tvdb_id`
в моделях, постеры уже шлются в трендах (`answer_photo`). Добавить URL-кнопки (открыть в TMDB/IMDb) в
`release_details`/`movie_details` — **нулевая нагрузка на бэкенд** (URL-кнопки Telegram).
**Где:** `bot/ui/keyboards.py` (`InlineKeyboardButton(url=...)`).
**Эффект/усилия:** средний / очень низкие. **Pi:** бесплатно. Источник: Searcharr.

---

## Стоит рассмотреть (medium)

### 6. Runtime-управление пользователями `/users` (admin)
**Зачем:** сейчас добавить пользователя = править `ALLOWED_TG_IDS` в env + рестарт. Searcharr даёт `/users`
с кнопками grant/revoke/admin на лету. Таблица `users` в БД уже есть; middleware читает allowlist из env —
можно дополнить чтением из БД.
**Где:** новый admin-handler + `bot/middleware/auth.py` + `bot/db.py`. **Усилия:** средние. Источник: Searcharr, Addarr.

### 7. `/health`-дашборд (доступность arr + диск + qBit)
**Зачем:** собрать в одну команду то, что уже есть по кускам: `check_connection()` всех клиентов +
`RootFolder.free_space` + qBit-статус + активные загрузки. Быстрый «всё ли живо».
**Где:** расширить [status.py](../bot/handlers/status.py). **Усилия:** низкие. **Pi:** дёшево. Источник: синтез (Tautulli/Notifiarr-идея).

### 8. Webhook-уведомления «появилось в Emby» (вместо 60-сек polling)
**Зачем:** сейчас `NotificationService` **поллит qBittorrent каждые 60с** ([notification_service.py](../bot/services/notification_service.py)).
Radarr/Sonarr умеют Connect→Webhook (`on import/upgrade`) — мгновенное и точное «<Фильм> готов». Поднять
лёгкий aiohttp-endpoint на Pi, arr шлёт POST → бот уведомляет.
**Где:** мини aiohttp-сервер в `main.py` + подписка. **Усилия:** средние. **Pi:** лёгкий aiohttp, ок. Источник: синтез (Notifiarr-паттерн).

---

## Честно: тебе, скорее всего, НЕ нужно (single-household)

- **Request/approval-очередь + квоты на пользователя** (Searcharr-Plus, Overseerr) — это для друзей/публичных
  инстансов с недоверенными юзерами. У тебя allowlist доверенного круга. Делать только если появятся дети/гости
  с ограничениями. *(open question самого ресёрча — то же сомнение.)*
- **Поэпизодный выбор** — non-goal во всей экосистеме (даже Overseerr).
- **Plex/Jellyfin-поддержка** — ты на Emby. Jellyfin почти API-совместим, добавить дёшево, но смысла нет, пока не сменишь сервер.
- **Локализация/i18n** — ты русский single-household; поле `UserPreferences.language` мы как раз удалили (DEAD-10).
- **Streaming-availability checks** — внешние зависимости, не для Pi.

---

## Рекомендуемый порядок

1. **#5 ссылки/постеры** (час работы, мгновенный UX-выигрыш) →
2. **#7 `/health`** (дёшево, полезно) →
3. **#2 выбор сезонов** (фича + закрывает monitor-дилемму) →
4. **#1 типизированный CallbackData** (рефакторинг, но предотвращает целый класс багов) →
5. **#3 FSM-мастер** и **#4 Emby-dedup** →
6. **#8 webhooks**, **#6 `/users`** — по желанию.

Источники по каждому пункту — в таблице ландшафта и подписях. Полный research-вывод (10 находок, 3-0 верификация,
101 агент) — в задаче `wfepdk8ef`.
