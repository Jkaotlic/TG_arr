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
MENU_TORRSERVER = "▶️ Смотреть"
#: 2026-09-24 (user request «куча кнопок и все в кучу»): the reply keyboard
#: keeps four buttons; this one opens an inline panel with the rest.
MENU_MORE = "⋯ Ещё"

#: Text of the ForceReply prompt that asks for a TorrServer query. The handler
#: matches replies against this exact string, so it lives here next to the
#: button labels for the same reason they do: filter and message must not drift.
TORRSERVER_PROMPT = "▶️ Что найти в TorrServer?"

#: ForceReply prompt of "🎵 Музыка" (audit UI-13): the reply is an artist
#: search, not a text for content-type auto-detection.
MUSIC_PROMPT = "🎵 Какого артиста найти?"

# All menu button texts — used by handlers/search.py to decide that a plain
# text message is NOT a search query but a main-menu button press (routed to
# its own handler instead). Texts that left the reply keyboard (🔥 Топ, 📊 qBit,
# …) stay here: an old keyboard on a user's phone still sends them, and their
# handlers still answer — they must never turn into a search query.
MENU_BUTTONS = frozenset({
    MENU_MORE,
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
    MENU_TORRSERVER,
})
