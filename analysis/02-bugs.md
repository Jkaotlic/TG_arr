# Анализ багов TG_arr v1.0 (раунд 3)

Дата: 2026-05-08.
Фокус: жалобы пользователя
- (Ж1) "Плохо ищет контент" — мало/нерелевантные результаты.
- (Ж2) "Не даёт выбрать фильм/сериал/музыка" — кнопочный выбор отсутствует.
- (Ж3) "Не понимает фильм/сериал/музыка" — детект типа сломан.

Round 2 (apr-2026) закрыл BUG-01..33 в файлах `bot/handlers/search.py`, `music.py`, `services/search_service.py`. Этот раунд — анализ оставшихся проблем поиска и type-detection.

---

## Критические (ломают UX поиска — напрямую объясняют жалобы)

### BUG-01: `_title_matches` — substring `query_clean in title_lower` ловит ложные совпадения для коротких/общих запросов
- **Файл**: `bot/services/search_service.py:128-132`
- **Проблема**: Логика `if query_clean in title_lower or title_lower in query_clean` — слишком слабая для коротких слов. Для query="дюна" Lidarr может вернуть артиста "Дюна" (есть украинская группа), Radarr — фильм "Дюна". `_title_matches` сработает на ОБОИХ. В `detect_content_type` порядок проверки: artists → movies → series. **Артист побеждает фильм** → `ContentType.MUSIC` → пользователь делегируется в music flow и видит "Артист не найден"/неправильный список.
   ```python
   # search_service.py:94-97
   if artists:
       for a in artists[:3]:
           if self._title_matches(query, a.name, None):
               return ContentType.MUSIC   # MUSIC побеждает MOVIE/SERIES
   ```
- **Симптом для пользователя (Ж3)**: Запрос "Дюна" → бот молча уходит в музыкальный flow → "Артист не найден" или нерелевантные группы.
- **Риск**: Высокий
- **Решение**: добавить fuzzy-score (например, `difflib.SequenceMatcher`) с порогом >0.85 для music; либо требовать exact match для music без disambiguation; либо ставить music ПОСЛЕДНИМ в priority-цепи (movie → series → music).
- **Статус**: [ ]

### BUG-02: `_title_matches` для music передаёт `year=None` — теряет проверку коллизии
- **Файл**: `bot/services/search_service.py:96`
- **Проблема**: Вызов `self._title_matches(query, a.name, None)` всегда передаёт `year=None`, поэтому `_title_matches` пропускает `if query_year and year` и возвращает True при первом же substring-совпадении. Это в комбинации с BUG-01 даёт false MUSIC detection. Также в `_title_matches` нет проверки длины (`len(query_clean) < 3` → почти всё совпадёт substring-логикой).
- **Симптом для пользователя (Ж3)**: Любой 2-3 символьный артист в Lidarr (например "U2", "AC", "X") приоритетно мэтчится на любом фрагменте.
- **Риск**: Высокий
- **Решение**: для music либо exact-match (`query_lower == name_lower`) либо `len(query) >= 4` AND substring AND длина артиста близка к длине query.
- **Статус**: [ ]

### BUG-03: `detect_content_type` приоритезирует music над movies/series
- **Файл**: `bot/services/search_service.py:93-107`
- **Проблема**: Жёсткий порядок: artists → movies → series. Если пользователь вводит "Joker", Lidarr/MusicBrainz найдёт артиста "Joker" (множество диджеев/инди-исполнителей с этим именем); Radarr — фильм. Music побеждает → music flow. Аналогично "Friends" (артист и сериал), "Avatar" (фильм и японская группа), "Heroes" и т.д.
- **Симптом для пользователя (Ж3)**: Большая часть популярных запросов уходит в music по ошибке.
- **Риск**: Высокий
- **Решение**: 
  1. Считать score для каждого результата (popularity / followers / ratings) и брать max.
  2. Или: если movie И music нашлись — спрашивать пользователя (`ContentType.UNKNOWN` → кнопочный выбор).
  3. Или: music требует exact-match без disambiguation, иначе ставится последним.
- **Статус**: [ ]

### BUG-04: `process_search` теряет ContentType.UNKNOWN при detect и не показывает кнопочный выбор
- **Файл**: `bot/handlers/search.py:155-190, 194-201`
- **Проблема**: Если `detect_content_type` вернул `MOVIE`/`SERIES`/`MUSIC`, кнопочный выбор НЕ показывается. Пользователь не имеет контроля. Но детектор ошибается (BUG-01..03), поэтому "Joker" уходит в music без вопроса. Пункт "Не даёт выбрать контент" — это и есть данный баг: бот **сам** решает за пользователя.
- **Симптом для пользователя (Ж2)**: Пользователь хотел сериал "Дюна: Пророчество", бот автоматически решил movie → ищет фильм "Дюна 2021".
- **Риск**: Высокий
- **Решение**: всегда показывать выбор кнопок если найдено >1 типа (и movie, и series, и music одновременно матчатся); альтернатива — добавить кнопку "не то?" в результаты и "переоткрыть как сериал/музыка".
- **Статус**: [ ]

