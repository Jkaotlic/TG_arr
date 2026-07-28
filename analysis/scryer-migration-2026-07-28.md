# Миграция TG_arr: *arr → Scryer 0.17.2 + музыкальный контур (2026-07-28)

## Что изменилось в инфраструктуре

24.07.2026 медиастек переехал на **Scryer 0.17.2** (Rust-монолит). Sonarr (`:8989`) и
Radarr (`:7878`) остановлены навсегда. Prowlarr (`:9696`) жив, но обслуживает Scryer, а не бот.
Музыкальный контур: Lidarr (`:8686`, поднят в ходе этой работы) → slskd (`:5030`) →
`G:\Music\Library` → Navidrome (`:4533`).

## Как устроен Scryer API (проверено интроспекцией и живыми вызовами)

- Единственная точка входа — `POST /graphql`. REST нет. 540 типов в схеме.
- Аутентификация — JWT из мутации `login`, TTL 24 ч (`expiresAt` в ответе).
- **Ошибки приходят с HTTP 200** в поле `errors` при `data: null`.
- Идентификаторы: `titleId` — UUID; `qualityProfileId` — слаг (`"4k"`, `"1080p"`);
  у root-folder своего id нет вообще — идентичность это путь.
- Библиотеки: `movie_default_library` (профиль `4k`), `series_default_library` (`4k`),
  `anime_default_library` (`1080p`).

### Ключевое отличие от Prowlarr-флоу

Prowlarr искал релизы по свободному тексту. Scryer ищет **по тайтлу**: `searchReleases`
принимает `titleId`, то есть тайтл должен уже быть в каталоге. Отсюда новый порядок:

```
searchMetadataMulti(query)        → кандидаты по всем трём фасетам, один round-trip
ensure_title(candidate)           → находим или добавляем тайтл (unmonitored!)
searchReleases(titleId)           → кандидаты с candidateToken и вердиктом Scryer
queueExistingTitleDownload(token) → постановка в очередь
setTitleMonitored(true)           → только теперь включаем мониторинг
```

Каждый релиз возвращается с `qualityProfileDecision {allowed, releaseScore, blockCodes}` —
это уже применённый профиль качества плюс Rego-правила
`English Audio + Russian Subtitles`.

## Принятые решения на развилках

### 1. Тайтл добавляется **немониторируемым** на этапе поиска

`searchReleases` требует `titleId`, значит показать список релизов нельзя, не добавив тайтл.
Чтобы «просто посмотрел» не превращалось в «поставил на автоскачивание», `ensure_title`
добавляет с `monitored: false`, а `grab_release` включает мониторинг **после** успешной
постановки в очередь. Если Scryer отказал (CONFLICT) — мониторинг не включается.

### 2. Скоринг бота не спорит со Scryer, а достраивает его

`ScoringService.sort_results` теперь ранжирует так:

1. заблокированные профилем (`allowed=false`) — вниз (показываем, но не предлагаем первыми);
2. среди остальных решает `releaseScore` Scryer;
3. собственная эвристика бота — только тай-брейк и единственный критерий там,
   где вердикта нет (сессия, сохранённая до миграции).

### 3. Парсер качества из Prowlarr-клиента сохранён

`parsedRelease` у Scryer на живых данных возвращает `videoCodec: null` и `audio: null`
для большинства релизов русских трекеров, а скоринг и правила по озвучке/сабам на эти поля
опираются. Логика парсинга вынесена в `bot/services/release_parser.py` и **дополняет**
ответ Scryer: его значения всегда выигрывают, парсер лишь заполняет пустоты.

### 4. Аниме — отдельный фасет во всём флоу

Добавлен `ContentType.ANIME`, команда `/anime`, кнопка в выборе типа. Важная деталь:
маркер сезона (`S01E05`) раньше немедленно давал SERIES. Теперь он лишь **сужает** выбор до
series-vs-anime, потому что это разные библиотеки с разными профилями — «Frieren S01E05»
должен попасть в аниме-библиотеку. При недоступности метаданных откат к SERIES сохранён.

### 5. qBittorrent остаётся только как явный обход

