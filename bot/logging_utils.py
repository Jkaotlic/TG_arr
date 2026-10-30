"""Small logging helpers (log review 2026-10-10).

- `describe_error`: httpx timeouts stringify to "", so `error=str(e)` left
  lines like `library_watch_imports_failed error=""` in the live log.
- `TransientPollingFilter`: aiogram logs every failed getUpdates at ERROR,
  including Telegram's own nightly 502/429 burst it recovers from by itself.
- `process_rss_mb`: resident memory for the periodic stats line, so a leak
  shows up as a trend instead of a surprise OOM kill.
"""

import asyncio
import logging
from typing import Any, Optional

import structlog

#: aiogram's polling loop logs failures as "Failed to fetch updates - <Type>: <msg>".
_FETCH_FAILED = "Failed to fetch updates - "
_RECOVERED = "Connection established"

#: Failures aiogram retries through on its own: Telegram-side 5xx, flood
#: control, timeouts. Unauthorized (revoked token) and Conflict (a second
#: instance polling) are not transient and keep their ERROR level.
_TRANSIENT_TYPES = frozenset({"TelegramServerError", "TelegramRetryAfter", "TelegramNetworkError"})


def describe_error(error: BaseException) -> str:
    """`str(error)`, or the exception's type name when the message is empty."""
    return str(error) or type(error).__name__


def log_background_task_end(task: "asyncio.Task[Any]") -> None:
    """Done-callback фонового цикла: завершение, о котором не просили, это смерть.

    Наши циклы ловят CancelledError и выходят штатно, поэтому `task.cancelled()`
    мало: запрошенную отмену показывает `task.cancelling()`. Супервизора нет
    намеренно: задача мертва до перезапуска контейнера, журнал это показывает.
    """
    if task.cancelled() or task.cancelling():
        return
    error = task.exception()
    structlog.get_logger().error(
        "background_task_died",
        task=task.get_name(),
        error=describe_error(error) if error is not None else "цикл завершился сам",
        error_type=type(error).__name__ if error is not None else None,
        exc_info=error,
    )


class TransientPollingFilter(logging.Filter):
    """Downgrade transient getUpdates failures to WARNING — up to a point.

    The first `threshold` transient failures in a row are logged as WARNING;
    from then on they are ERROR again, so a real outage (≈12 min of 72 s
    timeouts at the default 10) is not hidden. aiogram's "Connection
    established" line resets the streak. Never drops a record.
    """

    def __init__(self, threshold: int = 10) -> None:
        super().__init__()
        self.threshold = threshold
        self._streak = 0

    def filter(self, record: logging.LogRecord) -> bool:
        if not isinstance(record.msg, str):
            return True
        if record.msg.startswith(_RECOVERED):
            self._streak = 0
        elif record.msg.startswith(_FETCH_FAILED) and record.levelno == logging.ERROR:
            kind = record.args[0] if isinstance(record.args, tuple) and record.args else None
            if kind in _TRANSIENT_TYPES:
                self._streak += 1
                if self._streak <= self.threshold:
                    record.levelno = logging.WARNING
                    record.levelname = logging.getLevelName(logging.WARNING)
        return True


def process_rss_mb(status_path: str = "/proc/self/status") -> Optional[float]:
    """Resident set size of this process in MB (one decimal); None where
    procfs is unavailable (Windows/macOS dev runs) or unreadable."""
    try:
        with open(status_path, encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except (OSError, ValueError, IndexError):
        return None
    return None
