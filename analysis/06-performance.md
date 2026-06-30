# Анализ — Производительность · TG_arr (раунд 4, 2026-06-30)

Подтверждено находок: **7** (critical=0, high=0, medium=4, low=3). Все прошли состязательную верификацию (CONFIRMED/PLAUSIBLE).

Цель — Raspberry Pi 4 (ARM, SD-card, ограниченный CPU/RAM).

## Средние

### PERF-01: Pause/resume/delete torrent callbacks fetch the ENTIRE torrent list 2-3x per click
- **Файл**: `bot/clients/qbittorrent.py:303`
- **Проблема**: get_torrent_by_short_hash() calls get_torrents() with NO hashes filter, pulling qBittorrent's full /api/v2/torrents/info payload (all ~30 fields per torrent) and filtering in Python by short hash. In downloads.py every mutating callback calls it twice: handle_pause_torrent (line 335 + line 348 re-fetch), handle_resume_torrent (368+379), and t_delete/t_delf/t_recheck/t_prio each call it once, while pause/resume then redraw the whole list with another full get_torrents(). With 100-200 torrents on the Pi, a single pause tap triggers 2 full list serializations/transfers/JSON-parses (~hundreds of KB each) plus a parse of every TorrentInfo, instead of one hash-scoped request. On rpie4 over Wi-Fi this turns a sub-100ms action into a multi-second one and burns CPU parsing torrents the user did not touch.
- **Риск**: Low — behavior-preserving; only removes redundant full-list fetches.
- **Решение**: qBittorrent /api/v2/torrents/info accepts a 'hashes' parameter, but short_hash is only the first 8 chars so an exact lookup needs the full hash. Cache the last-rendered (short_hash -> full_hash) map from the list render, or fetch once and pass the resolved full hash to pause/resume/delete and skip the second re-fetch (reuse the already-parsed torrent and locally flip its state for the redraw). At minimum, do NOT re-call get_torrent_by_short_hash after the mutate — reuse the torrent object already in hand.
- **Верификация**: CONFIRMED — Verified directly in the current code. bot/clients/qbittorrent.py:266-301 get_torrents() builds params with only filter/sort/reverse (+optional category/limit/offset) and NEVER a 'hashes' parameter; it GETs /api/v2/torrents/info and returns the full parsed list. get_torrent_by_short_hash() (lines 303-309) calls get_torrents() with no args, pulling the entire payload, then filters in Python via t.hash.lower().startswith(short_hash). In bot/handlers/downloads.py the per-tap cost is exactly as claimed: handle_pause_torrent fetches the full list twice (line 335 lookup + line 348 re-fetch 'to show 
- **Статус**: [ ] Не исправлено

### PERF-03: Calendar fetches Sonarr+Radarr+Lidarr sequentially instead of in parallel
- **Файл**: `bot/handlers/calendar.py:48`
- **Проблема**: _fetch_and_send_calendar awaits sonarr.get_calendar(), then radarr.get_calendar(), then lidarr.get_calendar() one after another (lines 49, 55, 62). Each is an independent HTTP round-trip to a different *arr service. On rpie4 over Wi-Fi each call is 0.3-1.5s, so the user waits the SUM (up to ~4.5s) instead of the MAX (~1.5s). The status handler already does this correctly with asyncio.gather; calendar does not.
- **Риск**: Low — gather with return_exceptions preserves current per-service error handling.
- **Решение**: Run the three get_calendar calls concurrently with asyncio.gather(..., return_exceptions=True) and unpack results/exceptions per service (preserving the per-service error messages already appended to `errors`).
- **Верификация**: CONFIRMED — Opened bot/handlers/calendar.py. _fetch_and_send_calendar performs three independent, sequential awaits: line 49 `episodes = await sonarr.get_calendar(days=days)`, line 55 `movies = await radarr.get_calendar(days=days)`, line 62 `albums = await lidarr.get_calendar(days=days)`. Each writes a distinct variable (episodes/movies/albums) with no data dependency between them, and each is a separate HTTP round-trip to a different *arr service. Because each `await` blocks until completion before the next begins, the handler's latency is the SUM of the three calls rather than the MAX — exactly as claim
- **Статус**: [ ] Не исправлено

### PERF-04: Emby status does 3 sequential round-trips (server_info, libraries, sessions)
- **Файл**: `bot/handlers/emby.py:35`
- **Проблема**: show_emby_status awaits emby.get_server_info() (line 35), then get_libraries() (36), then get_sessions() (37) serially. These are three independent Emby HTTP GETs; the user waits their sum. On the Pi/Emby box this adds avoidable latency to every /emby open and refresh (and to scan-movies/scan-series which call get_libraries again right before refresh).
- **Риск**: Low — independent reads, no ordering dependency.
- **Решение**: Gather the three independent reads with asyncio.gather(emby.get_server_info(), emby.get_libraries(), emby.get_sessions()). For scan handlers, the libraries list is already fetched in status — pass it through or cache briefly instead of re-fetching.
- **Верификация**: CONFIRMED — Opened bot/handlers/emby.py and bot/clients/emby.py. show_emby_status awaits three independent reads serially: emby.get_server_info() (line 35), emby.get_libraries() (line 36), emby.get_sessions() (line 37). In bot/clients/emby.py these map to three independent HTTP GETs with no inter-dependency: get_server_info -> GET /System/Info (line 137), get_libraries -> GET /Library/VirtualFolders (line 156), get_sessions -> GET /Sessions (line 217). The results are only combined afterward in Formatters.format_emby_status (line 39), so nothing forces sequencing. The user therefore waits the SUM of three
- **Статус**: [ ] Не исправлено

### PERF-05: qBittorrent get_status() makes 4 sequential API calls per /qstatus and per speed menu
- **Файл**: `bot/clients/qbittorrent.py:218`
- **Проблема**: get_status() awaits get_version() (225), get_transfer_info() (228), _request(sync/maindata) (231), then get_torrents() (235) — four serial HTTP round-trips, the last returning the full torrent list just to count downloading/seeding/paused. It is invoked by cmd_qstatus and by handle_speed_menu (downloads.py:640) and after every speed change (handle_speed_set -> handle_speed_menu). On rpie4 this stacks 4 latencies plus a full-list parse for what is mostly summary data already present in sync/maindata (server_state has counts/speeds).
- **Риск**: Low — counts derivable from maindata; gather is behavior-preserving.
- **Решение**: version + transfer + counts can come largely from a single /api/v2/sync/maindata call (server_state includes dl/up speeds, free_space, dht_nodes, and per-torrent states for counting). Fetch version once and cache it (it never changes within a session). At minimum gather the independent calls with asyncio.gather instead of awaiting serially.
- **Верификация**: CONFIRMED — Opened bot/clients/qbittorrent.py:218-264 and reproduced the exact path. get_status() awaits four serial HTTP round-trips with no concurrency: get_version() at line 225 (/api/v2/app/version), get_transfer_info() at line 228 (/api/v2/transfer/info), _request GET /api/v2/sync/maindata at line 231, and get_torrents() at line 235 (/api/v2/torrents/info). Each await blocks on the prior. The full torrent list from call #4 is used only to compute three counts (active_downloads/active_uploads/paused_count at lines 237-245) and len() at line 257. Confirmed no version caching exists: grep for _version/s
- **Статус**: [ ] Не исправлено

## Низкие

### PERF-02: Notification monitor polls full torrent list every 60s pulling all fields just to check completion
- **Файл**: `bot/services/notification_service.py:139`
- **Проблема**: _check_for_completions() (run every notify_check_interval, default 60s, min 10s) calls qbittorrent.get_torrents() which hits /api/v2/torrents/info with no field filter and parses every torrent into a full TorrentInfo (dates, tags, speeds, ratios, etc.) only to read .hash, .progress and .state. With many torrents and a 10-60s loop this is continuous wakeups + JSON parsing on an idle Pi, competing with SD-card I/O and the event loop. qBittorrent supports /api/v2/sync/maindata with an rid for delta updates (only changed torrents), which is dramatically cheaper for a polling loop.
- **Риск**: Low-medium — maindata delta handling needs care for removed torrents, but the existing removed-hash reconciliation logic maps cleanly onto it.
- **Решение**: Switch the monitor loop to /api/v2/sync/maindata with a persisted rid so qBittorrent returns only deltas, or add a lightweight get_torrents variant requesting only hash/progress/state. Also consider widening the default notify_check_interval; 60s of full-list polling on a quiet Pi is wasteful.
- **Верификация**: CONFIRMED — I opened the cited file and the qBittorrent client and confirmed every claim against the current code. notification_service.py:139 (_check_for_completions) calls self.qbittorrent.get_torrents() with no arguments — and so do _initial_sync (line 116) and force_check (line 213). The loop at line 97-104 runs this every settings.notify_check_interval seconds. config.py:69 confirms notify_check_interval defaults to 60 with ge=10 (min 10s), le=3600 — exactly as claimed. get_torrents() (qbittorrent.py:266-301) hits GET /api/v2/torrents/info with NO field filter, and for every returned torrent calls _p
- **Статус**: [ ] Не исправлено

### PERF-06: Pagination/back re-fetch nothing but re-serialize full session JSON to SQLite on every page tap
- **Файл**: `bot/handlers/search.py:396`
- **Проблема**: handle_pagination (and handle_back, handle_release_selection, grab_best) call db.save_session(user_id, session) on every interaction. save_session serializes the ENTIRE SearchSession — up to 500 SearchResult models — to JSON via model_dump_json and writes+commits to SQLite on the SD card, even when only current_page (an int) changed. On rpie4 with WAL + synchronous=NORMAL each commit still fsyncs periodically; a user flipping through 10 result pages triggers 10 multi-KB JSON serializations and DB writes for a one-integer change. This adds latency and SD-card wear.
- **Риск**: Low — current_page is reconstructable from callback data; results blob is unchanged across page taps.
- **Решение**: For page-only changes, persist just the page number (a tiny UPDATE of a separate column) rather than re-dumping the whole results blob, or skip persisting current_page entirely and recompute the page from the callback each time (the page index is already encoded in the callback data). Avoid re-serializing 500 results to change one int.
- **Верификация**: CONFIRMED — Verified against current code. bot/handlers/search.py:394-396 (handle_pagination) sets only session.current_page = page (a single int parsed from callback.data at line 382) and then calls db.save_session(user_id, session). bot/db.py:329-354 (save_session) unconditionally calls session.model_dump_json() over the ENTIRE SearchSession — including session.results, a list of up to 500 SearchResult models (models.py:276 max_length=500). Each SearchResult (models.py:53-96) is a heavy ~20-field model with nested QualityInfo and several list fields, so the dump is multi-KB. save_session then does an IN
- **Статус**: [ ] Не исправлено

### PERF-07: Trending detail/add re-runs full content-type-aware lookups; series re-lookup on every add
- **Файл**: `bot/handlers/trending.py:433`
- **Проблема**: handle_add_series_from_trending resolves TVDB ID by calling sonarr_client.lookup_series(series.title) (line 435) on every add when tvdb_id is 0 (TMDb trending ALWAYS returns tvdb_id=0 per tmdb.py:182), and get_radarr_profiles/get_radarr_root_folders (and Sonarr equivalents) are fetched fresh on every single add. Profiles and root folders change rarely; fetching them on each grab/add is repeated N+1 HTTP work against Radarr/Sonarr for data that is effectively static within a session.
- **Риск**: Low — profiles/folders rarely change; TTL bounds staleness.
- **Решение**: Cache quality profiles and root folders per-service with a short TTL (e.g. 5 min) in the registry or AddService, since they are near-static. This removes 2 extra HTTP round-trips from every add/grab across trending.py and search.py.
- **Верификация**: CONFIRMED — Verified against current code. (1) bot/clients/tmdb.py:182 hardcodes tvdb_id=0 for TMDB trending series (comment: "Will be looked up via Sonarr"), so the lookup branch always triggers for trending series. (2) bot/handlers/trending.py:433-435 — handle_add_series_from_trending checks `if not series.tvdb_id:` (line 433) and calls `sonarr_client.lookup_series(series.title)` (line 435) on every add; cited lines match exactly. (3) Profiles/root-folders are fetched fresh on every add: trending.py:417-418 (sonarr) and 330-331 (radarr movie path), and the same pattern in search.py:672-673/710-711 and m
- **Статус**: [ ] Не исправлено
