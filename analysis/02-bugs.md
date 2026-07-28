# Bugs audit — TG_arr

### BUG-01: Stale release buttons act on the current search session
- **Files:** `bot/models.py:252-271`, `bot/ui/callbacks.py:16-78`, `bot/handlers/search/results.py:31-70,149-169`
- **Risk:** A click in an old search result can grab a different, newer release.
- **Fix:** Bind stateful callbacks to a short search-session nonce and reject stale callbacks.
- **Status:** [ ] Open

### BUG-02: Parallel searches can overwrite each other
- **Files:** `bot/handlers/search/commands.py:91-239`, `bot/handlers/music.py:134-186`
- **Risk:** A slow old lookup can overwrite a newer query; a double type-selection can run duplicate searches.
- **Fix:** Claim a per-user search generation before the first await and ignore stale completions.
- **Status:** [ ] Open

### BUG-03: Emby transport retries are bypassed
- **File:** `bot/clients/emby.py:95-141`
- **Risk:** A transient Emby timeout fails immediately instead of using configured retries.
- **Fix:** Retry transport errors before converting them into `EmbyError`.
- **Status:** [ ] Open

### BUG-04: Cancelled lookups are swallowed
- **File:** `bot/services/search_service.py:127-130`
- **Risk:** Shutdown/task cancellation can be converted to a normal failed lookup.
- **Fix:** Re-raise `asyncio.CancelledError`, catch only `Exception` for aggregation.
- **Status:** [ ] Open
