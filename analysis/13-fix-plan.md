# TG_arr Audit Fix Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Remove confirmed authorization, stale-action, retry, cancellation and deploy-observability defects without changing the deployed ARR data model.

**Architecture:** The bot remains a polling Telegram process. Stateful UI actions gain a server-side freshness check; transport retries retain their original exception type; Docker health is made an actual deployment gate. External ARR probes remain read-only.

**Tech Stack:** Python 3.12, aiogram, httpx/tenacity, pytest, Docker Compose.

## Global constraints

- Do not alter ARR libraries, queues, root folders or qBittorrent state during verification.
- Every observable behaviour fix starts with a failing regression test.
- Keep production webhook disabled and unexposed unless the user explicitly enables it.

## Fixes (this cycle)

### Task 1: Remove webhook-token log disclosure

**Files:** modify `bot/webhook.py`; test `tests/test_feat_webhook.py`.

- [ ] Add a regression that sends `/webhook/<secret>` through a captured logger and asserts the secret is absent.
- [ ] Replace route-segment logging with a non-secret authentication mode field.
- [ ] Run `pytest tests/test_feat_webhook.py -q`.

### Task 2: Protect Emby-wide maintenance

**Files:** modify `bot/handlers/emby.py` and its keyboard path; test `tests/test_handlers_status_emby.py`.

- [ ] Add non-admin cases that assert an alert and no `scan_library`/`refresh_library` call.
- [ ] Require the injected admin flag before invoking global Emby scan/refresh operations and hide those actions for non-admin users.
- [ ] Run the focused handler tests.

### Task 3: Correct retry and cancellation propagation

**Files:** modify `bot/clients/emby.py`, `bot/services/search_service.py`; test `tests/test_clients.py`, `tests/test_services.py`.

- [ ] Add tests for retrying two transient Emby timeouts, not retrying authentication failure, and re-raising `CancelledError`.
- [ ] Preserve transport exceptions until Tenacity has exhausted retries, then translate them; re-raise cancellation explicitly.
- [ ] Run focused client/service tests.

### Task 4: Make search callbacks session-safe

**Files:** `bot/models.py`, `bot/db.py`, `bot/ui/callbacks.py`, `bot/ui/keyboards/*`, `bot/handlers/search/*`, `bot/handlers/music.py`; related tests.

- [ ] Add failing regressions for an old release/page button after a newer search, and for slow A / fast B searches by one user.
- [ ] Add a compact session nonce and generation claim; carry/validate it in stateful callback data before rendering or grabbing.
- [ ] Run all search/callback tests plus the full suite.

### Task 5: Make deployment health-aware

**Files:** `Makefile`, deployment tests/docs.

- [ ] Add a command-contract test for `--wait` and bounded timeout in deploy and rollback.
- [ ] Update Make targets to fail on an unhealthy rollout and emit bounded diagnostic logs.
- [ ] Run compose config validation and deployment contract test.

### Task 6: Fix delivery/documentation gaps

**Files:** `docker-compose.yml`, `README.md`, `requirements.lock`, Dockerfile and test/docs as applicable.

- [ ] Keep webhooks off/unpublished by default, document the secure Portainer port mapping required when explicitly enabled, and test the configuration contract.
- [ ] Add hash verification to runtime dependency installation, or document the explicit reproducibility boundary if a cross-platform hash lock cannot be safely generated in this run.
- [ ] Mark the mypy gate honestly and split remaining annotation debt into a separately testable follow-up, avoiding a fake green gate.

## Refactoring (deferred — separate review)

- [ ] Split the large search/handler modules only after the nonce correctness change is stable.
- [ ] Resolve the broad mypy annotation backlog module-by-module; it is a non-runtime quality improvement and should not be hidden by weakening mypy.
