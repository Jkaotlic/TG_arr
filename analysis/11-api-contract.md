# API-contract audit — TG_arr

### API-01: Stateful Telegram callback contract lacks session identity
- **Files:** `bot/ui/callbacks.py:16-78`, `bot/handlers/search/results.py:31-70`
- **Risk:** Old UI events operate on a different current session.
- **Fix:** version/bind callbacks to a search nonce and reject stale input (BUG-01).
- **Status:** [ ] Open

### API-02: Webhook endpoint can disclose its authentication secret via logs
- **File:** `bot/webhook.py:101-150`
- **Fix:** remove token-bearing path data from all log fields (SEC-01).
- **Status:** [ ] Open
