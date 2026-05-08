# Logic & Architecture TG_arr v1.0 (раунд 3)

Дата: 2026-05-08.
Автор-аудитор: review бота на bot/, tests/.
Кодовая база: ~14580 LOC, Python 3.12, aiogram 3.27, pydantic 2.12.5, httpx, aiosqlite.

> Жалоба пользователя: «бот плохо ищет, не различает типы контента».
> В этом отчёте проблемы с приоритетом «связь с жалобой пользователя = да» прямо относятся к этой жалобе и должны фикситься в первую очередь.
>
> Раунд 2 уже зафиксировал god-files (LOGIC-01..05) как DEFERRED. В этом раунде мы их перепроверяем, но фокусируемся на behaviour-changing проблемах поиска.

---

## Поведенческие проблемы (исправляются СРАЗУ)

### LOGIC-01 (R3) — Detection: query идёт в lookup без очистки → Lidarr и Sonarr получают шум
- **Файл**: `bot/services/search_service.py:69-74`
- **Проблема**: `detect_content_type(query)` вызывается из `bot/handlers/search.py:162` уже с `parsed["title"]` (хорошо, BUG-09 закрыт). Но **внутри** `detect_content_type` исходный `query` отдаётся в `radarr.lookup_movie(query)`, `sonarr.lookup_series(query)`, `lidarr.lookup_artist(query)` — и при этом всё ещё содержит, например, `"Дюна 2021"` (год не вычистен). Radarr и Sonarr год терпимо нормализуют, но Lidarr через MusicBrainz по запросу `"Linkin Park 2014"` отдаёт мусор / пусто. Это снижает качество music-detection.
- **Решение**: внутри `detect_content_type` парсить query один раз (re-use `parse_query`) и в Radarr/Sonarr/Lidarr передавать уже cleaned title. Либо переименовать аргумент `detect_content_type(title: str)` и обязать caller передавать чистый title (это уже частично сделано, нужно убрать дублирование `re.search(year)` ниже в `_title_matches`).
- **Связь с жалобой пользователя**: **да** — прямо влияет на «не различает типы контента».
- **Статус**: [ ]

### LOGIC-02 (R3) — `_title_matches` substring-matching ломается на коротких/общих словах
- **Файл**: `bot/services/search_service.py:111-134`
- **Проблема**: Логика `query_clean in title_lower or title_lower in query_clean` крайне грубая:
  - запрос `"Лига"` совпадёт с фильмом `"Лига справедливости"`, артистом `"Лига Чемпионов"`, сериалом `"Лига" (2009)` → возможно неверная классификация на topN-3.
  - `"один"` подмешает совпадения вроде `"Один дома"`, `"Один в океане"`, `"Один: Тёмный мир"` — приоритет MUSIC > MOVIE > SERIES в коде (`if artists: ... if movies: ...`), и music побеждает, если хоть один артист в top-3 имеет в имени слово `"один"`.
- **Решение**: использовать нормализованную similarity (rapidfuzz / SequenceMatcher.ratio() ≥ 0.8) либо Jaccard на токенах. Минимум — требовать совпадения «начала» строки, не подстроки в обе стороны.
- **Связь с жалобой пользователя**: **да** — главная причина «бот не различает типы».
- **Статус**: [ ]

### LOGIC-03 (R3) — Detection: приоритет MUSIC>MOVIE>SERIES не учитывает length/relevance
- **Файл**: `bot/services/search_service.py:94-107`
- **Проблема**: При успешном Lidarr-match артист **всегда** перебивает Radarr/Sonarr, даже если в Radarr *идеальное* совпадение по `"title + год"`, а в Lidarr — лишь первое слово. Пример: запрос `"Аватар 2009"` → Radarr matched perfectly, но если Lidarr lookup `"Аватар 2009"` отдаст некий артист `"Аватар"` в top-3, ContentType.MUSIC выигрывает. Усугубляется LOGIC-02.
- **Решение**: scoring всех трёх кандидатов (artist/movie/series) с year-bonus и length-of-match, выбирать максимум. Или: если в query есть year → автоматически music dropped (год не присущ артистам).
- **Связь с жалобой пользователя**: **да** — частный случай LOGIC-02, но требует отдельного фикса.
- **Статус**: [ ]

### LOGIC-04 (R3) — `process_search` фильтрует по `detected_type` и теряет половину результатов
- **Файл**: `bot/services/search_service.py:163-164`
- **Проблема**: `[r for r in results if r.detected_type == content_type or r.detected_type == ContentType.UNKNOWN]`. `detected_type` устанавливается **внутри Prowlarr-парсера** только если категория попала в один из `MOVIE_CATEGORIES`/`TV_CATEGORIES`/`MUSIC_CATEGORIES` *или* (для серий) присутствует season-episode pattern в title. На многих русских торрент-индексерах (NoNaMe, Rutor, Kinozal) категории либо отсутствуют, либо нестандартные → `detected_type=UNKNOWN`, что **проходит** фильтр. Но индексер вроде RuTracker зачастую отдаёт фильм в категории 5070 (TV) для российских релизов → `detected_type=SERIES` → если пользователь ищет фильм, такой результат отсеивается, и он не виден. Жалоба «плохо ищет» прямо отсюда.
- **Решение**: либо не фильтровать вообще (полагаться на сортировку по score), либо вместо «type mismatch → drop» применять **штраф** в скоринге (`scoring.calculate_score` снижает на -30 если `detected_type != requested`).
- **Связь с жалобой пользователя**: **да** — прямая причина «плохо ищет».
- **Статус**: [ ]

