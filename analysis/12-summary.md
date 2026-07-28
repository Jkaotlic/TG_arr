# TG_arr audit summary — 2026-07-18

## Live state

- GitHub `origin/master` and local HEAD are identical at `6a5e0d3`.
- RPi4 bot is healthy, running for 13 days with zero restarts and fresh liveness heartbeats.
- Homeserver ARR services and qBittorrent are running; final queue/indexer/root evidence is pending the live-audit pass.

## Confirmed findings

High: SEC-01 (webhook-token log leak), LOGIC-01 (non-admin Emby maintenance), DEPLOY-01 (unverified deploy), TEST-01 (missing live boundary).

Medium: BUG-01/02 (stale and racing searches), BUG-03 (Emby retry), BUG-04 (cancellation), DEPLOY-02 (unreachable webhook), DEP-02 (red type gate).

Low/medium: DEP-01 (no artifact hashes).

## Verification already run

- `git fetch origin --prune`: up to date.
- `python -m ruff check bot/ tests/`: pass.
- `python -m pytest tests/ -q --basetemp C:\tmp\tgarr-pytest`: 680 passed.
- `python -m mypy bot/`: fails (existing type debt).
