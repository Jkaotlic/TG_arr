# Logic / architecture audit — TG_arr

### LOGIC-01: Non-admins can trigger global Emby maintenance
- **File:** `bot/handlers/emby.py:122,144,173`
- **Risk:** Any allowed Telegram user can create library-wide I/O load.
- **Fix:** Require admin authorization, hide the controls for other users and test the denial.
- **Status:** [ ] Open

The larger handler/service split is intentionally deferred: it is architectural refactoring rather than a standalone defect.