### BUG-05: При недоступности Radarr/Sonarr `detect_content_type` молча даёт wrong-detection или UNKNOWN
- **Файл**: `bot/services/search_service.py:69-91`
- **Проблема**: `asyncio.gather(..., return_exceptions=True)` корректно обрабатывает exception, но: если Radarr упал (медленный Wi-Fi → timeout от RPi), `movies = []` и detect просто пропускает фильмы. Пользователю кажется, что бот "не понимает фильм" — на самом деле это network issue. Также нет распознавания: timeout vs реально отсутствие — обе ситуации сливаются в `[]`.
   Конкретный сценарий: Radarr ответил, Lidarr ответил → music wins (BUG-01); или: Radarr timeout, Sonarr ответил → series wins, хотя реально это фильм.
- **Симптом для пользователя (Ж1, Ж3)**: Wi-Fi-сбой → пол-результатов пропадают, бот неверно угадывает тип.
- **Риск**: Высокий
- **Решение**: при exception от Radarr/Sonarr/Lidarr — возвращать `ContentType.UNKNOWN` вместо тихого пропуска, заставляя пользователя выбрать.
- **Статус**: [ ]

### BUG-06: `parse_query` извлекает год слишком жадно — ломает поиск на "Top Gun 1986" типа запросов
- **Файл**: `bot/services/search_service.py:223-230`
- **Проблема**: regex `[\(\[]?(\d{4})[\)\]]?` — необязательные скобки → сматчит ЛЮБУЮ 4-значную последовательность. Например "1984" (orwell) → year=1984, title="" (пусто). Тогда `search_term = (parsed.get("title") or "").strip() or query` — fallback на `query`, OK. Но "Blade Runner 2049" → year=2049, title="Blade Runner". Prowlarr получит "Blade Runner" без 2049 → много нерелевантных релизов из 1982. Аналогично "Star Wars Episode 1" → title="Star Wars Episode" (год=Episode 1?? нет, 4 цифры нет, ОК) — но "2001: Космическая Одиссея" → year=2001, title="Космическая Одиссея".
- **Симптом для пользователя (Ж1)**: "Blade Runner 2049" возвращает результаты по "Blade Runner" — пользователь видит мусор 80-х вместо 2017.
- **Риск**: Высокий
- **Решение**: год в `parse_query` использовать только для отображения / hint, а в Prowlarr search передавать ОРИГИНАЛЬНЫЙ query (включая год). Сейчас `process_search:200` передаёт parsed `title` БЕЗ года → Prowlarr теряет год как hint.
- **Статус**: [ ]

### BUG-07: `process_search` передаёт `parsed["title"]` (без года) в `search_releases` → Prowlarr теряет год
- **Файл**: `bot/handlers/search.py:200-201`
- **Проблема**: 
   ```python
   search_term = (parsed.get("title") or "").strip() or query
   results = await search_service.search_releases(search_term, content_type)
   ```
   Год удалён из `search_term`. Многие торрент-индексеры используют год как ключ распознавания — без него матчинг хуже. См. BUG-06: "Blade Runner 2049" → Prowlarr ищет "Blade Runner".
- **Симптом для пользователя (Ж1)**: Меньше точных результатов или путаница ремейков (Dune 1984 vs Dune 2021).
- **Риск**: Высокий
- **Решение**: передавать `query` (исходный) как primary, парсинг — для type-detection и фильтрации внутри. Или: реассамблировать `f"{title} {year}"`.
- **Статус**: [ ]

### BUG-08: `handle_release_selection` повторно ищет через `session.query` — игнорирует уточнённый title из Radarr
- **Файл**: `bot/handlers/search.py:430, 449`
- **Проблема**: 
   ```python
   movies = await search_service.lookup_movie(session.query)
   if movies:
       movie = movies[0]   # тупой берёт первый, не сопоставляет с релизом
   ```
   `session.query` — оригинальный (с грязным форматированием, например "блейдранер 2049"). `lookup_movie` вернёт список фильмов; берётся `[0]` — это может быть НЕ тот фильм, что в релизе. Например query="Aliens" → Radarr вернёт ["Aliens (1986)", "Aliens vs Predator (2004)", ...]. Пользователь выбрал релиз "Aliens.1986" — но `lookup_movie[0]` может быть "Aliens (2025)" если он популярнее по `popularity`. Movie info не сходится с релизом.
- **Симптом для пользователя (Ж1)**: После выбора релиза показывается описание ДРУГОГО фильма (поздняя версия/ремейк).
- **Риск**: Высокий
- **Решение**: использовать `release.detected_year` для фильтра lookup_movie, или искать по разделённому release.title (без quality tokens), или хотя бы matching по году релиза.
- **Статус**: [ ]