Раньше бот автоматически уходил в qBittorrent, когда *arr отклонял релиз. Теперь вердикт
Scryer авторитетен, а обход — осознанное действие пользователя (`force_download=True`).

### 6. Navidrome требует учётных данных, которых нет в конфиге

Пароль пользователя Navidrome лежит в его БД (`C:\ProgramData\Navidrome\navidrome.db`),
в `navidrome.ini` его нет. Клиент написан и покрыт тестами, но включается только после
добавления `NAVIDROME_USERNAME` / `NAVIDROME_PASSWORD` в `.env`. Без них фича молча выключена
(`navidrome_enabled == False`) — предупреждение о неполной настройке выдаётся при старте.

## Что удалено

- `bot/clients/radarr.py`, `bot/clients/sonarr.py`, `bot/clients/prowlarr.py`;
- настройки `PROWLARR_*`, `RADARR_*`, `SONARR_*`, `PROWLARR_SEARCH_TIMEOUT/RETRIES`;
- семафор + circuit breaker в детекции: они существовали, потому что один запрос
  веером бил в три *arr одновременно (и клал их). Scryer отвечает одним вызовом;
- пары «профиль + папка» для Radarr и Sonarr в настройках — вместо них одна пара Scryer
  (это *переопределение*: по умолчанию применяется профиль библиотеки).

## Баги, найденные при миграции

| ID | Где | Суть |
|---|---|---|
| MIG-01 | `bot/handlers/settings.py` | Значение настройки приводилось к `int()`. Id профилей Scryer — слаги, любой выбор отклонялся как «Неверное значение». Теперь `int` только для числовых Lidarr-ключей. |
| MIG-02 | `bot/clients/scryer.py` | Путь root-folder в `callback_data` ломает aiogram: `:` — разделитель полей `CallbackData`, а `G:\radarr\Films` его содержит. Введён `root_folder_id()` — короткий sha1-дайджест пути (стабилен между рестартами, в отличие от индекса). |
| MIG-03 | `bot/clients/scryer.py` | GraphQL-документ запрашивал `episode { seasonNumber episodeNumber isSeasonPack }`, а реальный `ParsedEpisodePayload` — это `{season, episodeNumbers[]}`. Поймано live-smoke-тестом; на моках было незаметно. |
| MIG-04 | `bot/clients/slskd.py` | Цикл опроса поиска проверял дедлайн ДО первого запроса: при малом `SLSKD_SEARCH_TIMEOUT` поиск всегда возвращал пусто, ни разу не опросив slskd. |
| MIG-05 | `bot/services/search_service.py` | `asyncio.gather(return_exceptions=True)` глотал `CancelledError`, превращая отмену задачи в обычный «не удалось определить тип». Теперь отмена пробрасывается. |

## Безопасность

`downloadUrl` от Scryer содержит apikey Prowlarr, а `candidateToken` — это JWT, внутри
которого лежит тот же URL целиком (проверено декодированием живого токена). Ни то, ни другое
не логируется: `mask_release_secrets()` вычищает credential-параметры, а в логе постановки
в очередь от токена остаются последние 8 символов. `guid` релиза намеренно строится из
`infoHash` или пары «индексер + заголовок», а не из URL.

## Верификация

- `python -m ruff check bot/ tests/` — чисто.
- `python -m pytest tests/ -q` — **730 passed** (было 691 до миграции; 12 live-тестов
  пропускаются без учётных данных).
- Live smoke против рабочего Scryer 0.17.2 (`tests/test_live_scryer_smoke.py`) — **12 passed**:
  логин, `systemHealth`, `titles` (в т.ч. по фасетам), `searchMetadata`/`searchMetadataMulti`,
  `searchReleases`, профили и root-folders, библиотеки, очередь, календарь, wanted.
- Живая проверка slskd клиентом бота: health `ok=True` (v0.24.5, залогинен в Soulseek),
  поиск «Metallica Master of Puppets» → 5 сгруппированных кандидатов (FLAC, 4.2 GB / 137 треков).
- Живая проверка Lidarr после запуска службы: `/api/v1/system/status` → v3.1.2.4938, 27 артистов.
