"""Single source of truth for main-menu button texts (LOGIC-07/DEAD-10).

Previously these Russian button-label strings were copy-pasted as module-level
``MENU_*`` constants in 8+ handler modules, a hand-maintained ``MENU_BUTTONS``
set in ``search.py`` (used to decide what "isn't" a text search query), and
literals inlined directly in ``Keyboards.main_menu()`` — three places that had
to be kept in sync by hand. If a button's text changed in one place but not
the others, the button would silently start being routed to text search
instead of its handler.

``Keyboards.main_menu()`` and every handler's ``F.text == MENU_*`` filter now
import from here, so the reply-keyboard layout and the filters that match its
button presses can never drift apart.
"""

MENU_SEARCH = "🔍 Поиск"
MENU_MUSIC = "🎵 Музыка"
MENU_TRENDING = "🔥 Топ"
MENU_CALENDAR = "📅 Календарь"
MENU_DOWNLOADS = "📥 Загрузки"
MENU_QSTATUS = "📊 qBit"
MENU_EMBY = "📺 Emby"
MENU_STATUS = "🔌 Статус"
MENU_SETTINGS = "⚙️ Настройки"
MENU_HISTORY = "📋 История"

# All menu button texts — used by handlers/search.py to decide that a plain
# text message is NOT a search query but a main-menu button press (routed to
# its own handler instead).
MENU_BUTTONS = frozenset({
    MENU_SEARCH,
    MENU_MUSIC,
    MENU_TRENDING,
    MENU_CALENDAR,
    MENU_DOWNLOADS,
    MENU_QSTATUS,
    MENU_EMBY,
    MENU_STATUS,
    MENU_SETTINGS,
    MENU_HISTORY,
})
