# Анализ — Логика / Архитектура · TG_arr (раунд 4, 2026-06-30)

Подтверждено находок: **4** (critical=0, high=0, medium=1, low=3). Все прошли состязательную верификацию (CONFIRMED/PLAUSIBLE).

## Средние

### LOGIC-01: REMUX scoring bonus (+30) is unreachable when title has no BluRay/WEB source token
- **Файл**: `bot/services/scoring.py:138`
- **Проблема**: The whole source-scoring block, including 'if quality.is_remux: score += source_remux' (+30), is nested under 'if quality.source:' (line 138). In prowlarr._parse_quality, is_remux = 'remux' in title_lower is set independently, but the substring 'remux' matches NONE of the source branches (bluray/web-dl/webrip/hdtv/dvdrip/cam/ts/tc), so a title like 'Movie.2021.2160p.REMUX.DTS-HD.MA' parses to source=None, is_remux=True. calculate_score then skips the entire block because quality.source is None, and the +30 remux bonus is never applied. A bare-REMUX 2160p release scores 30 points below an otherwise-identical BluRay and can be ranked below an inferior WEB-DL, despite remux being the top source tier.
- **Риск**: Best-result auto-grab and result ordering pick a lower-quality release over a REMUX.
- **Решение**: Move the remux check out of the 'if quality.source:' guard: add a standalone 'if quality.is_remux: score += self.weights.source_remux' (independent of source token), and turn the bluray/web-dl/... checks into elif of a non-remux branch so it is not double-counted when source already contains 'bluray'.
- **Верификация**: CONFIRMED — Verified directly in current code and by execution. In bot/services/scoring.py the source block opens with `if quality.source:` (line 138) and the remux bonus `if quality.is_remux: score += self.weights.source_remux` is nested inside it (lines 140-141). In bot/clients/prowlarr.py, `is_remux = "remux" in title_lower` (line 362) is set independently of `source`, and the source-detection branches (lines 291-307) only match bluray/blu-ray/bdrip, web-dl/webdl, webrip, hdtv, dvdrip, cam, ts, tc — none of which the substring 'remux' satisfies. So a title like `Movie.2021.2160p.REMUX.DTS-HD.MA` parses
- **Статус**: [x] Исправлено (раунд 4, TDD)

## Низкие

### LOGIC-03: bad_keywords language penalties false-positive on legitimately-titled content
- **Файл**: `bot/services/scoring.py:84`
- **Проблема**: bad_keywords includes language tags 'french','spanish','german','hindi','korean','chinese','ita' with word-boundary regex (lines 84-89, compiled at line 94). The patterns match anywhere in the release title, including the actual movie/series name. A release 'The.French.Dispatch.2021.1080p.BluRay' matches \bfrench\b and gets -3; 'The.Spanish.Apartment' matches \bspanish\b; a Korean-war documentary matches \bkorean\b. The penalty was intended to demote foreign-dub-only releases but mis-fires on titles where the word is part of the legitimate name, slightly mis-ranking those releases against identical-quality competitors.
- **Риск**: Releases whose real title contains a nationality word are unfairly down-ranked by a few points.
- **Решение**: Either drop the broad language-name penalties, or only apply them when the token appears as an audio/dub marker (e.g. require an adjacent delimiter pattern like '[.\s-](french|german|...)[.\s-](dub|audio|ac3|aac)' or restrict to the trailing tag region of the title rather than the whole string).
- **Верификация**: CONFIRMED — Opened bot/services/scoring.py. The bad_keywords dict (lines 72-90) currently contains the language tags 'ita':-3 (line 83), 'french':-3 (84), 'spanish':-3 (85), 'german':-3 (86), 'hindi':-3 (87), 'korean':-3 (88), 'chinese':-3 (89). These are compiled at lines 94-97 as re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE) — i.e. word-boundary, case-insensitive. In calculate_score (lines 232-236) the patterns are run via pattern.search(title) against the FULL release title (title = result.title, line 233), with no restriction to a trailing tag region and no dub/audio-marker adjacency requirement.
- **Статус**: [x] Исправлено (раунд 4, TDD)