### BUG-09: `handle_type_selection` для MUSIC сбрасывает session, теряя `query` контекст для следующего шага
- **Файл**: `bot/handlers/search.py:294-296`
- **Проблема**: 
   ```python
   await db.delete_session(user_id)  # music flow starts its own session
   await process_music_search(callback.message, session.query, db_user, db)
   ```
   Но `process_music_search(message=callback.message, ...)` передаёт CallbackMessage как Message, использует `message.answer(...)` → бот отправит "🔍 Ищу артистов в Lidarr..." как новое сообщение, не редактируя старое. UX: дубль сообщений. Также если Lidarr недоступен (ошибка `services is None`), `await db.delete_session` уже выполнен — пользователь теряет введённый query без шанса retry с тем же текстом.
- **Симптом для пользователя (Ж2)**: При выборе "🎵 Музыка" появляется второе сообщение, старое остаётся "битое".
- **Риск**: Средний
- **Решение**: либо edit_text старого сообщения через `callback.message.edit_text(...)`, либо удалять session ПОСЛЕ успешного `process_music_search`.
- **Статус**: [ ]

### BUG-10: `cmd_movie`/`cmd_series` — `replace("/movie", "")` глобальный, ломает запрос содержащий "/movie" в тексте
- **Файл**: `bot/handlers/search.py:83, 98`
- **Проблема**: 
   ```python
   query = message.text.replace("/movie", "").strip()
   ```
   `replace` без `, 1` удаляет ВСЕ вхождения. Запрос "/movie The Dark Knight movie" → "The Dark Knight ". OK, но: "/movie series 1" → query="series 1" → `parse_query` сработает на "season 1" вариант → `season=1` → detect SERIES, ВНУТРИ /movie cmd! Контракт "/movie" не выполняется. Исключения нет, но logic broken.
   Также `cmd_search`: `message.text.replace("/search", "").strip()` — то же самое.
- **Симптом для пользователя (Ж3)**: `/movie Mr. Robot s01` → бот ищет как series вопреки команде /movie. Нет — /movie передаёт `ContentType.MOVIE` явно, поэтому detect не вызывается. OK для /movie, но если query содержит "/movie" в середине — обрезается.
- **Риск**: Низкий
- **Решение**: `message.text.split(maxsplit=1)[1] if " " in message.text else ""` или `replace("/movie", "", 1)`.
- **Статус**: [ ]

### BUG-11: `detect_content_type` — series-pattern `r"s\d{1,2}"` ловит ложные совпадения в названиях
- **Файл**: `bot/services/search_service.py:54-66`
- **Проблема**: regex `s\d{1,2}` без word-boundary матчит "fast5" → s5? Нет, шаблон такой что нужен 's' + цифра. Но: "Glass" → "glass" → 's' с цифрой? нет. Но "Mass Effect 2" → 's' + ' ' + '2' — паттерн `s\d` без `\b` не сматчит (между s и 2 пробел). А вот "lost s5" → s5 — OK. Контр-пример: "9-1-1: Lone Star" → "1-1" не матчит. "1408" → нет 's'. OK, скорее всего safe.
   Но: "S.W.A.T." → "s.w.a.t." → 's' + 'w' — нет цифры. OK.
   Реальный кейс: "S1" в названии (например "Final Fantasy V Star Ocean") — нет, тут надо s+цифра.
   Худший кейс — "9s1" внутри название без пробела. Маловероятно.
- **Симптом для пользователя (Ж3)**: Маловероятно.
- **Риск**: Низкий
- **Решение**: добавить `\b` границы: `r"\bs\d{1,2}\b"`.
- **Статус**: [ ]

---

## Высокие (UX-проблемы поиска и сессий)

### BUG-12: `handle_type_selection` MOVIE/SERIES вызывает `process_search` которое перевызывает `parse_query`, `detect_content_type` (хоть тип уже задан) — лишние вызовы и потенциальный гонщик
- **Файл**: `bot/handlers/search.py:298-313, 154-162`
- **Проблема**: При нажатии "Фильм"/"Сериал" `process_search(content_type=ContentType.MOVIE)` НЕ вызывает `detect_content_type` — OK (`if content_type == ContentType.UNKNOWN`). Но всё равно вызывается `parse_query`, и далее `parsed["season"]` НЕ переопределяет content_type (он уже MOVIE). OK логически. **Но**: используется `callback.message` как `Message` для `message.answer(...)` — это валидно (`MaybeInaccessibleMessage`), однако `message.from_user` будет ботом, не юзером. Поэтому в `process_search` переменная `db_user` корректна (передана как arg). OK. Не баг как таковой, но фрагильно.
- **Симптом для пользователя**: нет прямого. Лишние вызовы создают latency на медленной сети.
- **Риск**: Низкий
- **Решение**: добавить флаг `skip_parse_query` или передавать уже распарсенные данные.
- **Статус**: [ ]

