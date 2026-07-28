# Security audit — TG_arr

## Critical / high

### SEC-01: Webhook token is emitted to Docker logs
- **File:** `bot/webhook.py:101-150`
- **Problem:** Path authentication accepts `/webhook/<token>` and logs the matching route segment as `service`.
- **Risk:** Anyone with Portainer or Docker-log access can recover the webhook secret.
- **Fix:** Never log the path segment; log only a fixed authentication mode. Add a log-capture regression test.
- **Status:** [ ] Open

## Checked without a new finding

Allow-list middleware, URL SSRF guards, non-root image, dropped Linux capabilities and read-only root filesystem have source coverage.
