"""Entry points for the search flow: /search, /movie, /series, plain text, and
the top-level query-processing pipeline that kicks off content-type detection
and shows the first results page."""

import html
import re
import time
from typing import Optional

import structlog
from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import Message

from bot.config import get_settings
from bot.db import Database
from bot.handlers.common import strip_command
from bot.models import ActionLog, ActionType, ContentType, SearchSession, User
from bot.services.search_service import SearchService, describe_search_failure
from bot.ui.formatters import Formatters
from bot.ui.keyboards import Keyboards
from bot.ui.menu import MENU_BUTTONS, MENU_SEARCH

from bot.handlers import search as _search
from .services import MAX_QUERY_LENGTH, router

logger = structlog.get_logger()


@router.message(Command("search"))
async def cmd_search(message: Message, db_user: User, db: Database) -> None:
    """Handle /search <query> command - auto-detect content type."""
    if not message.text:
        await message.answer("Укажите запрос: <code>/search Дюна 2021</code>")
        return

    query = strip_command(message.text, "/search")
    if not query:
        await message.answer("Укажите запрос: <code>/search Дюна 2021</code>")
        return

    await process_search(message, query, ContentType.UNKNOWN, db_user, db)


@router.message(Command("movie"))
async def cmd_movie(message: Message, db_user: User, db: Database) -> None:
    """Handle /movie <query> command."""
    if not message.text:
        await message.answer("Укажите название фильма: <code>/movie Дюна 2021</code>")
        return

    query = strip_command(message.text, "/movie")
    if not query:
        await message.answer("Укажите название фильма: <code>/movie Дюна 2021</code>")
        return

    await process_search(message, query, ContentType.MOVIE, db_user, db)


@router.message(Command("series"))
async def cmd_series(message: Message, db_user: User, db: Database) -> None:
    """Handle /series <query> command."""
    if not message.text:
        await message.answer("Укажите название сериала: <code>/series Breaking Bad</code>")
        return

    query = strip_command(message.text, "/series")
    if not query:
        await message.answer("Укажите название сериала: <code>/series Breaking Bad</code>")
        return

    await process_search(message, query, ContentType.SERIES, db_user, db)


@router.message(Command("anime"))
async def cmd_anime(message: Message, db_user: User, db: Database) -> None:
    """Handle /anime <query> — anime is its own ContentType (routes to
    Sonarr's `seriesType=anime`), not a plain flavour of series."""
    if not message.text:
        await message.answer("Укажите название аниме: <code>/anime Frieren</code>")
        return

    query = strip_command(message.text, "/anime")
    if not query:
        await message.answer("Укажите название аниме: <code>/anime Frieren</code>")
        return

    await process_search(message, query, ContentType.ANIME, db_user, db)


@router.message(F.text == MENU_SEARCH)
async def handle_menu_search(message: Message) -> None:
    """Handle search menu button."""
    settings = get_settings()
    suffix = ", сериала, аниме или артиста" if settings.music_enabled else ", сериала или аниме"
    await message.answer(f"🔍 Введите название фильма{suffix}:")


@router.message(F.text & ~F.text.startswith("/") & ~F.text.in_(MENU_BUTTONS))
async def handle_text_search(message: Message, db_user: User, db: Database) -> None:
    """Handle plain text as search query."""
    if not message.text:
        return

    await process_search(message, message.text.strip(), ContentType.UNKNOWN, db_user, db)


def _normalize_title(value: str) -> str:
    """Casefold + collapse punctuation so "Дюна:" == "дюна"."""
    return re.sub(r"[^\w\s]", "", (value or "").casefold()).strip()