### LOGIC-05 (R3) — `parse_query` снимает год до detect, но search_releases получает чистый title без года → Prowlarr теряет сигнал
- **Файл**: `bot/handlers/search.py:200-201`
- **Проблема**: `search_term = parsed.get("title")` — год вырезан. Многие индексеры используют `"Title 2024"` для match с раздачей `"Title.2024.2160p..."`. Без года (`"Title"`) Prowlarr сводит к шумному матчу: страница 1 завалена релизами `"Title 2"`, `"Title 1995"`, etc. Так создаётся проблема «плохо ищет».
- **Решение**: передавать в Prowlarr тот же query, что и юзер ввёл (`query`), а не `parsed.title`. Чистый title — только для lookup_movie / lookup_series (там Radarr и Sonarr сами умеют год). Это **разворачивает** часть фикса BUG-09; вместо этого правильнее «detect_content_type получает clean title; search_releases получает raw query».
- **Связь с жалобой пользователя**: **да** — прямая причина «плохо ищет».
- **Статус**: [ ]

### LOGIC-06 (R3) — `handle_release_selection` делает повторный `lookup_movie(session.query)` несмотря на cached `selected_content`
- **Файл**: `bot/handlers/search.py:430, 449`
- **Проблема**: При каждом нажатии на release делается новый `search_service.lookup_movie(session.query)` или `lookup_series`. Если пользователь пришёл из `trending.py` (movie уже в `_trending_movies_cache`) — этот кэш не передаётся в search-flow. Результат: лишний HTTP-запрос к Radarr/Sonarr на каждый клик, латентность 200-1500мс.
- **Решение**: в `_execute_grab` уже есть проверка `if not isinstance(movie, MovieInfo): movies = await search_service.lookup_movie(session.query)` (search.py:589-594). Аналогичную проверку нужно ввести в `handle_release_selection`: если `session.selected_content` уже установлен (например, прокинут из trending), не делать повторный lookup.
- **Связь с жалобой пользователя**: косвенно (медленно).
- **Статус**: [ ]

### LOGIC-07 (R3) — `handle_release_selection` берёт первый результат `movies[0]` — может быть «не тот» фильм
- **Файл**: `bot/handlers/search.py:432, 451`
- **Проблема**: `movie = movies[0]` без сравнения с `result.detected_year` или релизным title-pattern. Если `query = "Дюна"` и Radarr вернул `[Дюна (2021), Дюна (1984), Дети Дюны (2003)]`, то всегда берётся первый — независимо от того, какой релиз пользователь выбрал.
- **Решение**: matched-by-year эвристика: попытаться найти `m for m in movies if m.year == result.detected_year`, fallback на `movies[0]`.
- **Связь с жалобой пользователя**: **да** — пользователь видит «не тот» фильм после выбора release.
- **Статус**: [ ]

### LOGIC-08 (R3) — `process_search` рекурсивно вызывает себя через `handle_type_selection → process_search`, теряя status_msg
- **Файл**: `bot/handlers/search.py:307-313`
- **Проблема**: `handle_type_selection` после выбора фильм/сериал делает `await process_search(callback.message, session.query, content_type, ...)`. Внутри `process_search` создаётся **новый** `status_msg = await message.answer("🔍 Ищу релизы...")`. Старое сообщение с кнопками выбора типа остаётся в чате болтающимся — пользователь видит дубль и копию.
- **Решение**: extract `_search_and_show_results` который принимает уже-существующий status_msg для редактирования. `process_search` использует его при первом вызове, `handle_type_selection` — при втором (передаёт `callback.message`).
- **Связь с жалобой пользователя**: косвенно (UX выглядит «глючно»).
- **Статус**: [ ]

### LOGIC-09 (R3) — Gone session race: `handle_pagination` редко но возможно после grab — пагинация на пустом сообщении
- **Файл**: `bot/handlers/search.py:316-382, 678-680`
- **Проблема**: После `_execute_grab` в финале `await db.delete_session(user_id)`. Если пользователь успел кликнуть «◀️» (pagination) до того как grab завершился, callback видит ещё-живую сессию, идёт рендер. Но если grab отвалился раньше pagination-handler начался — `session is None` → корректный alert. Реальная гонка: grab `succeeded`, pagination клик ушёл *в момент* delete_session → 50/50.
- **Решение**: использовать optimistic locking (session.version), либо передавать ack-token в callback_data, либо просто не критично т.к. UX мягкий. Минор.
- **Связь с жалобой пользователя**: нет.
- **Статус**: [ ]

### LOGIC-10 (R3) — `handle_grab_best` выбирает `results[0]`, но не проверяет `calculated_score`
- **Файл**: `bot/handlers/search.py:493`
- **Проблема**: «Скачать лучший» = брать first sorted result, *безусловно*. Если все результаты ниже `auto_grab_score_threshold` (=80) и кнопка вообще показалась только из-за отсутствия проверки на пустоту — best может быть «плохим» (40 баллов). Однако `show_grab_best` в `process_search:225-229` уже проверяет пороги. Остаётся защита от race condition: если session загружена со старого state.
- **Решение**: повторная проверка `result.calculated_score >= settings.auto_grab_score_threshold` в `handle_grab_best`.
- **Связь с жалобой пользователя**: нет.
- **Статус**: [ ]

