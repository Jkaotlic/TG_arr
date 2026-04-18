FROM python:3.12-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

FROM python:3.12-slim
RUN useradd -m -u 1000 botuser
WORKDIR /app
COPY --from=builder /root/.local /home/botuser/.local
ENV PATH=/home/botuser/.local/bin:$PATH \
    PYTHONUNBUFFERED=1
RUN mkdir -p /app/data && chown -R botuser:botuser /app
COPY --chown=botuser:botuser bot/ ./bot/
USER botuser

# SEC-14 / DEPLOY-04: true liveness — checks that /tmp/tgarr-alive has been
# touched within the last 2 minutes by the bot's event loop. Manual verify:
# `docker kill --signal=SIGSTOP <cid>` → unhealthy in < 2 min.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD sh -c 'find /tmp/tgarr-alive -mmin -2 | grep -q alive'

CMD ["python", "-m", "bot.main"]