def needs_title_confirmation(
    candidates: list, query: str, query_year: Optional[int]
) -> bool:
    """Whether the user must pick which title they meant.

    Prod incident 2026-07-29: "Холодное сердце" returned a German film from
    2016 first (Disney's Frozen wasn't in the metadata list at all) and the bot
    silently added it to the catalog, then searched releases for the wrong
    film. Picking the top hit is only safe when the answer is unambiguous:

    - one candidate, or
    - the query carried a year and exactly one candidate matches it, or
    - exactly one candidate's title equals the query.

    Anything else is a guess, and a wrong guess costs a junk catalog entry plus
    a pointless indexer search — so ask instead.
    """
    if len(candidates) <= 1:
        return False

    wanted = _normalize_title(query)
    # Strip a trailing year from the query before comparing titles.
    if query_year:
        wanted = _normalize_title(re.sub(rf"\b{query_year}\b", "", query))

    if query_year:
        year_matches = [
            c for c in candidates if c.year and abs(int(c.year) - int(query_year)) <= 1
        ]
        if len(year_matches) == 1:
            return False
        # Several candidates in the same year — fall through to the title check.

    exact = [c for c in candidates if _normalize_title(c.title) == wanted]
    return len(exact) != 1


def should_ask_for_season(
    content_type: ContentType, seasons: list, parsed_season: Optional[int]
) -> bool:
    """Whether to offer a season picker before searching releases.

    Only worth asking when there is a real choice: an episodic facet, more than
    one season, and a query that didn't already name one ("Breaking Bad S02"
    has answered it).
    """
    if content_type not in (ContentType.SERIES, ContentType.ANIME):
        return False
    if parsed_season is not None:
        return False
    return len(seasons) > 1


def _pick_metadata_candidate(candidates: list, query_year: Optional[int]):
    """Choose the metadata candidate the user most likely meant.

    Prefers a year match (±1) when the query carried a year — "Dune 2021" must
    not resolve to the 1984 film just because it ranks higher by popularity.
    """
    if not candidates:
        return None
    if query_year:
        for candidate in candidates:
            if candidate.year and abs(int(candidate.year) - int(query_year)) <= 1:
                return candidate
    return candidates[0]


async def _lookup_metadata_candidates(search_service, term: str, content_type: ContentType) -> list:
    """Metadata candidates for one facet.

    Replaces `SearchService.search_metadata`, which Task 9's own brief
    neither tested nor specified and is left as a `NotImplementedError` stub
    (see its docstring) — this handler no longer resolves a catalog title
    before *listing* releases (that requirement is gone with the previous
    backend), only before *adding* one.

    Fix round 1 (review finding 1): this used to call
    `search_service.radarr.lookup_movie`/`.sonarr.lookup_series` directly,
    bypassing `_lookup_branch`'s semaphore/circuit-breaker entirely — the
    exact protection Task 8 restored to stop "a burst of searches takes all
    three services down at once" (see `_lookup_branch`'s docstring). Every
    explicit `/movie`, `/series`, `/anime` search now goes through
    `SearchService.lookup_movies`/`lookup_series`, the same guarded path
    `detect_content_type`'s own fan-out uses. Series/anime reuse
    `SearchService.split_series_candidates`, the same genre-based split
    `detect_content_type` uses, so a plain "/series" search and an
    auto-detected one classify anime identically.
    """
    if content_type is ContentType.MOVIE:
        return await search_service.lookup_movies(term)
    if content_type in (ContentType.SERIES, ContentType.ANIME):
        all_series = await search_service.lookup_series(term)
        series, anime = SearchService.split_series_candidates(all_series)
        return anime if content_type is ContentType.ANIME else series
    return []