### LOGIC-11 (R3) — `_trending_movies_cache` и `_trending_series_cache` глобальные, mutable, без TTL
- **Файл**: `bot/handlers/trending.py:26-30`
- **Проблема**: cache по tmdb_id хранится pro process, очищается только на overflow (>200). Между перезапусками контейнера stale данные не выкидываются (in-memory), но при долгом аптайме пользовательский cache «протухает» → клик по Trending Movie через сутки даёт stale movie info. Не critical, но запрос «обновить» нет.
- **Решение**: добавить `cached_at: datetime` per entry, expiry 1 час; либо переключить на `cachetools.TTLCache`.
- **Связь с жалобой пользователя**: нет.
- **Статус**: [ ]

### LOGIC-12 (R3) — `handle_trending_artist_click` делает `process_music_search(callback.message, name, ...)` — `callback.message.answer(...)` вместо `edit_text`
- **Файл**: `bot/handlers/music.py:316-339`
- **Проблема**: `process_music_search` всегда делает `await message.answer("🔍 Ищу артистов в Lidarr...")` — это **новое** сообщение в чате, т.к. `message` это `callback.message` (бот-сообщение). У пользователя на экране остаётся старое сообщение с trending-list **+** новое сообщение с поиском. Дублирование UI.
- **Решение**: ввести опциональный параметр `status_msg: Optional[Message]` в `process_music_search`, и при наличии редактировать его.
- **Связь с жалобой пользователя**: косвенно (UX).
- **Статус**: [ ]

### LOGIC-13 (R3) — `handle_add_series_from_trending` делает sonarr lookup_series **внутри** add-фазы, повторно вызывая Sonarr по title
- **Файл**: `bot/handlers/trending.py:432-450`
- **Проблема**: `if not series.tvdb_id: await sonarr.lookup_series(series.title)` — это уже внутри добавления, после нажатия пользователем «Добавить». На handle_series_from_trending пользователь видел детали и постер, но tvdb_id всё равно был 0. Логично его resolve **на стадии показа details** (handle_series_from_trending:228-279), чтобы add-flow был быстрее.
- **Решение**: в `handle_series_from_trending` сразу после `cache.get(...)` если tvdb_id=0 → resolve через Sonarr lookup; обновить cache. Тогда add-flow (`handle_add_series_from_trending`) уже имеет valid tvdb_id.
- **Связь с жалобой пользователя**: косвенно (медленнее).
- **Статус**: [ ]

### LOGIC-14 (R3) — `handle_pagination` для `CallbackData.PAGE` ловит **и** music pagination — пересечение с `Keyboards.artist_list`
- **Файл**: `bot/ui/keyboards.py:344-355`, `bot/handlers/search.py:316-382`
- **Проблема**: `Keyboards.artist_list` использует тот же `CallbackData.PAGE` префикс (`page:N`), что и search results. Когда пользователь на artist_list нажимает страницу, *первым* срабатывает `search.py:handle_pagination` (router include order: search_router до music_router). Внутри handle_pagination проверяется `session.results` — у music сессии оно пустое → возвращается «Сессия истекла», хотя artist list жив в `_artist_candidates[user_id]`.
- **Решение**: либо завести отдельный `CallbackData.ARTIST_PAGE = "art_page:"` и поправить keyboards.artist_list, либо в music.py зарегистрировать handler **до** search и проверять что session.content_type == MUSIC.
- **Связь с жалобой пользователя**: **да** для music UX (нельзя листать артистов > 5 штук).
- **Статус**: [ ]

### LOGIC-15 (R3) — `process_music_search` создаёт `SearchSession(content_type=MUSIC)` но **затирает** результаты search-флоу для того же пользователя
- **Файл**: `bot/handlers/music.py:137-142`
- **Проблема**: Если у пользователя уже есть активная search-session («Дюна» с 25 результатами), и он нажимает MENU_MUSIC → вводит «Metallica», то `db.save_session` затирает его movie-сессию. После «❌ Отмена» из music — назад к фильмам пользователь не может.
- **Решение**: per-content-type сессии: ключ `(user_id, content_type)`. Либо мягче — сохранять предыдущую `selected_content` как `prev_session` в blob.
- **Связь с жалобой пользователя**: косвенно.
- **Статус**: [ ]

### LOGIC-16 (R3) — `_resolve_folder` бросает `ValueError("Нет доступных папок ...")` который не ловится в `_execute_grab`
- **Файл**: `bot/handlers/search.py:557-564`, `bot/handlers/search.py:567-680`
- **Проблема**: `_resolve_folder` raise `ValueError`, но `_execute_grab` ловит только generic `except Exception` (search.py:677) → fallback message «Операция временно недоступна». Пользователь не видит конкретную причину «нет папок в Radarr». Информативность нулевая.
- **Решение**: `except ValueError as ve: await message.edit_text(Formatters.format_error(str(ve)))` отдельно.
- **Связь с жалобой пользователя**: косвенно (UX).
- **Статус**: [ ]