### BUG-13: `handle_release_selection` — `if not session.results` уберёт legitimate empty-results case
- **Файл**: `bot/handlers/search.py:396-398`
- **Проблема**: `if not session or not session.results` — если session есть, но results пустой (после ошибки сериализации Pydantic), пользователь увидит "Сессия истекла" — misleading.
- **Симптом для пользователя**: "Сессия истекла" в неподходящий момент (на самом деле session есть).
- **Риск**: Низкий
- **Решение**: разделять `session is None` (истекла) vs `not session.results` (нет данных).
- **Статус**: [ ]

### BUG-14: `_artist_candidates` global dict — race при concurrent запросах от одного пользователя
- **Файл**: `bot/handlers/music.py:47, 132-134, 180-184`
- **Проблема**: Пользователь делает /music Metallica, не дождавшись ответа делает /music ABBA. Два concurrent process_music_search — gloabl `_artist_candidates[user_id]` перезаписывается. Если callback `artist:5` пришёл от первого списка (Metallica) после второго (ABBA), idx=5 укажет на артиста из ABBA-списка. Открывается неверный артист.
- **Симптом для пользователя (Ж2/Ж3)**: Выбираешь "Metallica" — открывается "Madonna" или другой случайный артист из второго поиска.
- **Риск**: Средний
- **Решение**: хранить candidates в БД-сессии (как `session.results` для releases) либо привязать к Telegram message_id (key = (user_id, msg_id)).
- **Статус**: [ ]

### BUG-15: `_artist_candidates` обрезается до 25 (`artists[:25]`) но cleanup_overflow проверяет всю структуру
- **Файл**: `bot/handlers/music.py:39-42, 134`
- **Проблема**: `_cleanup_if_overflow(_artist_candidates)` срабатывает при `len(_artist_candidates) > 100` (юзеров). Удаляются ВСЕ записи всех юзеров. Если в этот момент у user_A был открыт artist_list, его выбор `artist:N` вернёт "Выбор истёк" — даже если делал поиск 5 минут назад. На активном чате — приемлемо, но дискомфорт.
- **Симптом для пользователя (Ж2)**: При большом количестве пользователей выбор артиста "истекает" мгновенно.
- **Риск**: Низкий
- **Решение**: TTL вместо общего clear; либо хранить в БД-сессии.
- **Статус**: [ ]

### BUG-16: `process_music_search` — нет fallback на музыкальный poolarr-search при отсутствии артиста в Lidarr
- **Файл**: `bot/handlers/music.py:118-130`
- **Проблема**: Если Lidarr вернул `[]` — "Артист не найден". Однако на Prowlarr могут быть music-релизы (через 3xxx categories) даже если MusicBrainz не знает артиста (russian-rock без MB-entry). Бот не показывает Prowlarr-результаты в music-flow вообще — только Lidarr lookup.
- **Симптом для пользователя (Ж1)**: Поиск русских/нишевых артистов даёт "Артист не найден", хотя на rutracker есть.
- **Риск**: Средний (ограничение архитектуры — может не быть багом)
- **Решение**: при пустом Lidarr fallback на `prowlarr.search(query, ContentType.MUSIC)`.
- **Статус**: [ ]

### BUG-17: `_extract_year` Prowlarr берёт ПЕРВЫЙ найденный год — багует на multi-year релизах
- **Файл**: `bot/clients/prowlarr.py:308-323`
- **Проблема**: Релиз "The Office US 2005-2013 Complete S01-S09" — `_extract_year` вернёт 2005. Но если кто-то введёт year=2013 в query, scoring/sorting не учтёт; либо scoring задействует "year" в `result.detected_year=2005` → пользователь думает релиз 2005 года.
- **Симптом для пользователя (Ж1)**: Search results показывают неправильный год для multi-year contents.
- **Риск**: Низкий
- **Решение**: для multi-year (2005-2013) брать min год; либо хранить tuple(start, end).
- **Статус**: [ ]

### BUG-18: `_is_season_pack` — regex `s\d{1,2}(?:\b|[\.\s\-])` для S01 без episode — некоторые релизы упускают
- **Файл**: `bot/clients/prowlarr.py:353-373`
- **Проблема**: Релиз "Show.Name.S01.1080p.WEB-DL" — `s01.` (после s01 точка) → паттерн `s\d{1,2}(?:\b|[\.\s\-])` сматчит → season pack OK. Но "Show.Name.S01-S09.Complete" — `s01-s09` → паттерн `s\d{1,2}(?:\b|[\.\s\-])` → s01- → match → season pack True. Но `_extract_season_episode` найдёт `s\d{1,2}(?![e\d])` → s01 → season=1, episode=None → OK. **Но**: одиночный пакет S01-S09 будет помечен как `is_season_pack=True` с `detected_season=1`. В `_execute_grab` `monitor_type = "all"` (BUG-32 fixed), но user думает это сезон 1.
- **Симптом для пользователя (Ж1)**: Multi-season pack помечается как season 1 в выдаче.
- **Риск**: Низкий
- **Решение**: специальный pattern для `s\d+-s\d+` (multi-season) → `is_season_pack=True, detected_season=None`.
- **Статус**: [ ]

