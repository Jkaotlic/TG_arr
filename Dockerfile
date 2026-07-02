# DEPLOY-03: pin the base image by multi-arch index digest instead of the
# floating `python:3.12-slim` tag for reproducible builds (amd64 + arm64 for
# the Raspberry Pi). Digest resolved 2026-06-30 → python 3.12.13-slim-trixie.
# Refresh with: docker buildx imagetools inspect python:3.12-slim
FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf AS builder
WORKDIR /build
# DEP-02: install from the fully-resolved lock (top-level + transitive, pinned
# to the exact arm64/py3.12 versions) for reproducible builds. requirements.txt
# is copied too so it stays present as the human-readable source of top-level
# pins; the lock is what pip actually resolves against.
COPY requirements.txt requirements.lock ./
RUN pip install --user --no-cache-dir -r requirements.lock

FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf
# DEPLOY-07: tzdata is needed for ZoneInfo(settings.timezone) — used to render
# user-facing timestamps (calendar, history, notifications) in TIMEZONE=
# Europe/Moscow, see bot/ui/formatters.py. It does NOT affect structlog: the
# JSON logs' TimeStamper defaults to utc=True, so `docker logs` timestamps
# stay in UTC regardless of TZ/TIMEZONE — that's intentional, not a bug.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 botuser
WORKDIR /app
COPY --from=builder /root/.local /home/botuser/.local
ENV PATH=/home/botuser/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Moscow
# DEPLOY-05: only /app/data (sqlite db) needs to be writable by botuser at
# runtime — the app code itself doesn't. Leaving bot/ root-owned means a
# compromised process can't rewrite its own source even without read_only
# rootfs; combined with `read_only: true` in compose it's defense-in-depth.
RUN mkdir -p /app/data && chown -R botuser:botuser /app/data
COPY bot/ ./bot/
USER botuser

# SEC-14 / DEPLOY-04: true liveness — checks that /tmp/tgarr-alive has been
# touched within the last 2 minutes by the bot's event loop. Manual verify:
# `docker kill --signal=SIGSTOP <cid>` → unhealthy in < 2 min.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD sh -c 'find /tmp/tgarr-alive -mmin -2 | grep -q alive'

CMD ["python", "-m", "bot.main"]