### LOGIC-17 (R3) — `parse_query` не извлекает «Артист — Альбом» паттерны для music
- **Файл**: `bot/services/search_service.py:202-258`
- **Проблема**: Парсер заточен на movie/series. Music-запросы вида `"Metallica - Master of Puppets"` или `"Pink Floyd: The Wall"` не распадаются на artist + album. `process_music_search` в music.py отдаёт raw query прямо в Lidarr lookup_artist, который ищет **только artist**, теряя album-сигнал.
- **Решение**: добавить music-aware split на ` - `, ` — `, ` : ` если query похож на «слова - слова». При detection MUSIC использовать левую часть для lookup_artist; правую — потом для lookup_album (когда выбран артист).
- **Связь с жалобой пользователя**: **да** для music.
- **Статус**: [ ]

### LOGIC-18 (R3) — Quality-парсер ложно срабатывает на DVO/MVO/AVO внутри длинных слов
- **Файл**: `bot/clients/prowlarr.py:280-285`
- **Проблема**: `re.search(r"[\.\s\-_]mvo[\.\s\-_$]", title_lower)` — `[\.\s\-_$]` означает класс из 5 символов, **не end-of-string** (нужен `(?:[\.\s\-_]|$)`). Из-за этого паттерны типа `"...mvo$"` (релиз заканчивается на mvo) не матчатся, а паттерны типа `"...mvo "` (с пробелом) — да. Минор.
- **Решение**: переписать на `(?:[\.\s\-_]|$)` либо использовать `\b`.
- **Связь с жалобой пользователя**: нет, но скоринг русских релизов слегка неточен.
- **Статус**: [ ]

### LOGIC-19 (R3) — `_extract_year` берёт самый первый match, но в `"S01E03 2024 1080p"` — год правильный; в `"Дом-2 (2003)"` — `2003` (тоже правильный); в `"Movie 1080p 2024"` — pattern `\.(\d{4})\.` или `\s(\d{4})\s` → находит `2024`. Однако в `"Movie.4K.2160p.x265"` совпадение с `4K` уже сделано в _parse_quality, но `_extract_year` может зацепить `2160` если `\s(\d{4})\s` обрамлён точками? Нет, `1900 ≤ year ≤ 2100` отсеет. ОК, не баг.
- **Файл**: `bot/clients/prowlarr.py:308-323`
- **Проблема**: ложноположительных нет. Минор.
- **Статус**: [ ] (не баг — ноут для документации)

### LOGIC-20 (R3) — Каждый callback создаёт новый `SearchService`/`AddService` экземпляр (раунд 2 LOGIC-15 не закрыт)
- **Файл**: `bot/handlers/search.py:46-58`, `bot/handlers/music.py:51-64`, `bot/handlers/trending.py:326, 413`, `bot/handlers/settings.py:29-37`
- **Проблема**: `get_services()` строит новый SearchService и AddService на каждый callback (singleton только на уровне clients). Дёшево, но `ScoringService` теперь singleton (хорошо). В trending.py — даже AddService создаётся **inline** через `AddService(prowlarr, radarr, sonarr, qbittorrent)` без `lidarr=`, без shared scoring.
- **Решение**: вынести построение services в `bot/services/factory.py` со сквозным lru_cache (на основе clients). Это уже отмечено в раунде 2 LOGIC-15, deferred → продолжаем deferred. Однако простой fix: trending.py:326 подменить на `add_service = AddService(prowlarr, radarr, sonarr, qbittorrent=qbittorrent, lidarr=await get_lidarr())`. Именно для trending **сейчас** Lidarr не передаётся, потому что trending.py добавляет только movies/series — но это не явно. Под линтер.
- **Связь с жалобой пользователя**: нет.
- **Статус**: [ ]

### LOGIC-21 (R3) — `MENU_BUTTONS` set дублируется и расходится между `start.py` и `search.py`
- **Файл**: `bot/handlers/search.py:39-43` vs `bot/handlers/start.py:15-21`
- **Проблема**: список Russian menu кнопок прописан **дважды**. При добавлении новой кнопки нужно обновить оба места (в start.py — чтобы /menu отрисовал; в search.py — чтобы text-handler не глотал её как поиск). Easy footgun.
- **Решение**: вынести в `bot/ui/menu.py` константу `MENU_BUTTON_TEXTS: frozenset[str]` с единым источником.
- **Связь с жалобой пользователя**: нет.
- **Статус**: [ ]

### LOGIC-22 (R3) — `Formatters.format_search_results_page` всегда показывает 🎬 для Movie и 📺 — fallback. MUSIC получает 📺 (неправильно)
- **Файл**: `bot/ui/formatters.py:89`
- **Проблема**: `type_emoji = "🎬" if content_type == ContentType.MOVIE else "📺"` — для MUSIC покажется 📺 (TV). Но music flow идёт через `process_music_search`, а не через format_search_results_page → на практике не вызывается. Мёртвый bug, но опасный (если завтра music пойдёт через generic flow).
- **Решение**: dict-mapping ContentType → emoji. Включить ContentType.UNKNOWN→🔍.
- **Связь с жалобой пользователя**: нет (latent).
- **Статус**: [ ]

