# Testing-quality audit — TG_arr

### TEST-01: No opt-in real-stack smoke boundary
- **Files:** `tests/test_search_grab_flow.py:256-267`, `tests/test_clients.py:205-230`
- **Risk:** mocks cover business branches but not the deployed Homeserver API, qBit category or arm64 image boundary.
- **Fix:** Add an explicitly environment-gated, non-destructive integration smoke suite.
- **Status:** [ ] Open

### TEST-02: Type-check gate is red
- **File:** `Makefile:49-50`
- **Risk:** type regressions cannot be distinguished from the existing noise.
- **Fix:** See DEP-02.
- **Status:** [ ] Open