### BUG-19: Filter `r.detected_type == content_type or r.detected_type == ContentType.UNKNOWN` пропускает чужие категории
- **Файл**: `bot/services/search_service.py:163-164`
- **Проблема**: 
   ```python
   results = [r for r in results if r.detected_type == content_type or r.detected_type == ContentType.UNKNOWN]
   ```
   Если `content_type == MOVIE`, пропускаем `MOVIE` и `UNKNOWN`. Но многие индексеры выдают релизы с категориями `MUSIC` (3000) ВНУТРИ movie-search (ошибочно класифицированные). Эти релизы НЕ пропадут — попадут как UNKNOWN, либо будут отфильтрованы. Но это ещё хуже: пропускаются релизы у которых `detected_type==SERIES` хотя query было MOVIE. Если индексер ошибся в категории, пользователь не увидит свой фильм.
   А `Prowlarr.search` уже фильтрует по `categories=MOVIE_CATEGORIES`, так что обычно `detected_type` = MOVIE/UNKNOWN. Но: Prowlarr API возвращает все matched cats; если релиз в нескольких cat (e.g. 2000 и 5000), обе попадают.
- **Симптом для пользователя (Ж1)**: Меньше результатов, чем выдаёт Prowlarr напрямую.
- **Риск**: Средний
- **Решение**: НЕ фильтровать по detected_type, доверять Prowlarr-категориям; либо доверять только когда detected_type явно конфликтует.
- **Статус**: [ ]

### BUG-20: `Prowlarr.search` `params["categories"] = list_of_int` — не все индексеры понимают list-кодирование httpx
- **Файл**: `bot/clients/prowlarr.py:53-60`
- **Проблема**: httpx сериализует `params={"categories": [2000, 2010, ...]}` как `?categories=2000&categories=2010&...` — Prowlarr API это принимает (DRF-style). Но если httpx рендерит как `?categories=2000,2010` (CSV), Prowlarr игнорирует. Проверка: httpx-default — repeat-each. OK, не баг, но fragile.
- **Симптом для пользователя**: нет.
- **Риск**: Низкий (защитная нота)
- **Решение**: явно использовать `httpx.QueryParams([("categories", c) for c in cats])`.
- **Статус**: [ ]

### BUG-21: `Prowlarr.search` timeout=60.0 hardcoded — игнорирует `settings.http_timeout`
- **Файл**: `bot/clients/prowlarr.py:66`
- **Проблема**: `await self.get("/api/v1/search", params=params, timeout=60.0)` — 60 секунд. На медленной RPi-Wi-Fi это разумно, но settings.http_timeout=30s дефолт. Если админ хочет больше (на VPS slow link) — не сможет настроить. Зато: если админ хочет жёсткие 10s — не сможет. Hardcoded value — несоответствие.
- **Симптом для пользователя**: нет, скорее operational.
- **Риск**: Низкий
- **Решение**: `timeout=settings.prowlarr_search_timeout` (новый ключ конфига).
- **Статус**: [ ]

### BUG-22: `detect_content_type` `query.strip() < 4` отдаёт UNKNOWN — но "Mob"/"Up"/"Her" — реальные фильмы из 2-3 букв
- **Файл**: `bot/services/search_service.py:48-49`
- **Проблема**: PERF-06 защита для коротких запросов. Но "Up", "It", "Her", "X" — реальные фильмы с очень короткими названиями. Такие запросы → UNKNOWN → пользователь видит выбор кнопок. OK; но детект мог бы успеть на "Up" (2009).
- **Симптом для пользователя (Ж2)**: Для коротких legitimate названий всегда показывается выбор — лишний клик.
- **Риск**: Низкий
- **Решение**: убрать порог 4 ИЛИ `process_search` переходит сразу в Prowlarr search (без detect) для короткого query — скрывая лишний клик.
- **Статус**: [ ]

---

## Средние

