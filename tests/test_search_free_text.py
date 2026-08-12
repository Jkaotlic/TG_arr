"""SearchService.search_free_text — раздачи для тайтла, которого нет в каталоге.

`ProwlarrClient.search()` существовал без единого производственного вызова —
см. «KNOWN GAP» в докстринге bot/services/add_service.py. Это его вызов, и он
же оживляет push-цепочку граба.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.clients.base import ServiceConnectionError
from bot.models import ContentType, SearchResult
from bot.services.search_service import SearchService


def _result(title: str, seeders: int = 10) -> SearchResult:
    return SearchResult(
        guid=f"guid-{title}",
        origin="prowlarr",
        indexer="RuTracker",
        indexer_id=2,
        title=title,
        size=1_000_000,
        seeders=seeders,
    )


def _service(prowlarr=None) -> SearchService:
    return SearchService(MagicMock(), MagicMock(), prowlarr=prowlarr)


@pytest.mark.asyncio
async def test_calls_prowlarr_and_returns_results():
    prowlarr = MagicMock()
    prowlarr.search = AsyncMock(return_value=[_result("A"), _result("B")])
    service = _service(prowlarr)

    results = await service.search_free_text("дюна")

    prowlarr.search.assert_awaited_once_with("дюна", ContentType.UNKNOWN)
    assert {r.title for r in results} == {"A", "B"}


@pytest.mark.asyncio
async def test_scores_are_written_not_just_used_for_sorting():
    """Урок cbfe0ae: calculate_score как ключ сортировки без присваивания
    оставлял пользователю «Оценка: 0/100»."""
    prowlarr = MagicMock()
    prowlarr.search = AsyncMock(return_value=[_result("Dune 2021 1080p WEB-DL")])
    service = _service(prowlarr)

    results = await service.search_free_text("dune")

    assert results[0].calculated_score > 0


@pytest.mark.asyncio
async def test_no_prowlarr_is_a_clear_error_not_an_empty_list():
    """Пустой список читался бы как «ничего не нашлось», а это «поиск не
    настроен» — разные вещи и разные действия пользователя."""
    service = _service(None)

    with pytest.raises(ServiceConnectionError):
        await service.search_free_text("дюна")


@pytest.mark.asyncio
async def test_empty_result_set_is_returned_as_is():
    prowlarr = MagicMock()
    prowlarr.search = AsyncMock(return_value=[])
    service = _service(prowlarr)

    assert await service.search_free_text("ничего") == []


@pytest.mark.asyncio
async def test_content_type_is_passed_through():
    prowlarr = MagicMock()
    prowlarr.search = AsyncMock(return_value=[])
    service = _service(prowlarr)

    await service.search_free_text("дюна", ContentType.MOVIE)

    prowlarr.search.assert_awaited_once_with("дюна", ContentType.MOVIE)