### LOGIC-23 (R3) — `bot/handlers/search.py:198` (`status_msg = await message.answer(...)`) overwrites name from line 156
- **Файл**: `bot/handlers/search.py:156, 195`
- **Проблема**: Переменная `status_msg` вначале объявлена в branch `if content_type == UNKNOWN` (строка 156), потом — снова после resolved branch (строка 195). Если ContentType сразу известен (например /movie), первый branch не входит, status_msg создаётся с строки 195 — OK. Но если ContentType=UNKNOWN и `detect_content_type` возвращает MOVIE/SERIES/UNKNOWN (не MUSIC) — `await status_msg.delete()` (line 192) удаляет первое сообщение, потом создаётся новое (line 195). Лишнее API-обращение к Telegram.
- **Решение**: либо делать `status_msg.edit_text("🔍 Ищу релизы...")` вместо delete+answer, либо хотя бы свести в один helper.
- **Связь с жалобой пользователя**: нет, но UX ухудшен мерцанием.
- **Статус**: [ ]

### LOGIC-24 (R3) — `handle_back` после grab-flow / music-flow не работает: `session.results` пусто → «Сессия истекла»
- **Файл**: `bot/handlers/search.py:683-700`, `bot/handlers/music.py` (back в artist_details)
- **Проблема**: Music keyboard `artist_details` имеет кнопку BACK (keyboards.py:376), которая идёт через `CallbackData.BACK` → handler в search.py:683 ищет `session.results` (пусто для music) → пользователь видит «Сессия истекла», хотя на самом деле сессия активна. Music-back ломан.
- **Решение**: в `handle_back`: если `session.content_type == MUSIC` — делегировать в music.py (новый `handle_music_back`, рендерит обратно `artist_list` из `_artist_candidates`).
- **Связь с жалобой пользователя**: **да** для music.
- **Статус**: [ ]

### LOGIC-25 (R3) — `lookup_movie_by_tmdb` отсутствует у Sonarr — нет аналога для series
- **Файл**: `bot/clients/sonarr.py` (нет lookup_series_by_tmdb), `bot/handlers/trending.py:432-450`
- **Проблема**: Trending series приносит `tmdb_id`, но Sonarr `series/lookup` ищет по term-string. В `handle_add_series_from_trending` приходится делать `sonarr.lookup_series(series.title)`, потом фильтровать по `lr.tmdb_id == series.tmdb_id` (trending.py:438). Это medium-стабильно — вернуть может несколько серий с одинаковым title (different countries).
- **Решение**: добавить `SonarrClient.lookup_series_by_tmdb_id(tmdb_id) -> Optional[SeriesInfo]` — Sonarr поддерживает `tmdb:NNN` term (сравнить с `lookup_series_by_tvdb` line 52-59).
- **Связь с жалобой пользователя**: нет, но trending series→add иногда «не тот» сериал.
- **Статус**: [ ]

### LOGIC-26 (R3) — Inconsistency: `MovieInfo.year: int` (required, default 0), `SeriesInfo.year: Optional[int]`, `ArtistInfo` нет year
- **Файл**: `bot/models.py:107, 133`
- **Проблема**: При parsing TMDb с пустым `release_date` формируется `MovieInfo(year=0)`, который falsy в `f" ({movie.year})"` → не отрисовывается. SeriesInfo.year опционален → требует `if series.year`. ArtistInfo не имеет year (правильно). Несимметрия повышает риск ошибки. Trending tmdb may return year=0 → grab_movie_release использует `movie.year` в payload Radarr (year=0 → Radarr не сохранит фильм правильно).
- **Решение**: `MovieInfo.year: Optional[int]` (consistent with SeriesInfo). Везде где сейчас `movie.year` напрямую — делать `movie.year or "?"`.
- **Связь с жалобой пользователя**: нет, но baseline-bug возможен.
- **Статус**: [ ]

### LOGIC-27 (R3) — `process_search` exception handler логирует, но не уведомляет user о content_type detection failure
- **Файл**: `bot/handlers/search.py:267-269`
- **Проблема**: `except Exception as e: log.error(...); await message.answer(format_error("Поиск временно недоступен"))`. Пользователь видит generic message — даже если, например, упал только Radarr, а Sonarr и Lidarr ответили. Detection running через `asyncio.gather(return_exceptions=True)` — никогда не raise, но **search_releases** raise может. Уровень детализации UX = 0.
- **Решение**: при отдельных «Prowlarr недоступен» / «Radarr недоступен» давать user-friendly текст. (Можно унаследовать exception class в base.py).
- **Связь с жалобой пользователя**: нет.
- **Статус**: [ ]

### LOGIC-28 (R3) — `SearchService.detect_content_type` не возвращает confidence
- **Файл**: `bot/services/search_service.py:36-109`
- **Проблема**: Возвращает только `ContentType` enum. UI показывает «🤔 Дюна — это фильм или сериал?» **только** если detection вернул UNKNOWN. Если detection вернул MOVIE с слабым match (1 буква совпала) — UI идёт сразу в search и пользователь не знает, что мог неправильно угадаться тип.
- **Решение**: `detect_content_type` → `tuple[ContentType, float]` (confidence 0..1). При confidence < 0.6 → UI показывает type_selection с подсказкой "Похоже на фильм, но не уверен".
- **Связь с жалобой пользователя**: **да** — позволит пользователю поправить.
- **Статус**: [ ]

### LOGIC-29 (R3) — `Prowlarr._normalize_result` — `seeders=None` если qBT API подкинул не-int. Но score-фильтр `min_seeders` в `filter_by_quality` сравнивает `seeders < min_seeders` — `None < int` → TypeError
- **Файл**: `bot/services/scoring.py:312-313`, `bot/clients/prowlarr.py:107-112`
- **Проблема**: `if result.seeders is not None and result.seeders < min_seeders: continue` — guard есть. ОК.
- **Статус**: [ ] (не баг, документация)