### LOGIC-05: Calendar fetches Sonarr/Radarr/Lidarr sequentially instead of in parallel
- **Файл**: `bot/handlers/calendar.py:48`
- **Проблема**: _fetch_and_send_calendar awaits sonarr.get_calendar (line 49), then radarr.get_calendar (line 55), then lidarr.get_calendar (line 62) strictly sequentially. On the rpie4 -> VPS link each call is independent and read-only, so total latency is the sum of three round trips (and three potential timeouts) rather than the max. detect_with_confidence already uses asyncio.gather for the analogous parallel-lookup case; the calendar path does not, making the calendar button noticeably slower and more likely to hit the Telegram callback window on slow links.
- **Риск**: Calendar view latency = sum of 3 backends; slow/timeouts compound.
- **Решение**: Wrap the three get_calendar calls in asyncio.gather(..., return_exceptions=True) and partition successes/failures into episodes/movies/albums/errors, mirroring the gather pattern in search_service.detect_with_confidence.
- **Верификация**: CONFIRMED — CONFIRMED at code level. In bot/handlers/calendar.py, _fetch_and_send_calendar awaits the three calendar fetches strictly sequentially: `episodes = await sonarr.get_calendar(days=days)` (line 49), then `movies = await radarr.get_calendar(days=days)` (line 55), then `albums = await lidarr.get_calendar(days=days)` (line 62), each in its own try/except. Each await fully completes before the next starts. Each get_calendar is one independent read-only HTTP GET — verified in sonarr.py:254 `await self.get("/api/v3/calendar", params=params)`; radarr.py:187 and lidarr.py:166 are structurally identical 
- **Статус**: [x] Исправлено (раунд 4, мультиагент)

### LOGIC-06: Duplicated grab/push/qBittorrent-fallback flow copied 3x across movie/series/music with drifting behavior
- **Файл**: `bot/services/add_service.py:278`
- **Проблема**: grab_movie_release (line 278), grab_series_release (line 428) and grab_music_release (line 640) are ~95% identical: ensure-in-library, validate URL + push_release, direct grab on indexer_id>0, qBittorrent fallback, rejected-handling, search fallback. The copies have already drifted: e.g. the series push-rejection branch uses an explicit else (line 514) while music uses fall-through (line 715), and the rejection-message join differs (', '.join(rejections) vs ', '.join(str(r) for r in rejections)). This triplication is the architectural root that lets such inconsistencies and future bug-fix-misses creep in (a fix applied to one copy is easily forgotten in the others).
- **Риск**: Bug fixes applied to one arr flow silently miss the other two; behavior already diverging.
- **Решение**: Extract a single _grab_release(client, release, category, search_fallback_callable, action) helper that takes the per-*arr client and category and runs the shared push/grab/qBittorrent/fallback pipeline; have the three public methods only do the library-existence check and delegate. This is an architectural refactor (no behavior change intended).
- **Верификация**: CONFIRMED — I read all three methods in full in bot/services/add_service.py. The cited line numbers are accurate: grab_movie_release at 278, grab_series_release at 428, grab_music_release at 640. The three bodies are ~95% structurally identical — each does ensure-in-library, _validate_download_url + push_release, direct grab when release.indexer_id > 0 and not release_rejected, qBittorrent fallback (only category string differs: "radarr" L389 / "tv-sonarr" L541 / "music" L738), rejection handling, then a search fallback. Both cited drift points are real in the current code: (1) the push-rejection branch u
- **Статус**: [ ] Не исправлено

## Отклонено верификацией (false positives — не чинить)

- **LOGIC-02** Music releases get an unconditional -20 size penalty (MUSIC reuses movie size thresholds) — _The cited code is real but the failure path is unreachable. scoring.py:211-230 indeed only adjusts size thresholds for ContentType.SERIES (line 218) with no MUSIC branch, so calculate_score with content_type=MUSIC would apply movie thresholds (min 1.0GB). However, the finding's l_
- **LOGIC-04** ProwlarrClient.grab_release posts to the search endpoint and is dead/incorrect — _The finding's primary technical claim is false. I read bot/clients/prowlarr.py:447-457: grab_release POSTs {"guid","indexerId"} to "/api/v1/search" via _post_no_retry (a POST helper). The finding claims this "posts to the search endpoint" and would "treat a grab as a search and r_