### BUG-23: `_validate_download_url` блокирует приватные IP — но Prowlarr может legitimately отдавать локальный URL
- **Файл**: `bot/services/add_service.py:70-105`
- **Проблема**: SSRF-protection правильно, но если Prowlarr на same-LAN VPS отдаёт download_url с private IP (DHT-tracker, локальный proxy), `_validate_download_url` отклоняет — push_release не пускает релиз. На RPi-deployment: prowlarr на VPS, индексеры наружу, всё public. OK. Но edge-case: rutracker возвращает proxy-URL с RFC1918 → блок.
- **Симптом для пользователя (Ж1)**: Релиз нельзя скачать (молча падает в fallback `release_rejected=True` → "Релиз отклонён").
- **Риск**: Средний
- **Решение**: env-флаг `ALLOW_PRIVATE_DOWNLOAD_URLS=true`; или whitelist domains.
- **Статус**: [ ]

### BUG-24: `db.save_session` НЕ atomic — между save и read возможна гонка из 2-х параллельных callbacks от одного user
- **Файл**: `bot/db.py:321-347`
- **Проблема**: `INSERT...ON CONFLICT DO UPDATE` атомарен на уровне SQL. Но `get_session` → mutate object → `save_session` — два таких потока могут перезаписать друг друга. Pre-existed BUG-30 (Round 2). Решение не реализовано, статус LOW.
- **Симптом для пользователя**: Двойные клики путают session selected_result. Редко.
- **Риск**: Низкий
- **Решение**: per-user asyncio.Lock либо optimistic concurrency через updated_at.
- **Статус**: [ ]

### BUG-25: `SearchSession.results` deserialize может упасть на `ContentInfo` discriminator при отсутствии `content_model_type`
- **Файл**: `bot/models.py:259-267, 270-282`, `bot/db.py:359-381`
- **Проблема**: Старые сессии (созданные до добавления `content_model_type`) при `model_validate` упадут с `ValidationError: Unable to extract tag using discriminator`. Сейчас `db.get_session` ловит exception → удаляет session → `None`. OK, но это значит миграция: после деплоя все active sessions становятся "истекли". Не критично, но pain point на каждом deploy.
- **Симптом для пользователя**: после деплоя текущая search session "истекла", надо повторить.
- **Риск**: Низкий
- **Решение**: backwards-compatible parser с default discriminator.
- **Статус**: [ ]

### BUG-26: `process_search` exception handler шлёт generic "Поиск временно недоступен" — теряет конкретную причину
- **Файл**: `bot/handlers/search.py:267-269`
- **Проблема**: Любая ошибка (timeout Prowlarr, JSON-decode, Pydantic validation) → "Поиск временно недоступен". Пользователь не понимает "это временно или я что-то сделал не так?". В Round 2 уже фикс был, но всё ещё generic.
- **Симптом для пользователя (Ж1)**: Непонятно, что не так — повторять или нет.
- **Риск**: Низкий
- **Решение**: разделить `ServiceConnectionError` (тайм-аут — повторите) vs `APIError` (проблема индексера) vs Pydantic (баг бота).
- **Статус**: [ ]

### BUG-27: `_title_matches` regex год `[\(\[]?(\d{4})[\)\]]?` сматчит ЛЮБУЮ 4-цифру в query (как в parse_query)
- **Файл**: `bot/services/search_service.py:117-124`
- **Проблема**: Тот же баг что и BUG-06, в другом месте. Запрос "Joker 2019 reactor 2049" → `query_year` = 2019, потом `re.search(...).start()` определит позицию первого года, обрезает. Логика `query_year` (line 117) и `year_match_clean` (line 121) ищут разные вещи без `[\(\[]` — две различные регулярки → query_year=2019, query_clean — обрезка по второму regex. Несогласованно.
- **Симптом для пользователя (Ж3)**: detect неправильно работает с числами в названиях.
- **Риск**: Средний
- **Решение**: одна регулярка для года, требующая бордер `\b(\d{4})\b` + диапазон 1900-2100.
- **Статус**: [ ]

### BUG-28: Tenacity `stop_after_attempt(2)` — только 1 retry → недостаточно для медленного Wi-Fi-RPi
- **Файл**: `bot/clients/base.py:103`
- **Проблема**: 2 попытки (1 + 1 retry). На RPi-WiFi с TLS-handshake до VPS, transient timeout > 30s — частая ситуация. После 1 retry — provideErrorMessage. Больше пользователю не дать шанса.
- **Симптом для пользователя (Ж1)**: "Поиск временно недоступен" каждый 3-й запрос — на самом деле 2-й retry бы сработал.
- **Риск**: Средний (в комбинации с BUG-26)
- **Решение**: `stop_after_attempt(3)` для GET-методов; не трогать для grab (POST non-idempotent — `_post_no_retry`).
- **Статус**: [ ]

