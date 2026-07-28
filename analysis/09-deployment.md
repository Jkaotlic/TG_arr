# Deployment / IaC audit — TG_arr

### DEPLOY-01: Deploy and rollback do not wait for health
- **File:** `Makefile:57-71`
- **Fix:** use `docker compose up -d --wait --wait-timeout` and show logs on failure.
- **Status:** [ ] Open

### DEPLOY-02: Webhook setting is not reachable in the stock compose
- **Files:** `docker-compose.yml:74-87`, `README.md:174-176`
- **Risk:** enabling the feature starts an internal-only listener; the Windows ARR host cannot deliver events.
- **Fix:** supply a documented secure port mapping when enabled, and keep it absent by default.
- **Status:** [ ] Open
