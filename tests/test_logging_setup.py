"""OBS-01/OBS-13: stdlib loggers (aiogram/httpx/aiohttp) must flow through the
same JSON+mask pipeline as structlog, and log rendering format must be
independent from log level (LOG_FORMAT vs LOG_LEVEL)."""

import json
import logging

import pytest
import structlog


@pytest.fixture(autouse=True)
def _reset_logging():
    """setup_logging() mutates global logging/structlog state; restore it so
    tests don't leak handlers into each other."""
    root = logging.getLogger()
    prev_handlers = list(root.handlers)
    prev_level = root.level
    yield
    root.handlers[:] = prev_handlers
    root.setLevel(prev_level)
    structlog.reset_defaults()


def test_stdlib_logger_emits_masked_json(capsys):
    """A stdlib logger (e.g. 'httpx') must produce a JSON line through the
    same processor pipeline — including token masking — not a bare string."""
    from bot.main import setup_logging

    setup_logging(log_level="INFO", log_format="json")

    stdlib_logger = logging.getLogger("test.stdlib")
    stdlib_logger.error("leaked token bot123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")

    out = capsys.readouterr().out.strip().splitlines()
    assert out, "expected at least one log line on stdout"
    line = out[-1]
    data = json.loads(line)  # must be valid JSON, not a bare string
    assert "bot***:***" in data["event"]
    assert "AAAAAAAA" not in data["event"]
    assert data["level"] == "error"


def test_noisy_stdlib_loggers_set_to_warning():
    """httpx/httpcore/aiogram.event/aiohttp.access are extremely chatty at
    INFO (one line per API call / update) and must be raised to WARNING
    regardless of the configured LOG_LEVEL."""
    from bot.main import setup_logging

    setup_logging(log_level="DEBUG", log_format="json")

    for name in ("httpx", "httpcore", "aiogram.event", "aiohttp.access"):
        assert logging.getLogger(name).level == logging.WARNING, name


def test_log_format_console_independent_of_debug_level(capsys):
    """OBS-13: LOG_FORMAT must control the renderer, not LOG_LEVEL. DEBUG
    level with LOG_FORMAT=json must still emit JSON (previous behavior
    silently switched to ConsoleRenderer whenever level==DEBUG)."""
    from bot.main import setup_logging

    setup_logging(log_level="DEBUG", log_format="json")
    structlog.get_logger("test.format").debug("hello")

    out = capsys.readouterr().out.strip().splitlines()
    assert out
    data = json.loads(out[-1])  # must not raise — must be JSON, not console-rendered
    assert data["event"] == "hello"


def test_mask_tokens_recurses_into_nested_dict_and_list():
    """SEC-05: secrets nested inside dict/list kv values (e.g. a `summary`
    dict logged wholesale) must also be masked, not just top-level strings."""
    from bot.main import _mask_tokens

    event_dict = {
        "event": "warmup_completed",
        "summary": {
            "radarr": ("error", "connect to bot123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA failed"),
            "nested_list": ["ok", "token bot987654321:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB here"],
        },
    }
    out = _mask_tokens(None, "info", event_dict)
    flat = str(out["summary"])
    assert "AAAAAAAA" not in flat
    assert "BBBBBBBB" not in flat
    assert "bot***:***" in flat


def test_mask_tokens_masks_userinfo_in_urls():
    """SEC-05: `scheme://user:pass@host` credentials (e.g. a proxy URL leaking
    into an httpx exception message) must be masked."""
    from bot.main import _mask_tokens

    event_dict = {"event": "request failed", "error": "http://alice:s3cr3t@proxy.example.com:8899/ timeout"}
    out = _mask_tokens(None, "warning", event_dict)
    assert "alice" not in out["error"]
    assert "s3cr3t" not in out["error"]
    assert "://***:***@" in out["error"]


def test_log_format_console_renders_non_json(capsys):
    from bot.main import setup_logging

    setup_logging(log_level="INFO", log_format="console")
    structlog.get_logger("test.format2").info("hello_console")

    out = capsys.readouterr().out.strip().splitlines()
    assert out
    line = out[-1]
    with pytest.raises((json.JSONDecodeError, ValueError)):
        json.loads(line)
    assert "hello_console" in line
