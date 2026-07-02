"""Search and content management handlers.

Split (follow-up refactor, behavior-preserving) into:
  - services.py  — router, shared service wiring, grab-guard state, shared page renderer
  - commands.py  — /search, /movie, /series, plain-text entry points, process_search
  - results.py   — content-type selection, pagination, release selection/lookup, back/cancel/noop
  - grab.py      — grab confirmation/execution, force-grab, season-monitoring presets

Everything importable from the old single-file `bot/handlers/search.py` (router,
handlers, and the private helpers other modules/tests reach into) is re-exported
here so `from bot.handlers import search` / `from bot.handlers.search import X`
keep working unchanged.

`patch.object(bot.handlers.search, "get_services", ...)` (and similarly for
get_emby/grab_release/_execute_grab/_decide_monitor_type/etc.) must keep
working post-split. Submodules never do `from .services import get_services`
and call it directly — they do `from bot.handlers import search as _search`
and call `_search.get_services()`, so a patch on this package's attribute is
observed by every submodule at call time.
"""

from bot.ui.keyboards import CallbackData  # noqa: F401 -- re-exported, used by tests/callers as search.CallbackData

from .services import (  # noqa: F401
    MAX_QUERY_LENGTH,
    _SCORING_SERVICE,
    _claim_grab,
    _grab_guard_lock,
    _grab_in_progress,
    _release_grab,
    _render_results_page,
    get_emby,
    get_services,
    router,
)

# Import submodules AFTER the shared state/functions above are bound on this
# package module — each submodule does `from bot.handlers import search as
# _search` at import time, resolving against this partially-initialized
# module object in sys.modules, so it must already have get_services/
# get_emby/etc. defined by this point.
from . import grab  # noqa: E402,F401
from . import results  # noqa: E402,F401
from . import commands  # noqa: E402,F401

# grab.py and results.py hold the real implementations of these; re-export
# them here too (in addition to on their own submodules) since callers/tests
# patch and import them as `bot.handlers.search.<name>`.
from .commands import process_search  # noqa: E402,F401
from .grab import (  # noqa: E402,F401
    _decide_monitor_type,
    _execute_grab,
    _resolve_folder,
    _SEASON_PRESETS,
    grab_release,
    handle_confirm_grab,
    handle_force_grab,
    handle_grab_best,
    handle_season_back,
    handle_season_menu,
    handle_season_preset,
)
from .results import (  # noqa: E402,F401
    _emby_library_note,
    _pick_by_year,
    _resolve_movie,
    _resolve_series,
    handle_back,
    handle_cancel,
    handle_legacy_page,
    handle_noop,
    handle_pagination,
    handle_release_selection,
    handle_type_selection,
)
from .commands import (  # noqa: E402,F401
    cmd_movie,
    cmd_search,
    cmd_series,
    handle_menu_search,
    handle_text_search,
)