### LOGIC-30 (R3) — Функция `grab_release` (search.py:545-554) — пустая обёртка над `_execute_grab`, добавляющая ничего
- **Файл**: `bot/handlers/search.py:545-554`
- **Проблема**: Дубль. `grab_release` принимает те же параметры и просто вызывает `_execute_grab`. Inline → сократить файл на 10 строк.
- **Решение**: убрать `grab_release`, callers напрямую звать `_execute_grab`.
- **Связь с жалобой пользователя**: нет.
- **Статус**: [ ]

### LOGIC-31 (R3) — `handle_release_selection` ловит `Exception` после уже выполненного `edit_text` и не использует `release_details` keyboard
- **Файл**: `bot/handlers/search.py:467-473`
- **Проблема**: При ошибке в lookup_movie/lookup_series — keyboard всё ещё передаётся (`reply_markup=Keyboards.release_details(...)`), но user видит «Ошибка загрузки информации» **с** кнопками «Скачать»/«qBit». Если он нажмёт «Скачать» — `_execute_grab` пойдёт в lookup snd-time (search.py:589), что может снова отвалиться. UX противоречивый: ошибка показана, но грабить можно.
- **Решение**: при exception показать только Cancel/Back keyboard.
- **Связь с жалобой пользователя**: нет.
- **Статус**: [ ]

### LOGIC-32 (R3) — `auth_grab_score_threshold` хардкоден на default=80, нет UI для его настройки per-user
- **Файл**: `bot/config.py:75-77`, `bot/handlers/settings.py`
- **Проблема**: Параметр глобальный (env), не per-user. UserPreferences имеет `auto_grab_enabled: bool` — все/никто. Threshold не настраиваемый из бота.
- **Решение**: добавить `auto_grab_score_threshold: int = 80` в UserPreferences и UI-toggle 60/80/90/100.
- **Связь с жалобой пользователя**: нет.
- **Статус**: [ ]

### LOGIC-33 (R3) — `Formatters.format_release_details` показывает «Оценка: 100/100», но scoring.calculate_score возвращает -100..150
- **Файл**: `bot/ui/formatters.py:142`
- **Проблема**: hardcoded `/100`. Score может быть 145 или -50. UX показывает «145/100».
- **Решение**: убрать `/100` или поменять на «Оценка: <b>145</b> (max 150)».
- **Связь с жалобой пользователя**: нет.
- **Статус**: [ ]

### LOGIC-34 (R3) — Magic «25» в `process_music_search:134`: `_artist_candidates[user_id] = artists[:25]`
- **Файл**: `bot/handlers/music.py:134`
- **Проблема**: hard-cap 25 артистов в кэше per user, но `artist_list` keyboard рендерит per_page=5 — на 25 получится 5 страниц. Magic, не задокументировано.
- **Решение**: `MUSIC_CANDIDATES_LIMIT = 25` в constants.py.
- **Связь с жалобой пользователя**: нет.
- **Статус**: [ ]

### LOGIC-35 (R3) — `bot/handlers/calendar.py: _user_period` per-user dict не TTL — память накапливается на длинном аптайме
- **Файл**: `bot/handlers/calendar.py:23-24`
- **Проблема**: cap 100 (clear at overflow), но без TTL. Не критично для whitelist-бота с ~3-5 юзеров.
- **Статус**: [ ] (не приоритет)

### LOGIC-36 (R3) — `Prowlarr.search` `limit=100` хардкодед, не использует `bot.constants.PROWLARR_SEARCH_LIMIT`
- **Файл**: `bot/clients/prowlarr.py:32`
- **Проблема**: Constants файл уже есть (раунд 2 закрыл LOGIC-08), но `prowlarr.py:32` не импортирует. Аналогично `bot/handlers/downloads.py:19` использует свой `TORRENTS_PER_PAGE = 5` (строка 19) вместо `from bot.constants import TORRENTS_PER_PAGE`.
- **Решение**: заменить локальные константы на импорт из `bot.constants`.
- **Связь с жалобой пользователя**: нет.
- **Статус**: [ ]

### LOGIC-37 (R3) — `MOVIE_CATEGORIES` / `TV_CATEGORIES` / `MUSIC_CATEGORIES` хардкодед в prowlarr.py — не настраивается пользователем
- **Файл**: `bot/clients/prowlarr.py:15-18`
- **Проблема**: Категории Newznab фиксированы. Если индексер использует кастомные категории (например Anime в 5070, не 5050) — не попадёт. Аудио-categories overlap с usenet (3xxx общие).
- **Решение**: вынести в Settings. Минор.
- **Связь с жалобой пользователя**: косвенно (anime/foreign контент пропадает).
- **Статус**: [ ]

### LOGIC-38 (R3) — `release_rejected = False` инициализируется, но `release.download_url` пустой → flow проваливается в `if release_rejected: return False`
- **Файл**: `bot/services/add_service.py:338-339, 405-409`
- **Проблема**: Если `release.download_url` пустой (например только magnet-only Prowlarr с `magnet_url` set, но `download_url=None`):
  - block `if release.download_url:` пропускается
  - `release_rejected` остаётся False
  - блок `if release.indexer_id > 0 and not release_rejected: try grab_release` — попытка
  - если grab_release падает с APIError, `release_rejected` остаётся False, идём в qBittorrent fallback. Но `(release_rejected or force_download) and self.qbittorrent` — fallback **не сработает** для magnet-only без force.
