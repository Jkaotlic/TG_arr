# DEPLOY-03: pin the base image by multi-arch index digest instead of the
# floating `python:3.12-slim` tag for reproducible builds (amd64 + arm64 for
# the Raspberry Pi). Digest resolved 2026-06-30 → python 3.12.13-slim-trixie.
# Refresh with: docker buildx imagetools inspect python:3.12-slim
FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf
# DEPLOY-03: install tzdata so Python's datetime.now()/structlog timestamps
# match TIMEZONE=Europe/Moscow instead of defaulting to UTC.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 botuser
WORKDIR /app
COPY --from=builder /root/.local /home/botuser/.local
ENV PATH=/home/botuser/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Moscow
RUN mkdir -p /app/data && chown -R botuser:botuser /app
COPY --chown=botuser:botuser bot/ ./bot/
USER botuser

# SEC-14 / DEPLOY-04: true liveness — checks that /tmp/tgarr-alive has been
# touched within the last 2 minutes by the bot's event loop. Manual verify:
# `docker kill --signal=SIGSTOP <cid>` → unhealthy in < 2 min.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD sh -c 'find /tmp/tgarr-alive -mmin -2 | grep -q alive'

CMD ["python", "-m", "bot.main"]
