# Dependencies audit — TG_arr

### DEP-01: Runtime lock does not verify artifact hashes
- **Files:** `Dockerfile:9-10`, `requirements.lock:1-9`
- **Risk:** Exact versions constrain resolution but do not validate the downloaded artifact.
- **Fix:** Produce a hash-locked runtime requirements file and install it with `--require-hashes`.
- **Status:** [ ] Open

### DEP-02: Static type gate is advertised but currently fails
- **Files:** `Makefile:49-50`, `README.md:297-300`
- **Evidence:** `python -m mypy bot/` reports 110 errors on the installed toolchain.
- **Fix:** Make the supported typecheck command clean, or stop presenting it as a passing gate.
- **Status:** [ ] Open
