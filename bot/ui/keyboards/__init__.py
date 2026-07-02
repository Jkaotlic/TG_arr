"""Inline keyboard builders for Telegram bot.

This is a package split by domain (search/torrent/settings/music/emby/
trending/calendar/menu) that used to be a single ~970-line module
(``bot/ui/keyboards.py``). The public API is unchanged: ``Keyboards`` still
exposes every keyboard-building static method it always did, and
``CallbackData`` still exposes every callback-data string constant — callers
do ``from bot.ui.keyboards import Keyboards, CallbackData`` exactly as before.

See ``bot/ui/formatters/__init__.py`` for the sibling package this split
mirrors.
"""

from bot.ui.keyboards._constants import CallbackData
from bot.ui.keyboards.calendar import _CalendarKeyboards
from bot.ui.keyboards.emby import _EmbyKeyboards
from bot.ui.keyboards.menu import _MenuKeyboards
from bot.ui.keyboards.music import _MusicKeyboards
from bot.ui.keyboards.search import _SearchKeyboards
from bot.ui.keyboards.settings import _SettingsKeyboards
from bot.ui.keyboards.torrent import _TorrentKeyboards
from bot.ui.keyboards.trending import _TrendingKeyboards

__all__ = ["CallbackData", "Keyboards"]


class Keyboards(
    _MenuKeyboards,
    _SearchKeyboards,
    _TorrentKeyboards,
    _SettingsKeyboards,
    _MusicKeyboards,
    _EmbyKeyboards,
    _TrendingKeyboards,
    _CalendarKeyboards,
):
    """Inline keyboard builders.

    Domain-specific keyboards live in mixins (see search.py/torrent.py/
    settings.py/music.py/emby.py/trending.py/calendar.py/menu.py) — this
    class only composes them so every ``Keyboards.xxx(...)`` call from before
    the split keeps working unchanged.
    """
