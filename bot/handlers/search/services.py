"""Shared service wiring and cross-cutting state for the search handler package.

Holds everything the other submodules (commands/results/grab) need to reach
through the package's top-level module object — so `patch.object(bot.handlers
.search, "get_services", ...)` (and similar patches used across the test
suite) keep working exactly as they did when this was a single file. Submodules
call these via `from bot.handlers import search as _search` and `_search.xxx()`
rather than importing the name directly, so a patch on the package attribute
is observed everywhere.
"""

import asyncio

import structlog
from aiogram import Router
from aiogram.types import Message

from bot.clients.registry import get_emby, get_lidarr, get_prowlarr, get_qbittorrent, get_radarr, get_sonarr  # noqa: F401 -- get_emby re-exported for patch.object(bot.handlers.search, "get_emby", ...)
from bot.handlers.common import safe_edit
from bot.models import ContentType, User
from bot.services.add_service import AddService
from bot.services.scoring import ScoringService
from bot.services.search_service import SearchService
from bot.ui.formatters import Formatters
from bot.ui.keyboards import Keyboards

logger = structlog.get_logger()
router = Router()

# PERF-04: Singleton ScoringService shared across all requests.
# ScoringService is stateless (only holds pre-compiled weights), so one instance is safe.
_SCORING_SERVICE = ScoringService()

# RACE-01: guard against double-grab. aiogram dispatches each callback as its own
# task, so a rapid double-tap (or Confirm→Force) would run the grab twice. A
# per-user in-progress claim makes the second concurrent grab a no-op.
_grab_in_progress: set[int] = set()
_grab_guard_lock = asyncio.Lock()

MAX_QUERY_LENGTH = 200


async def _claim_grab(user_id: int) -> bool:
    """Claim the grab slot for a user. Returns False if one is already in flight."""
    async with _grab_guard_lock:
        if user_id in _grab_in_progress:
            return False
        _grab_in_progress.add(user_id)
        return True


def _release_grab(user_id: int) -> None:
    """Release a user's grab slot (safe to call even if not held)."""
    _grab_in_progress.discard(user_id)


async def get_services() -> tuple[SearchService, AddService]:
    """Get service instances using singleton clients from registry.

    LOGIC-22: used to return `ScoringService` as a third element, but no
    caller in this module ever consumed it (music.py imports the module-level
    `_SCORING_SERVICE` singleton directly instead — see below).
    """
    prowlarr = await get_prowlarr()
    radarr = await get_radarr()
    sonarr = await get_sonarr()
    qbittorrent = await get_qbittorrent()  # Returns None if not configured
    lidarr = await get_lidarr()  # Returns None if not configured

    search_service = SearchService(prowlarr, radarr, sonarr, _SCORING_SERVICE, lidarr=lidarr)
    add_service = AddService(prowlarr, radarr, sonarr, qbittorrent=qbittorrent, lidarr=lidarr)

    return search_service, add_service


async def _render_results_page(
    message: Message,
    results: list,
    page: int,
    total_pages: int,
    query: str,
    content_type: ContentType,
    per_page: int,
    db_user: User,
    settings,
) -> None:
    """LOGIC-04: shared renderer for a page of search results.

    Deduplicates the "per_page → total_pages → slice → best_result →
    show_grab_best → format + Keyboards → edit" block that used to be copied
    verbatim in process_search, handle_pagination and handle_back.

    BUG-03: swallows the harmless "message is not modified" TelegramBadRequest
    that a fast double-tap on pagination/back triggers (re-rendering identical
    text+keyboard) — without this, callback.answer() downstream never runs and
    the button spins forever.
    """
    start_idx = page * per_page
    page_results = results[start_idx:start_idx + per_page]

    best_result = results[0] if results else None
    show_grab_best = bool(
        best_result
        and best_result.calculated_score >= settings.auto_grab_score_threshold
        and db_user.preferences.auto_grab_enabled
    )

    text = Formatters.format_search_results_page(
        page_results,
        page,
        total_pages,
        query,
        content_type,
        per_page=per_page,
    )

    await safe_edit(
        message,
        text,
        reply_markup=Keyboards.search_results(
            page_results,
            page,
            total_pages,
            per_page,
            show_grab_best,
            best_result.calculated_score if best_result else 0,
        ),
        parse_mode="HTML",
    )