### BUG-29: `parse_query` quality detection заменяет "4k" на "2160p", но НЕ удаляет "4k" из title
- **Файл**: `bot/services/search_service.py:248-253`
- **Проблема**: 
   ```python
   for q in quality_patterns:
       if q in query_lower:
           result["quality"] = q if q != "4k" else "2160p"
           result["title"] = re.sub(re.escape(q), "", result["title"], flags=re.IGNORECASE).strip()
           break
   ```
   `q` итерируется по patterns, и для "4k" удаляет "4k" из title — OK. НО: order patterns: `["2160p", "4k", "1080p", "720p", "480p"]`. Если query = "Dune 2021 4k", `2160p in query_lower` → False (нет 2160p в query); следующая итерация `4k in query_lower` → True; quality=2160p, title удаляет "4k". OK.
   Но: query = "Dune 2160p 4k" → first iter `2160p` matches, quality=2160p, удаляет "2160p". Title = "Dune 4k" — "4k" остаётся. Передаётся в Prowlarr → сужает результаты.
- **Симптом для пользователя (Ж1)**: Quality tokens частично остаются в title, ломая поиск.
- **Риск**: Низкий
- **Решение**: убрать `break`, прогнать все паттерны; или удалять обе ("4k" и "2160p") если matched.
- **Статус**: [ ]

### BUG-30: `parse_query` не учитывает Russian quality tokens "4К", "ультраHD"
- **Файл**: `bot/services/search_service.py:248`
- **Проблема**: Только английские tokens. "Дюна 4К 2160p" → "4К" остаётся в title (английская "K" vs кириллическая "К" разные codepoints). Prowlarr может не пройти.
- **Симптом для пользователя (Ж1)**: Русские пользователи пишут "4К" → попадает в title.
- **Риск**: Низкий
- **Решение**: добавить кириллические синонимы.
- **Статус**: [ ]

### BUG-31: `process_search` query length validation — minimum 2 символа, но `detect_content_type` требует 4 → inconsistency
- **Файл**: `bot/handlers/search.py:137-139`, `bot/services/search_service.py:48-49`
- **Проблема**: `if len(query) < 2: "слишком короткий"`. OK. Но при 2-3 символьном query `detect_content_type` всегда возвращает UNKNOWN → кнопочный выбор. Получаем странный UX: "/search Up" → ВСЕГДА выбор (BUG-22). Пользователь не понимает почему.
- **Симптом для пользователя (Ж2)**: Лишний клик для коротких queries.
- **Риск**: Низкий
- **Решение**: либо синхронизировать пороги, либо явно сообщать "Запрос короткий — выберите тип".
- **Статус**: [ ]

### BUG-32: `process_music_search` пишет 'Артист не найден' но БД-session не удаляется
- **Файл**: `bot/handlers/music.py:125-130`
- **Проблема**: При отсутствии артистов return без `db.delete_session(user_id)`. Стары session (если был от другого поиска) остаётся. При нажатии любой callback от старой session — она триггернётся. Чисто side-effect.
- **Симптом для пользователя (Ж2)**: Старая сессия (предыдущий фильм) реактивируется.
- **Риск**: Низкий
- **Решение**: `await db.delete_session(user_id)` перед return.
- **Статус**: [ ]

### BUG-33: `cmd_music` `replace("/music", "", 1)` — корректно, но `cmd_search/cmd_movie/cmd_series` без `, 1`
- **Файл**: `bot/handlers/search.py:68, 83, 98` vs `bot/handlers/music.py:74`
- **Проблема**: Inconsistency. cmd_music сделал правильно (`, 1`), остальные — нет. См. BUG-10.
- **Симптом для пользователя**: см. BUG-10.
- **Риск**: Низкий
- **Решение**: унифицировать.
- **Статус**: [ ]

### BUG-34: `handle_text_search` — фильтр `~F.text.in_(MENU_BUTTONS)` чувствителен к будущим изменениям меню
- **Файл**: `bot/handlers/search.py:114, 39-43`
- **Проблема**: Если в `Keyboards.main_menu()` появится новая кнопка не добавленная в `MENU_BUTTONS`, текст кнопки попадёт в search → "не понимает кнопку". Сейчас все нынешние кнопки перечислены, но добавление новой требует ручного добавления в MENU_BUTTONS. Maintenance hazard.
- **Симптом для пользователя**: после деплоя новой кнопки, нажатие на неё ищет её текст в Prowlarr.
- **Риск**: Низкий
- **Решение**: получать список из `Keyboards.main_menu()` programmatically.
- **Статус**: [ ]

---

## Низкие / Edge-cases

### BUG-35: `Prowlarr._normalize_result` — leechers parsing использует or-fallback, может вернуть None для valid 0
- **Файл**: `bot/clients/prowlarr.py:111-112`
- **Проблема**: 
   ```python
   leechers = int(item.get("leechers") or item.get("peers") or 0) if item.get("leechers") or item.get("peers") else None
   ```
   Если `leechers=0` (валидное!), `0 or item.get("peers")` → берёт peers; если оба 0 — `None`. Логически: 0-ливера — валидный кейс (новый релиз, mkv но zero-peers), но парсится как None. Минор.