async def _resolve_arr_entry(
    add_service,
    content_type: ContentType,
    candidate,
    db_user: User,
):
    """Turn a chosen metadata candidate into a library entry with an arr_id.

    Rollback 2026-08-10 (Task 12). *arr's interactive release search needs a
    movie/series id already in Radarr/Sonarr's own library — unlike the
    previous backend, which searched releases per catalog title after an
    explicit "ensure_title" add (unmonitored, purely to get an id).
    Radarr/Sonarr have no such "list releases for something not yet added"
    mode, and adding is cheap and is what the user wants anyway if they go on
    to grab something — so this adds the title the moment a candidate is resolved
    (`search_for_movie`/`search_for_missing=False`: nothing is grabbed
    automatically, the user is about to pick one specific release), and the
    caller tells the user plainly when that happened (`created=True`).

    A candidate whose lookup already carries a `radarr_id`/`sonarr_id` is
    already in the library — reused as-is, no add call, no extra network
    round-trip.

    Returns `(title_info, arr_id, created)`; `(None, None, False)` when
    there is genuinely nothing to resolve (no candidate) or the add call
    itself failed against a *reachable, configured* Radarr/Sonarr. Raises
    `ValueError` when Radarr/Sonarr has no quality profiles or no root
    folders configured at all — that is a setup problem, not "this title
    doesn't exist", and the caller renders it as a distinct message instead
    of the generic "nothing found" (fix round 1, review finding — the two
    used to read identically to the user).

    Fix round 1 (review finding 2): `radarr_quality_profile_id`/
    `radarr_root_folder_id` and `sonarr_quality_profile_id`/
    `sonarr_root_folder_id` are separate `UserPreferences` fields, not one
    shared pair — Radarr's and Sonarr's ids are independent
    sequences (live measurement: both start at 1, 2, pointing at unrelated
    paths), so a single shared preference could silently apply a movie's
    folder/profile choice to a series.
    """
    if candidate is None:
        return None, None, False

    if content_type is ContentType.MOVIE:
        if candidate.radarr_id:
            return candidate, candidate.radarr_id, False
        client = add_service.radarr
        profiles = await client.get_quality_profiles()
        folders = await client.get_root_folders()
        if not profiles or not folders:
            logger.warning("radarr_add_blocked_no_profiles_or_folders")
            raise ValueError("В Radarr не настроены профили качества или папки — обратитесь к администратору")
        profile = add_service.resolve_profile(profiles, db_user.preferences.radarr_quality_profile_id)
        folder_path = add_service.resolve_root_folder(folders, db_user.preferences.radarr_root_folder_id)
        added, _action = await add_service.add_movie(
            candidate, quality_profile_id=profile.id, root_folder_path=folder_path,
            search_for_movie=False,
            # Added UNMONITORED: this add exists only to obtain a Radarr id so
            # the interactive search can run. `search_for_movie=False` stops the
            # immediate search, but a monitored title still joins Radarr's RSS
            # loop — so a user who merely looked at the release list and walked
            # away would get the film downloaded anyway. Monitoring is switched
            # on in `_execute_grab`, once a release is actually taken.
            monitored=False,
        )
        if added is None or not added.radarr_id:
            return None, None, False
        return added, added.radarr_id, True

    # SERIES / ANIME
    if candidate.sonarr_id:
        return candidate, candidate.sonarr_id, False
    client = add_service.sonarr
    profiles = await client.get_quality_profiles()
    folders = await client.get_root_folders()
    if not profiles or not folders:
        logger.warning("sonarr_add_blocked_no_profiles_or_folders")
        raise ValueError("В Sonarr не настроены профили качества или папки — обратитесь к администратору")
    profile = add_service.resolve_profile(profiles, db_user.preferences.sonarr_quality_profile_id)
    folder_path = add_service.resolve_root_folder(folders, db_user.preferences.sonarr_root_folder_id)
    added, _action = await add_service.add_series(
        candidate, quality_profile_id=profile.id, root_folder_path=folder_path,
        content_type=content_type, search_for_missing=False,
        # Same reasoning as the movie branch, and worse if ignored: the client
        # default is `monitor="all"`, so browsing one season of a 10-season show
        # would enlist every episode of every season into Sonarr's RSS loop.
        monitored=False, monitor="none",
    )
    if added is None or not added.sonarr_id:
        return None, None, False
    return added, added.sonarr_id, True


def _known_seasons(title, content_type: ContentType) -> list[int]:
    """Season numbers already carried by `title.seasons` (from Sonarr's own
    lookup/add response — see `SonarrClient._parse_series`), newest-first-
    friendly. No API call: Sonarr's `/series/lookup` returns the full season
    list even for a title not yet in the library, so there is nothing left to
    fetch separately (unlike the previous backend's dedicated seasons query,
    which `SearchService.get_seasons` — a `NotImplementedError` stub — used
    to serve).
    """
    if content_type not in (ContentType.SERIES, ContentType.ANIME):
        return []
    seasons = getattr(title, "seasons", None) or []
    numbers = {
        s.get("seasonNumber") for s in seasons
        if isinstance(s, dict) and isinstance(s.get("seasonNumber"), int) and s.get("seasonNumber", 0) > 0
    }
    return sorted(numbers)


