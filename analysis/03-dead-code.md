# Анализ — Мёртвый код · TG_arr (раунд 4, 2026-06-30)

Подтверждено находок: **13** (critical=0, high=0, medium=1, low=12). Все прошли состязательную верификацию (CONFIRMED/PLAUSIBLE).

## Средние

### DEAD-01: Entire bot/constants.py module is orphaned (never imported)
- **Файл**: `bot/constants.py:1`
- **Проблема**: No file in bot/ or tests/ imports bot.constants (grep for 'from.*constants import', 'import constants', and 'import *' returns zero hits). All 10 constants are unreachable. Worse, several are silently duplicated as magic numbers or local re-definitions: QBT_ETA_INFINITY=8640000 is dead while models.py:385 hardcodes the literal 8640000; MAX_QUERY_LENGTH=200 is redefined locally in handlers/search.py:138 and handlers/music.py:99; TORRENTS_PER_PAGE=5 is redefined in handlers/downloads.py:19. A future edit to constants.py (e.g. raising the page size) would have no effect, creating a correctness-drift trap.
- **Риск**: Removing the file is safe (zero importers). If kept, wiring it in changes behavior only where literals currently diverge.
- **Решение**: Either delete bot/constants.py entirely, or make it the single source of truth: import MAX_MESSAGE_LENGTH/SAFE_MESSAGE_LENGTH/TORRENTS_PER_PAGE/MAX_QUERY_LENGTH/QBT_ETA_INFINITY from it and remove the local redefinitions in downloads.py:19, search.py:138, music.py:99 and the literal 8640000 in models.py:385.
- **Верификация**: CONFIRMED — Independently verified against current code. bot/constants.py (22 lines, 10 constants) is genuinely orphaned: grep for `from bot.constants`, `from .constants`, `from ..constants`, `import constants`, and `constants import` across the whole repo returns ZERO hits in bot/ or tests/ — the symbols appear only in their own definition file and in analysis/*.md docs. Of the 10 constants, 8 (MAX_MESSAGE_LENGTH, SAFE_MESSAGE_LENGTH, SEARCH_RESULTS_MAX, PROWLARR_SEARCH_LIMIT, TRENDING_LIMIT, QBT_ETA_INFINITY, SESSION_TTL_HOURS, SEARCH_HISTORY_DAYS) appear nowhere outside constants.py. All four cited dup
- **Статус**: [x] Исправлено (раунд 4, мультиагент)

## Низкие

### DEAD-02: ProwlarrClient.grab_release is never called
- **Файл**: `bot/clients/prowlarr.py:447`
- **Проблема**: ProwlarrClient.grab_release() (posts to /api/v1/search) has zero callers. Grabbing in production goes exclusively through radarr/sonarr/lidarr .grab_release (add_service.py:373/525/723). grep 'grab_release' against bot/services, bot/handlers, tests shows no Prowlarr caller and no internal self-call inside prowlarr.py.
- **Решение**: Delete ProwlarrClient.grab_release (lines 447-end of method).
- **Верификация**: CONFIRMED — I opened bot/clients/prowlarr.py:447-457 and confirmed ProwlarrClient.grab_release(guid, indexer_id) exists, posting to /api/v1/search via _post_no_retry. I then grepped the entire codebase for callers. Findings: (1) Every production .grab_release( call site routes through a *arr client, not Prowlarr — add_service.py:373 self.radarr.grab_release, :525 self.sonarr.grab_release, :723 self.lidarr.grab_release. (2) There is NO occurrence of prowlarr.grab_release, self.prowlarr.grab_release, or any Prowlarr-instance call to grab_release anywhere in bot/. (3) The only other grab_release symbol is th
- **Статус**: [x] Исправлено (раунд 4, мультиагент)

### DEAD-03: Lidarr album-lookup chain (lookup_album / format_album_info) is dead in production
- **Файл**: `bot/clients/lidarr.py:42`
- **Проблема**: LidarrClient.lookup_album (lidarr.py:42) has zero callers anywhere (not in bot/, not in tests). Its only consumer would be Formatters.format_album_info (formatters.py:275), which itself has zero references anywhere (no internal use, no handler use). So an entire album-search/display feature is wired up in the model layer (AlbumInfo), client layer (lookup_album, _parse_album), and UI layer (format_album_info) but never reachable: music handlers only do artist lookup/add (music.py).
- **Решение**: Remove LidarrClient.lookup_album (lidarr.py:42-61) and Formatters.format_album_info (formatters.py:275ff). _parse_album is then only used by a unit test (test_lidarr.py:69); decide whether to keep _parse_album+AlbumInfo for the calendar path or drop them too.
- **Верификация**: CONFIRMED — Verified in current code. Grep across the whole repo shows `lookup_album` (bot/clients/lidarr.py:42) is defined but has zero callers anywhere in bot/ or tests/ (only the definition + analysis/*.md doc mentions). `format_album_info` (bot/ui/formatters.py:275) likewise has zero references except its own definition. `_parse_album` is called only by the dead `lookup_album` (lidarr.py:54) and by the unit test test_lidarr.py:69. The music handler flow (bot/handlers/music.py) only ever uses `lookup_artist` (via search_service, line 119) and `add_artist` (line 314); `selected_content` is only ever an 
- **Статус**: [x] Исправлено (раунд 4, мультиагент)

### DEAD-04: LidarrClient.search_album is never called
- **Файл**: `bot/clients/lidarr.py:160`
- **Проблема**: search_album() (triggers Lidarr AlbumSearch command) has zero callers in bot/ or tests. The artist-add path uses search_artist (add_service.py:759) only; there is no album-grain search flow.
- **Решение**: Delete LidarrClient.search_album (lidarr.py:160-164).
- **Верификация**: CONFIRMED — Independently verified. LidarrClient.search_album is defined at bot/clients/lidarr.py:160-164 (triggers the Lidarr "AlbumSearch" command). A repo-wide Grep for "search_album"/"AlbumSearch" across the whole tree returns only: (a) the definition in lidarr.py, and (b) the stale analysis/03-dead-code.md report — no callers in bot/ or tests/. A full enumeration of every "lidarr." method invocation in bot/ (search_service.py: lookup_artist; add_service.py: get_quality_profiles, get_metadata_profiles, get_root_folders, get_artist_by_mbid, add_artist, push_release, grab_release, search_artist; registr
- **Статус**: [x] Исправлено (раунд 4, мультиагент)

### DEAD-05: SonarrClient.lookup_series_by_tvdb is never called
- **Файл**: `bot/clients/sonarr.py:52`
- **Проблема**: lookup_series_by_tvdb() has zero callers. The symmetric Radarr method lookup_movie_by_tmdb IS used in the trending flow (trending.py:187/304), but the Sonarr counterpart was never wired in: the trending series path instead calls sonarr.lookup_series(series.title) (trending.py:435). So this method is orphaned.
- **Решение**: Delete SonarrClient.lookup_series_by_tvdb (sonarr.py:52ff), or wire it into the trending-series handler if a by-id lookup was intended.
- **Верификация**: CONFIRMED — Verified directly. (1) `SonarrClient.lookup_series_by_tvdb` is defined at bot/clients/sonarr.py:52-59. (2) A repo-wide Grep for `lookup_series_by_tvdb` over bot/ returns ONLY the definition line — no call sites anywhere; tests/ has zero matches; no getattr/dynamic dispatch exists. (3) The trending series path (bot/handlers/trending.py:435) resolves a missing TVDB ID via `sonarr_client.lookup_series(series.title)` (by-title, then matched on tmdb_id at line 438), never using lookup_series_by_tvdb. (4) The symmetric Radarr method lookup_movie_by_tmdb IS used at trending.py:187 and 304, confirming
- **Статус**: [x] Исправлено (раунд 4, мультиагент)

### DEAD-06: DeezerClient.get_trending_albums is never called
- **Файл**: `bot/clients/deezer.py:51`
- **Проблема**: get_trending_albums() has zero references in bot/ or tests. The trending-music handler only fetches artists via get_trending_artists (music.py:371). The album-chart variant is dead.
- **Решение**: Delete DeezerClient.get_trending_albums (deezer.py:51-74).
- **Верификация**: CONFIRMED — Verified against current code. DeezerClient.get_trending_albums is defined at bot/clients/deezer.py:51-74. A repo-wide grep for "get_trending_albums" returns exactly one hit — its definition — with zero callers in bot/ or tests/. The trending-music handler (bot/handlers/music.py:365-371) obtains the Deezer client via get_deezer() and only calls get_trending_artists(limit=10); there is no album-chart path. Tests (tests/test_lidarr.py:144,152,159) only exercise get_trending_artists. So get_trending_albums is genuinely unreferenced dead code. Severity low is correct: it is pure maintainability de
- **Статус**: [x] Исправлено (раунд 4, мультиагент)

### DEAD-07: Database.get_search_results and the search_results table reads are dead in production
- **Файл**: `bot/db.py:317`
- **Проблема**: save_search (db.py:280, called at search.py:259) writes results into the search_results table, but the only reader, Database.get_search_results (db.py:317), is referenced only in tests/test_db.py:119 — never in bot/. Production reloads result state from the sessions table (db.get_session). So the search_results table is write-only dead storage and get_search_results is dead production code. On a Raspberry Pi with SD-card I/O this also wastes a JSON-blob INSERT (full result set) on every search.
- **Решение**: Remove Database.get_search_results, and either stop persisting into search_results (drop the INSERT in save_search plus the table/index) or document why the write is retained. If save_search no longer needs to return an id, simplify it.
- **Верификация**: CONFIRMED — Verified against current code. bot/db.py:280-315 save_search does two INSERTs: into `searches` and into `search_results`, where it serializes the FULL result set as a JSON blob (results_json = json.dumps([r.model_dump(...) for r in results]) at line 302). bot/db.py:317-326 get_search_results is the only reader of search_results. A repo-wide Grep for get_search_results in bot/ returns exactly ONE hit — its own definition at db.py:317 — so it has zero production call sites; the only callers are tests/test_db.py:119 (reader) and test_db.py:350 (which only calls save_search). At the production cal
- **Статус**: [ ] Не исправлено

### DEAD-08: ScoringService.get_best_result and filter_by_quality are test-only
- **Файл**: `bot/services/scoring.py:262`
- **Проблема**: get_best_result (scoring.py:262) and filter_by_quality (scoring.py:290) are referenced only in tests/test_services.py (228, 238), never in bot/. Production picks the best release via results[0] after sort_results (search.py:270/403/567/791) and never filters by quality through this method. preferred_resolution (the only input filter_by_quality would consume) is also never passed to it in production.
- **Решение**: Remove ScoringService.get_best_result and filter_by_quality (and their tests), or wire filter_by_quality into the grab flow if the preferred_resolution preference is meant to actually filter releases.
- **Верификация**: CONFIRMED — Verified against current code. bot/services/scoring.py defines get_best_result at line 262 and filter_by_quality at line 290. A repo-wide grep for both names shows references ONLY in test files: tests/test_services.py:228/238 and tests/test_scoring.py:249-346 (including the only call sites passing preferred_resolution, e.g. test_scoring.py:303). No file under bot/ invokes either method. Production picks the best release via results[0] after sort_results: search_service.py:292 calls self.scoring.sort_results(...), and handlers select session.results[0]/results[0] at bot/handlers/search.py:270, 
- **Статус**: [ ] Не исправлено

### DEAD-09: NotificationService.force_check / get_stats / unsubscribe_user are test-only
- **Файл**: `bot/services/notification_service.py:200`
- **Проблема**: force_check (notification_service.py:200), get_stats (238), and unsubscribe_user (52) are referenced only in tests/test_qbittorrent.py (580, 553, 547); no handler or command invokes them in production. force_check duplicates most of the _check_for_completions logic but is never reachable from the running bot (no /notify command, no admin endpoint).
- **Решение**: Remove force_check and get_stats (and their tests). Keep unsubscribe_user only if a future unsubscribe command is planned; otherwise drop it too since subscribe_user is wired at main.py:118 but unsubscribe never is.
- **Верификация**: CONFIRMED — I opened bot/services/notification_service.py and confirmed all three methods exist at the cited lines: unsubscribe_user (line 52), force_check (line 200), get_stats (line 238). I grepped the entire bot/ production tree for force_check|get_stats|unsubscribe and the ONLY hits are the definition sites themselves in notification_service.py — no handler, no main.py, no other service calls them. Grepping all references repo-wide, the sole non-definition callers are tests/test_qbittorrent.py: test_unsubscribe_user (line 547 calls service.unsubscribe_user), test_get_stats (line 553 calls service.get_
- **Статус**: [ ] Не исправлено

### DEAD-10: UserPreferences.language field is never read or written
- **Файл**: `bot/models.py:243`
- **Проблема**: UserPreferences.language (default 'en') is never assigned or read anywhere. The only 'language' references are tmdb.py's unrelated self.language and config.py's tmdb_language. The field is serialized into the preferences JSON blob in the DB on every user update but has no effect (no i18n exists; all UI strings are hardcoded Russian).
- **Решение**: Remove the language field from UserPreferences (models.py:243).
- **Верификация**: CONFIRMED — Verified against current code. models.py:243 declares `language: str = "en"` on UserPreferences. An exhaustive grep for `preferences.language`, `prefs.language`, `.language =`, and getattr/setattr of "language" returned ZERO hits anywhere in bot/ or tests/ — the only `.language` write is tmdb.py:26 `self.language` on the unrelated TMDBClient class, and the only config reference is config.py:64 `tmdb_language` (a separate, functioning TMDB-content path used via registry.py:152 -> tmdb.py:17/71/102). Every OTHER UserPreferences field is actively read/written (settings.py:143-499, search.py:654-7
- **Статус**: [x] Исправлено (раунд 4, мультиагент)

### DEAD-11: SearchSession.monitor_type field is written but never read
- **Файл**: `bot/models.py:282`
- **Проблема**: SearchSession.monitor_type (models.py:282) is never read: grep for 'session.monitor_type' / '.monitor_type' (excluding kwarg assignments and model defs) returns nothing. The monitor_type values passed to grab_series_release/add_series are computed as fresh local variables in the handlers (e.g. search.py:725-728 sets monitor_type = 'existing'/'all') and never sourced from the session. The model field is dead.
- **Решение**: Remove the monitor_type field from SearchSession (models.py:282), or populate and read it if per-session monitor policy was intended.
- **Верификация**: CONFIRMED — Confirmed in current code. SearchSession.monitor_type is defined at bot/models.py:282 (Literal[...] = "all"). A grep for attribute access `\.monitor_type` over the entire bot/ tree returns ZERO matches, so the field is never read off any session. It is also never written: both SearchSession(...) constructors (bot/handlers/search.py:225-229 and 261-267) omit monitor_type, leaving it at its default. The monitor_type values actually used by the grab flow are computed as fresh local variables in the handler (search.py:721-728: force_download/is_season_pack -> "all", detected_season -> "existing") 
- **Статус**: [x] Исправлено (раунд 4, мультиагент)

### DEAD-12: SearchService.detect_content_type wrapper is test-only
- **Файл**: `bot/services/search_service.py:65`
- **Проблема**: detect_content_type (search_service.py:65) is a 'backward-compatible wrapper' around detect_with_confidence. Production calls detect_with_confidence directly (search.py:185). The wrapper is referenced only in tests (test_services.py:88/94, test_lidarr.py:284/298).
- **Решение**: Either delete the wrapper and update the tests to call detect_with_confidence, or keep it but acknowledge it exists solely for tests.
- **Верификация**: CONFIRMED — Independently verified in current code. bot/services/search_service.py:65-68 defines `async def detect_content_type(self, query)` whose docstring is literally "Backward-compatible wrapper around detect_with_confidence" — it calls self.detect_with_confidence(query) and returns only result.content_type, discarding confidence/reason/candidates. The sole production caller, bot/handlers/search.py:185, calls detect_with_confidence directly (it needs the full DetectionResult for stage_done logging at lines 190-192 and for content_type at 194), so it cannot and does not use the wrapper. Grepping `\.de
- **Статус**: [ ] Не исправлено

### DEAD-13: Dead defensive guard: hasattr(r, 'get_size_gb') on a typed SearchResult
- **Файл**: `bot/services/search_service.py:301`
- **Проблема**: In search_releases, top_preview computes 'size_gb': r.get_size_gb() if hasattr(r, 'get_size_gb') else None. r is always a SearchResult (results come from self.prowlarr.search / scoring.sort_results, both typed list[SearchResult]), and SearchResult always defines get_size_gb (models.py:92). The hasattr branch can never be False, so the 'else None' arm is unreachable.
- **Решение**: Replace with a plain r.get_size_gb() (search_service.py:301).
- **Верификация**: CONFIRMED — Verified in current code. bot/services/search_service.py:301 reads `"size_gb": r.get_size_gb() if hasattr(r, "get_size_gb") else None`. The variable `r` iterates over `results[:5]` (line 305). `results` originates from `self.prowlarr.search(...)` which is typed `-> list[SearchResult]` (bot/clients/prowlarr.py:36) and may be reassigned by `self.scoring.sort_results(...)` which is also typed `-> list[SearchResult]` (bot/services/scoring.py:245). `SearchResult` defines `get_size_gb` as a plain unconditional instance method at bot/models.py:92-94. Therefore every `r` is a `SearchResult` and `hasat
- **Статус**: [x] Исправлено (раунд 4, мультиагент)
