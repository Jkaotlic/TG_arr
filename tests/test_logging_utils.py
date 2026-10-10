"""Log review 2026-10-10: helpers behind three observability fixes.

- `describe_error`: httpx timeouts stringify to "" — the live log carried
  `library_watch_imports_failed error=""` with no way to tell what failed.
- `TransientPollingFilter`: aiogram logs every failed getUpdates at ERROR;
  Telegram's nightly 502/429 burst made ~8 false alarms a day.
- `process_rss_mb`: memory crept to 199 of 256 MB with nothing in the log
  to tell a leak from a one-off step.
"""

import logging

import httpx
import pytest
from structlog.testing import capture_logs


# ------------------------------------------------------------ describe_error
def test_describe_error_falls_back_to_type_name_for_empty_message():
    from bot.logging_utils import describe_error

    assert describe_error(httpx.ReadTimeout("")) == "ReadTimeout"


def test_describe_error_keeps_a_real_message():
    from bot.logging_utils import describe_error

    assert describe_error(ValueError("boom")) == "boom"


def test_poll_failure_log_names_the_error_type():
    from bot.services.library_watcher import _log_poll_failure

    with capture_logs() as logs:
        _log_poll_failure("library_watch_imports_failed", httpx.ReadTimeout(""), service="radarr")

    [entry] = logs
    assert entry["error"] == "ReadTimeout"
    assert entry["error_type"] == "ReadTimeout"


# ------------------------------------------------------- polling log filter
def _record(msg: str, *args: object, level: int = logging.ERROR) -> logging.LogRecord:
    return logging.LogRecord("aiogram.dispatcher", level, __file__, 1, msg, args, None)


_FETCH = "Failed to fetch updates - %s: %s"


@pytest.mark.parametrize(
    "kind", ["TelegramServerError", "TelegramRetryAfter", "TelegramNetworkError"],
)
def test_transient_fetch_failure_is_downgraded_to_warning(kind):
    from bot.logging_utils import TransientPollingFilter

    f = TransientPollingFilter()
    record = _record(_FETCH, kind, "Bad Gateway")

    assert f.filter(record) is True  # never drops a line
    assert record.levelno == logging.WARNING
    assert record.levelname == "WARNING"


def test_unauthorized_and_conflict_stay_errors():
    from bot.logging_utils import TransientPollingFilter

    f = TransientPollingFilter()
    for kind in ("TelegramUnauthorizedError", "TelegramConflictError"):
        record = _record(_FETCH, kind, "x")
        f.filter(record)
        assert record.levelno == logging.ERROR


def test_long_outage_escalates_back_to_error_after_threshold():
    from bot.logging_utils import TransientPollingFilter

    f = TransientPollingFilter(threshold=10)
    levels = []
    for _ in range(11):
        record = _record(_FETCH, "TelegramNetworkError", "Request timeout error")
        f.filter(record)
        levels.append(record.levelno)

    assert levels[:10] == [logging.WARNING] * 10
    assert levels[10] == logging.ERROR


def test_connection_established_resets_the_streak():
    from bot.logging_utils import TransientPollingFilter

    f = TransientPollingFilter(threshold=2)
    for _ in range(2):
        f.filter(_record(_FETCH, "TelegramServerError", "Bad Gateway"))
    f.filter(_record("Connection established (tryings = %d, bot id = %d)", 2, 1, level=logging.INFO))

    record = _record(_FETCH, "TelegramServerError", "Bad Gateway")
    f.filter(record)
    assert record.levelno == logging.WARNING


def test_setup_logging_installs_the_filter_on_aiogram_dispatcher():
    from bot.logging_utils import TransientPollingFilter
    from bot.main import setup_logging

    dispatcher = logging.getLogger("aiogram.dispatcher")
    before = list(dispatcher.filters)
    try:
        setup_logging(log_level="INFO", log_format="json")
        setup_logging(log_level="INFO", log_format="json")  # idempotent
        installed = [f for f in dispatcher.filters if isinstance(f, TransientPollingFilter)]
        assert len(installed) == 1
    finally:
        dispatcher.filters[:] = before


def test_downgraded_line_renders_as_warning_in_json(capsys):
    """End to end: the JSON `level` field follows the filter's downgrade."""
    import json

    import structlog

    from bot.main import setup_logging

    root = logging.getLogger()
    dispatcher = logging.getLogger("aiogram.dispatcher")
    prev = (list(root.handlers), root.level, list(dispatcher.filters))
    try:
        setup_logging(log_level="INFO", log_format="json")
        dispatcher.error(_FETCH, "TelegramServerError", "Telegram server says - Bad Gateway")
        line = capsys.readouterr().out.strip().splitlines()[-1]
        assert json.loads(line)["level"] == "warning"
    finally:
        root.handlers[:], dispatcher.filters[:] = prev[0], prev[2]
        root.setLevel(prev[1])
        structlog.reset_defaults()


# ------------------------------------------------------------ process_rss_mb
def test_process_rss_mb_reads_vmrss(tmp_path):
    from bot.logging_utils import process_rss_mb

    status = tmp_path / "status"
    status.write_text("Name:\tpython\nVmHWM:\t  208684 kB\nVmRSS:\t  199432 kB\n")

    assert process_rss_mb(str(status)) == 194.8


def test_process_rss_mb_is_none_without_procfs(tmp_path):
    from bot.logging_utils import process_rss_mb

    assert process_rss_mb(str(tmp_path / "missing")) is None


# ---------------------------------------------------- periodic stats line
async def _one_cleanup_cycle(monkeypatch, notification_service):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from bot import main

    db = AsyncMock()
    db.run_maintenance.return_value = {}
    logger = MagicMock()
    sleeps = 0

    async def fake_sleep(_interval):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError()

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(main, "process_rss_mb", lambda: 194.8)
    await main._periodic_cleanup(db, logger, notification_service)
    return {c.args[0]: c.kwargs for c in logger.info.call_args_list}


@pytest.mark.asyncio
async def test_notification_stats_line_carries_rss(monkeypatch):
    from unittest.mock import MagicMock

    service = MagicMock()
    service.get_stats.return_value = {"running": True}

    lines = await _one_cleanup_cycle(monkeypatch, service)

    assert lines["notification_stats"] == {"running": True, "rss_mb": 194.8}


@pytest.mark.asyncio
async def test_rss_is_logged_even_without_notification_service(monkeypatch):
    lines = await _one_cleanup_cycle(monkeypatch, None)

    assert lines["process_stats"] == {"rss_mb": 194.8}


# Background services log to nobody but the log; an empty `error=""` there is
# a dead end. Handlers/clients are out of scope: their errors reach the chat.
_BACKGROUND_MODULES = (
    "bot/main.py",
    "bot/services/digest.py",
    "bot/services/health_monitor.py",
    "bot/services/library_watcher.py",
    "bot/services/live_status.py",
    "bot/services/music_retry.py",
    "bot/services/notification_service.py",
)


@pytest.mark.parametrize("path", _BACKGROUND_MODULES)
def test_background_modules_never_log_bare_str_of_exception(path):
    import re
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / path).read_text(encoding="utf-8")
    offenders = re.findall(r"error=str\(\w+\)", source)
    assert offenders == [], f"{path}: use describe_error() — {len(offenders)} left"