- **Решение**: учесть `magnet_url` — если есть и нет `download_url`, всё равно валидно. Текущая логика `(release_rejected or force_download)` пропускает magnet-only при normal grab. Сейчас флоу почти правильный, но edge-case есть.
- **Связь с жалобой пользователя**: косвенно.
- **Статус**: [ ]

### LOGIC-39 (R3) — Отсутствие FSM aiogram → state хранится в SQLite (db.sessions)
- **Файл**: `bot/handlers/search.py`, `bot/db.py:321-380`
- **Проблема**: aiogram 3 имеет встроенный FSMContext с storage backends (Memory/Redis/SQLite). Бот не использует — собственная реализация SearchSession в SQLite. Двойная работа: и aiogram middleware и custom session manager.
- **Решение** (deferred): мигрировать в FSMContext с RedisStorage / FilesystemStorage, отказаться от db.sessions table. Большой рефакторинг.
- **Связь с жалобой пользователя**: нет.
- **Статус**: [ ] Deferred

### LOGIC-40 (R3) — `parse_query` слаб: не обрабатывает «Movie Title (2024) [4K]» формат, MULTI/DUAL/SUB
- **Файл**: `bot/services/search_service.py:202-258`
- **Проблема**: Для типичных запросов работает, но не извлекает: language tags (RUS, ENG, MULTI, DUAL), edition (Director's Cut, Extended), source (BluRay).
- **Решение**: использовать [guessit](https://pypi.org/project/guessit/) или [PTN](https://pypi.org/project/parse-torrent-name/) — сторонние парсеры с миллионами тестов на торрент-неймах. Заменит самописный parse_query+_parse_quality (~150 строк регексов).
- **Связь с жалобой пользователя**: косвенно (ScoringService теряет сигналы).
- **Статус**: [ ] Deferred (требует миграции; risk: weights на других scale).

---

## Архитектурный рефакторинг (DEFERRED — отдельный PR)

### LOGIC-D1 — God-file `bot/ui/formatters.py` (1038 строк) — повтор раунд 2 LOGIC-01
- **Описание**: 30+ статических методов для torrent / movie / series / artist / album / calendar / emby / qbittorrent / trending. Файл вырос с 890 → 1038 строк.
- **Размер**: 1 файл, 1038 строк → 6-7 файлов по 150-200.
- **План**: `bot/ui/formatters/{__init__,common,search,torrent,media,calendar,music,emby,trending}.py`
- **Статус**: [ ] Deferred

### LOGIC-D2 — God-file `bot/ui/keyboards.py` (860 строк) — повтор раунд 2 LOGIC-02
- **Описание**: классы CallbackData (огромный) + Keyboards (статика). Слишком много.
- **Размер**: 1 файл, 860 строк → 5-6 файлов.
- **План**: `bot/ui/keyboards/{__init__,common,search,torrent,emby,trending,calendar,settings}.py`
- **Статус**: [ ] Deferred

### LOGIC-D3 — `bot/handlers/search.py` (784) + music.py (339) + trending.py (480) дублируют grab/add логику — повтор раунд 2 LOGIC-03
- **Описание**: см. подробно LOGIC-03 раунд 2. trending.py дополнительно строит AddService inline без Lidarr.
- **Размер**: 3 файла, ~1600 строк → ввести `GrabOrchestrator` / `AddOrchestrator` в services/.
- **Статус**: [ ] Deferred

### LOGIC-D4 — `RadarrClient`/`SonarrClient`/`LidarrClient` дублируют `_parse_*`, image extract, ratings parse — повтор раунд 2 LOGIC-04
- **Описание**: `_parse_movie`/`_parse_series`/`_parse_artist`. Тот же pattern: poster/fanart from images list, ratings dict normalize, root_folder_path or path.
- **Размер**: 3 файла, ~80 строк дублей → ввести `bot/clients/_arr_common.py: ArrBaseClient(_parse_images, _parse_ratings, _parse_root_folder)`.
- **Статус**: [ ] Deferred

### LOGIC-D5 — `grab_movie_release`/`grab_series_release`/`grab_music_release` 95% identical — повтор раунд 2 LOGIC-05
- **Описание**: см. add_service.py:278-568 + 624-748. Различия: target client, qBit category (`radarr`/`tv-sonarr`/`music`), action_type, content_type, monitor_type.
- **Размер**: 3 метода, ~270 строк → unify в `_grab(release, target_client, category, ensure_fn, content_type)`.
- **Статус**: [ ] Deferred

### LOGIC-D6 — `parse_query` возвращает `dict`, не dataclass — повтор раунд 2 LOGIC-06
- **Описание**: weakly-typed dict; mypy/IDE не помогает.
- **Размер**: ~50 строк рефактора + ParsedQuery dataclass.
- **Статус**: [ ] Deferred

### LOGIC-D7 — `SearchService.detect_content_type` — переход на ML-classifier или внешний LLM
- **Описание**: текущая heuristic + parallel lookup ненадёжна (см. LOGIC-02, 03 раунд 3). Альтернативы:
  - **Локальный кэш** на `(query.lower(), result)` с TTL 1 час — снимет нагрузку на повторных запросах.
  - **fuzzy matching** через rapidfuzz — заменит `_title_matches`.
  - **LLM** (например, локальный llama-3-8b или Anthropic API) — overkill для 3 классов.
- **Размер**: ~150 строк, новый `bot/services/content_classifier.py`.
- **Статус**: [ ] Deferred

### LOGIC-D8 — `ScoringService` — слишком много весов, поддерживается ли?
- **Описание**: 30+ полей `ScoringWeights`. Некоторые правила overlap (codec_x265 + hdr_dolby_vision + audio_atmos накладываются). Пользователь не понимает, почему «Movie X 2160p HDR DV Atmos REMUX» получает 145 баллов, а «Movie X 1080p Web-DL» — 80.
- **Размер**: ~50 строк рефактора в DSL/YAML rules engine.
- **Альтернативы**: использовать **Custom Format**-логику Radarr/Sonarr (уже есть на стороне arr — можно делегировать им).
- **Статус**: [ ] Deferred

### LOGIC-D9 — Inconsistent return types: `RadarrClient.lookup_movie → list[MovieInfo]`, `Prowlarr._normalize_result → SearchResult`. Calendar везде возвращает `list[dict]` — почему?
- **Файл**: `bot/clients/sonarr.py:235-273`, `bot/clients/radarr.py:187-232`, `bot/clients/lidarr.py:166-198`
- **Описание**: calendar-методы возвращают `list[dict[str, Any]]` — в формате, удобном для `Formatters.format_calendar`. Но это не моделируется (нет `CalendarEpisode` / `CalendarMovie` / `CalendarAlbum` dataclass). Линтер/mypy не помогают.
- **Размер**: добавить 3 dataclass в `bot/models.py`.
- **Статус**: [ ] Deferred

### LOGIC-D10 — `bot/handlers/__init__.py` order имеет смысл (search до music) — fragile, нужно тестом проверять
- **Файл**: `bot/handlers/__init__.py:24-33`
- **Описание**: BUG-27 был критическим багом из-за order routers. Сейчас комментарий есть, но никакого test-protection нет. Если будущий разработчик переставит include_router → silent breakage.
- **Размер**: добавить unit-тест который проверяет CONFIRM_GRAB → search_router handler. Или схема: явно `include_router(search_router)` с приоритетом-флагом.
- **Статус**: [ ] Deferred

---

## Итого

### Поведенческие (раунд 3, новые)
- **HIGH (связано с жалобой пользователя)**: LOGIC-01, LOGIC-02, LOGIC-03, LOGIC-04, LOGIC-05, LOGIC-07, LOGIC-14, LOGIC-17, LOGIC-24, LOGIC-28
- **MED**: LOGIC-06, LOGIC-08, LOGIC-13, LOGIC-15, LOGIC-16, LOGIC-26, LOGIC-31
- **LOW**: LOGIC-09, LOGIC-10, LOGIC-11, LOGIC-12, LOGIC-18, LOGIC-20, LOGIC-21, LOGIC-22, LOGIC-23, LOGIC-25, LOGIC-27, LOGIC-30, LOGIC-32, LOGIC-33, LOGIC-34, LOGIC-35, LOGIC-36, LOGIC-37, LOGIC-38

### Deferred (раунд 3, новые + перенесённые из раунда 2)
- LOGIC-D1 (R2 LOGIC-01) god-file formatters.py
- LOGIC-D2 (R2 LOGIC-02) god-file keyboards.py
- LOGIC-D3 (R2 LOGIC-03) handlers grab dup
- LOGIC-D4 (R2 LOGIC-04) ArrBaseClient
- LOGIC-D5 (R2 LOGIC-05) unified grab
- LOGIC-D6 (R2 LOGIC-06) ParsedQuery dataclass
- LOGIC-D7 (новый) ML/heuristic classifier
- LOGIC-D8 (новый) ScoringService DSL
- LOGIC-D9 (новый) Calendar dict→dataclass
- LOGIC-D10 (новый) router-order test
- LOGIC-39 (новый) aiogram FSM migration
- LOGIC-40 (новый) parse_query → guessit/PTN

### Сводная статистика раунд 3
- **HIGH (связано с жалобой)**: 10
- **MED**: 7
- **LOW**: 19
- **Deferred**: 12

### Главные выводы (для жалобы «плохо ищет, не различает»)

1. **LOGIC-04** — фильтр результатов Prowlarr по `detected_type` отсеивает корректные релизы, у которых индексер не выставил category (русские трекеры).
2. **LOGIC-05** — search_releases получает clean title без года → Prowlarr теряет ключевой сигнал.
3. **LOGIC-02** — `_title_matches` substring-based: запрос «один» совпадает с десятком фильмов и серий → false positives, неправильный contentType.
4. **LOGIC-03** — приоритет MUSIC > MOVIE > SERIES игнорирует наличие года в query: «Аватар 2009» может стать MUSIC если в Lidarr есть артист «Аватар».
5. **LOGIC-01** — Lidarr получает год в lookup_artist (мусор для MusicBrainz).
6. **LOGIC-17** — нет «artist - album» парсинга для music-флоу.
7. **LOGIC-28** — нет confidence; UX не даёт пользователю поправить detection.

Все 7 — низко-средний LOC fix (по 5-30 строк). Реализация 1-2 дня. Это оптимально решит главную жалобу.