async def process_search(
    message: Message,
    query: str,
    content_type: ContentType,
    db_user: User,
    db: Database,
    chosen_title=None,
    season_override: Optional[int] = None,
) -> None:
    """Process a search query.

    `chosen_title` is set when the user answered the "which title did you
    mean?" question — it skips both the metadata search and the ambiguity
    check, since the answer is now explicit. `season_override` likewise carries
    the answer to the season picker.
    """
    if len(query) > MAX_QUERY_LENGTH:
        await message.answer(f"❌ Запрос слишком длинный (макс. {MAX_QUERY_LENGTH} символов)")
        return

    if len(query) < 2:
        await message.answer("❌ Запрос слишком короткий (мин. 2 символа)")
        return

    settings = get_settings()
    search_service, add_service = await _search.get_services()

    # BUG-01: bind log BEFORE try so the except handler never sees a NameError
    # if message.answer() fails before log could be assigned inside try.
    user_id = db_user.tg_id
    log = logger.bind(user_id=user_id, query=query)
    t_start = time.monotonic()
    # LOGIC-23: tracked so the except-handler can edit the in-flight status
    # message ("Ищу релизы...") instead of leaving it hanging and sending a
    # brand-new error message underneath it.
    status_msg: Optional[Message] = None
    # LOGIC-06: only set when content_type was UNKNOWN and detection ran;
    # carries lookup_results forward into the session below so a later grab
    # doesn't repeat the same metadata lookup.
    detection = None

    try:
        parsed = search_service.parse_query(query)
        clean_title = (parsed.get("title") or "").strip()
        log.info(
            "search_started",
            parsed=parsed,
            initial_content_type=content_type.value,
        )

        # Detect content type if unknown
        if content_type == ContentType.UNKNOWN:
            status_msg = await message.answer("🔍 Определяю тип контента...")

            # Note: a season marker no longer short-circuits to SERIES here.
            # It narrows detection to series-vs-anime *inside*
            # detect_content_type, because those score separately (a genre
            # split, not separate services) — see its docstring.
            #
            # Fix (Task 12): this called the nonexistent
            # `search_service.detect_with_confidence` — a name from the
            # previous backend's era that never existed on the rewritten
            # *arr SearchService (only
            # `detect_content_type`, which already returns the same
            # confidence-carrying `DetectionResult`; see
            # tests/test_detect_content_type.py, Task 8/9's own coverage,
            # which never called it anything else). Every plain-text query
            # (`handle_text_search` — the bot's single most common entry
            # point) hit this `AttributeError` before it was caught here.
            t_detect = time.monotonic()
            detection = await search_service.detect_content_type(query)
            log.info(
                "stage_done",
                stage="detect_content_type",
                elapsed_ms=round((time.monotonic() - t_detect) * 1000, 1),
                winner=detection.content_type.value,
                confidence=round(detection.confidence, 3),
                reason=detection.reason,
            )
            content_type = detection.content_type

            # Music auto-detected → hand off to the music flow.
            if content_type == ContentType.MUSIC:
                await status_msg.delete()
                from bot.handlers.music import process_music_search

                await process_music_search(message, query, db_user, db)
                return

            # Unknown OR low/ambiguous confidence → ask the user (BUG-04, LOGIC-28).
            if content_type == ContentType.UNKNOWN:
                show_music = settings.music_enabled
                question_suffix = (
                    "фильм, сериал, аниме или музыка?" if show_music else "фильм, сериал или аниме?"
                )
                hint = ""
                if detection and detection.candidates:
                    hint_lines = []
                    for kind, label in (
                        ("movie", "🎬"), ("series", "📺"), ("anime", "🎌"), ("music", "🎵"),
                    ):
                        items = detection.candidates.get(kind) or []
                        if items:
                            shown = ", ".join(html.escape(t) for t in items[:2])
                            hint_lines.append(f"{label} {shown}")
                    if hint_lines:
                        hint = "\n\n<i>Похоже на:</i>\n" + "\n".join(hint_lines)
                await status_msg.edit_text(
                    f"🤔 <b>{html.escape(query)}</b> — это {question_suffix}{hint}",
                    reply_markup=Keyboards.content_type_selection(show_music=show_music),
                    parse_mode="HTML",
                )
                session = SearchSession(
                    user_id=user_id,
                    query=query,
                    content_type=ContentType.UNKNOWN,
                )
                await db.save_session(user_id, session)
                log.info("search_branch", branch="question_user")
                return

            await status_msg.delete()

        status_msg = await message.answer("🔍 Ищу релизы...")

        # *arr's interactive search needs a movie/series id already in
        # Radarr/Sonarr's library — resolve (or add) it before listing
        # releases. `chosen_title` (the answer to "which title did you
        # mean?", or a season-picker round trip) already names the exact
        # candidate, so it skips the metadata lookup entirely — that lookup
        # is a live TMDb/TVDB round trip through *arr (measured 2026-08-10:
        # Sonarr ~34s), and repeating it on every "which season?" tap would
        # be a multi-second regression for no reason, the candidate is
        # already known.
        lookup_term = clean_title or query
        if chosen_title is not None:
            chosen = chosen_title
        else:
            candidates = (
                detection.lookup_results
                if detection and detection.content_type == content_type and detection.lookup_results
                else await _lookup_metadata_candidates(search_service, lookup_term, content_type)
            )

            # 2026-07-29: don't guess. Adding the wrong title costs a junk
            # library entry AND an indexer search for a film the user never
            # asked for.
            if needs_title_confirmation(candidates, lookup_term, parsed.get("year")):
                session = SearchSession(
                    user_id=user_id,
                    query=query,
                    content_type=content_type,
                    lookup_candidates=list(candidates[:5]),
                )
                await db.save_session(user_id, session)
                await status_msg.edit_text(
                    f"🤔 Уточните, что именно нужно — <b>{html.escape(query)}</b>:",
                    reply_markup=Keyboards.title_candidates(candidates),
                    parse_mode="HTML",
                )
                log.info("search_branch", branch="ask_title", candidates=len(candidates))
                return

            chosen = _pick_metadata_candidate(candidates, parsed.get("year"))

        try:
            title, arr_id, created = await _resolve_arr_entry(add_service, content_type, chosen, db_user)
        except ValueError as ve:
            # Fix round 1 (review finding, Minor): a misconfigured Radarr/
            # Sonarr (no quality profiles or root folders at all) used to
            # read to the user as "your title doesn't exist" — the generic
            # "no_metadata" message below. This is a setup problem, distinct
            # from a genuine no-match, so it gets its own message.
            await status_msg.edit_text(Formatters.format_error(str(ve)))
            log.warning("search_branch", branch="add_config_error", error=str(ve))
            return
        if title is None or arr_id is None:
            # Каталог знает только то, что несут TMDb/TVDB. Тупик здесь — не
            # тупик для индексеров, и кнопка ниже единственное место, где
            # пользователь об этом узнаёт. Сессия сохраняется, чтобы она взяла
            # запрос, а не просила набрать его заново.
            await db.save_session(user_id, SearchSession(
                user_id=user_id, query=query, content_type=content_type,
            ))
            await status_msg.edit_text(
                Formatters.format_warning(f"Ничего не найдено для <b>{html.escape(query)}</b>"),
                reply_markup=Keyboards.free_search_offer(),
                parse_mode="HTML",
            )
            log.info("search_branch", branch="no_metadata")
            return

        arr_name = "Radarr" if content_type is ContentType.MOVIE else "Sonarr"
        if created:
            # Honest about what just happened: unlike the previous backend
            # (one bridge, one catalog), the title now really does exist in
            # Radarr/Sonarr's own library, not just in a bot-side session.
            add_action = ActionLog(
                user_id=user_id,
                action_type=ActionType.ADD,
                content_type=content_type,
                content_title=title.title,
                content_id=str(getattr(title, "tmdb_id", None) or getattr(title, "tvdb_id", None) or ""),
            )
            add_action.success = True
            await db.log_action(add_action)
            await status_msg.edit_text(
                f"🆕 <b>{html.escape(title.title)}</b> добавлен в {arr_name} — ищу релизы...",
                parse_mode="HTML",
            )
            log.info("title_added", title=title.title, arr_id=arr_id, content_type=content_type.value)

        # A multi-season show searched whole returns packs the user may not
        # want and spends indexer quota on episodes already on disk — offer the
        # choice while it's still cheap to make.
        seasons = _known_seasons(title, content_type)
        if should_ask_for_season(content_type, seasons, parsed.get("season")):
            session = SearchSession(
                user_id=user_id,
                query=query,
                content_type=content_type,
                selected_content=title,
            )
            await db.save_session(user_id, session)
            await status_msg.edit_text(
                f"📺 <b>{html.escape(title.title)}</b>\n\nЧто искать?",
                reply_markup=Keyboards.season_scope(seasons, str(arr_id)),
                parse_mode="HTML",
            )
            log.info("search_branch", branch="ask_season", seasons=len(seasons))
            return

        t_search = time.monotonic()
        results = await search_service.search_releases_for_title(
            content_type,
            arr_id,
            season=parsed.get("season") or season_override,
            # DEAD-06: the user's resolution preference must reach the scorer,
            # or "Качество" in /settings silently stops affecting the ranking.
            preferred_resolution=db_user.preferences.preferred_resolution,
        )
        log.info(
            "stage_done",
            stage="search_releases",
            elapsed_ms=round((time.monotonic() - t_search) * 1000, 1),
            result_count=len(results),
            arr_id=arr_id,
        )

        if not results:
            # Тот же довод, что и в ветке no_metadata: *arr спрашивает
            # индексеры по своим правилам и мог отсеять всё до показа. Прямой
            # запрос в Prowlarr — следующий разумный шаг, а не «приходите
            # завтра».
            await db.save_session(user_id, SearchSession(
                user_id=user_id, query=query, content_type=content_type,
            ))
            await status_msg.edit_text(
                Formatters.format_warning(
                    f"Релизы для <b>{html.escape(title.title)}</b> не найдены.\n\n"
                    f"Тайтл в библиотеке {arr_name} и будет доступен для "
                    "автоматического поиска по расписанию."
                ),
                reply_markup=Keyboards.free_search_offer(),
                parse_mode="HTML",
            )
            log.info("search_branch", branch="no_results")
            return

        # DB-03: search_results was a write-only table (JSON blob duplicated
        # into `sessions` right below) — never read by any handler. Dropped
        # from the hot path; `actions` (ActionType.SEARCH, logged below)
        # already covers search history.
        session = SearchSession(
            user_id=user_id,
            query=query,
            content_type=content_type,
            results=results,
            current_page=0,
            selected_content=title,
        )
        await db.save_session(user_id, session)

        per_page = settings.results_per_page
        total_pages = (len(results) + per_page - 1) // per_page

        # LOGIC-04: shared renderer (also swallows "message is not modified").
        await _search._render_results_page(
            status_msg, results, 0, total_pages, query, content_type, per_page, db_user, settings
        )

        action = ActionLog(
            user_id=user_id,
            action_type=ActionType.SEARCH,
            content_type=content_type,
            query=query,
        )
        await db.log_action(action)
        log.info(
            "search_branch",
            branch="results_shown",
            total_elapsed_ms=round((time.monotonic() - t_start) * 1000, 1),
            content_type=content_type.value,
        )

    except Exception as e:
        log.error("Search failed", error=str(e), exc_info=True)
        # LOGIC-23: edit the in-flight status message ("Ищу релизы...") rather
        # than leaving it hanging forever with a separate error message below it.
        # 2026-07-29: say *what* failed when we know — a masked internal
        # error is almost always "every indexer is rate-limited", and the user
        # can act on that (wait it out) but not on "временно недоступен".
        error_text = Formatters.format_error(html.escape(describe_search_failure(e)))
        if status_msg is not None:
            try:
                await status_msg.edit_text(error_text)
            except TelegramBadRequest:
                await message.answer(error_text)
        else:
            await message.answer(error_text)
