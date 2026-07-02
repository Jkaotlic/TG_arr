"""Calendar/schedule formatting helpers (date-key extraction and human
readable relative-day headers).

Note: `Formatters.format_calendar` itself stays in `bot/ui/formatters/__init__.py`
rather than here, because it calls `datetime.now(tz)` directly and
`tests/test_formatters.py::test_calendar_today_uses_local_timezone_not_utc`
monkeypatches the `datetime` name on the `bot.ui.formatters` module object
(`import bot.ui.formatters as formatters_mod; monkeypatch.setattr(formatters_mod,
"datetime", ...)`). Moving that call into a submodule would silently break the
patch, since it would resolve `datetime` from this module's globals instead.
"""

from datetime import datetime

from bot.ui.formatters._common import _to_local


def _extract_date_key(date_str: str) -> str:
    """Extract sortable date key (YYYY-MM-DD) from ISO date string.

    BUG-11: parse as tz-aware and convert to the configured TIMEZONE
    so the *local* calendar day is used for grouping.
    """
    if not date_str:
        return "9999-12-31"
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        local = _to_local(dt)
        return local.strftime("%Y-%m-%d")
    except (ValueError, IndexError):
        return date_str[:10] if len(date_str) >= 10 else "9999-12-31"


def _format_date_header(date_key: str, today) -> str:
    """Format date key to human-readable header with relative day marker."""
    months = [
        "", "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    ]
    try:
        from datetime import date as date_cls
        parts = date_key.split("-")
        dt_date = date_cls(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, IndexError):
        return date_key

    diff = (dt_date - today).days
    day_month = f"{dt_date.day} {months[dt_date.month]}"

    if diff == 0:
        return f"{day_month} — сегодня"
    elif diff == 1:
        return f"{day_month} — завтра"
    elif diff == 2:
        return f"{day_month} — послезавтра"
    elif diff == -1:
        return f"{day_month} — вчера"
    elif diff < -1:
        return f"{day_month} ({-diff} дн. назад)"
    else:
        return f"{day_month} (через {diff} дн.)"