- **Симптом для пользователя**: нет (UI отображает "—" или "0").
- **Риск**: Низкий
- **Решение**: явное `None`-check вместо truthy.
- **Статус**: [ ]

### BUG-36: `Prowlarr._extract_season_episode` — паттерн `s(\d{1,2})(?![e\d])` ловит "S01" но не "S001"
- **Файл**: `bot/clients/prowlarr.py:335-338`
- **Проблема**: max 2 цифры в номере сезона. Сериалы с >99 сезонов? Маловероятно. OK.
- **Симптом**: нет.
- **Риск**: Низкий
- **Решение**: разрешить 3 цифры.
- **Статус**: [ ]

### BUG-37: `Prowlarr._is_season_pack` `season pack` literal string — пропускает "season packs" (мн.ч.)
- **Файл**: `bot/clients/prowlarr.py:358`
- **Проблема**: `if any(x in title_lower for x in ["complete season", "season pack", "full season"])` — "season packs" plural тоже сматчит ("season pack" substring). OK.
- **Симптом**: нет.
- **Риск**: -
- **Статус**: [ ] (false alarm — substring уже работает)

### BUG-38: `_artist_candidates[user_id] = artists[:25]` — обрезка до 25 — пользователь не увидит 26-го артиста
- **Файл**: `bot/handlers/music.py:134`
- **Проблема**: MusicBrainz может вернуть >25 однофамильцев. Lidarr `lookup_artist` возвращает все, но handler обрезает до 25 без пагинации.
- **Симптом для пользователя (Ж2)**: Если артист на 26-м месте — невидим.
- **Риск**: Низкий
- **Решение**: пагинация как для releases.
- **Статус**: [ ]

### BUG-39: `_normalize_result` `seeders=0 → seeders=None` через truthy check
- **Файл**: `bot/clients/prowlarr.py:109-110`
- **Проблема**: 
   ```python
   if "seeders" in item:
       seeders = int(item["seeders"]) if item["seeders"] is not None else None
   ```
   OK — проверка `is not None`. **Но**: scoring `if result.seeders is not None and result.seeders > 0` (scoring.py:204) — здесь Defensive, OK.
- **Симптом**: нет.
- **Риск**: -
- **Статус**: [ ] (false alarm)

### BUG-40: ContentType.UNKNOWN утечка из `process_search` если content_type стал MUSIC и music handler сам упал
- **Файл**: `bot/handlers/search.py:165-170`
- **Проблема**: `if content_type == ContentType.MUSIC: ... await process_music_search(...); return`. Если `process_music_search` бросит exception (Lidarr down), exception всплывёт в `process_search` `except Exception` → "Поиск временно недоступен". Юзер не увидит конкретной ошибки про Lidarr; status_msg уже удалён (`await status_msg.delete()`). OK как fallback.
- **Симптом**: непонятная ошибка на нестабильной music backend.
- **Риск**: Низкий
- **Решение**: catch внутри music delegation, fallback к obj-search или show error.
- **Статус**: [ ]

### BUG-41: `_validate_download_url` использует `socket.getaddrinfo` через `asyncio.to_thread` — на RPi-OS thread starvation
- **Файл**: `bot/services/add_service.py:93-95`
- **Проблема**: каждый grab вызывает DNS lookup в default thread pool. На RPi (4 cores) при много-grab случае может быть starvation. Не баг для одиночного user.
- **Симптом**: нет.
- **Риск**: Очень низкий
- **Решение**: aiodns / httpx native.
- **Статус**: [ ]

---

## Сводка

**Всего**: 41 пункт. Из них **критичных по жалобам пользователя**: 11 штук (BUG-01..11).

| ID | Жалоба | Резюме |
|----|---|---|
| BUG-01 | Ж3 | substring `_title_matches` ловит false music match |
| BUG-02 | Ж3 | music без year-check всегда matches |
| BUG-03 | Ж3 | music приоритет над movies/series в detect |
| BUG-04 | Ж2 | бот сам решает type, не показывает выбор |
| BUG-05 | Ж1, Ж3 | Wi-Fi сбой Radarr/Sonarr/Lidarr → wrong detect |
| BUG-06 | Ж1 | parse_query жадно отрывает год от title |
| BUG-07 | Ж1 | Prowlarr получает title БЕЗ года → плохой матчинг |
| BUG-08 | Ж1 | повторный lookup_movie берёт `[0]` без year-фильтра |
| BUG-09 | Ж2 | type_selection MUSIC сбрасывает session раньше успеха |
| BUG-10 | Ж3 | `replace("/movie", "")` глобально — реже, но edge |
| BUG-11 | Ж3 | series-pattern без \b — фолз-позитив |

Остальные (BUG-12..41) — high/medium/low, объясняют расширенный список UX-проблем поиска (стейт сессии, медленная сеть, edge-cases индексеров).
