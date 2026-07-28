# Performance audit — TG_arr

### PERF-01: Duplicate type selection can start duplicate remote searches
- **Files:** `bot/handlers/search/commands.py:91-239`, `bot/handlers/music.py:134-186`
- **Risk:** needless Prowlarr/*arr load and race with the visible result.
- **Fix:** Addressed together with BUG-02 via a per-user search claim.
- **Status:** [ ] Open
