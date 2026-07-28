# Observability audit — TG_arr

### OBS-01: Deployment path ignores the existing health signal
- **Files:** `Makefile:57-71`, `Dockerfile:38-40`
- **Risk:** `make deploy` can print container state and exit successfully while the rollout is unhealthy.
- **Fix:** Wait for health with a bounded timeout and print diagnostic logs on failure.
- **Status:** [ ] Open

Live RPi4 data is recorded in the summary; watchdog, liveness and structured logs are present and healthy.
