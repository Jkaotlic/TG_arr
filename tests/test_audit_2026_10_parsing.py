"""Задача 5 аудита 2026-10-30: парсер Prowlarr, скоринг, slskd, /wanted.

LOGIC-05, LOGIC-06, LOGIC-11, BUG-11, SEC-02. Заголовки в тестах взяты из
живых раздач: русские трекеры, scene-названия, разрешения вида 1920x1080.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from bot.clients.prowlarr import ProwlarrClient
from bot.clients.slskd import SlskdClient
from bot.models import ContentType, QualityInfo, SearchResult
from bot.services.scoring import ScoringService, grab_best_candidate

_PROWLARR = ProwlarrClient("http://prowlarr.local:9696", "key")


def season_episode(title: str) -> tuple[int | None, int | None]:
    return _PROWLARR._extract_season_episode(title)


def is_pack(title: str) -> bool:
    return _PROWLARR._is_season_pack(title)


# ------------------------------------------------------------------- LOGIC-05


@pytest.mark.parametrize(
    ("title", "expected", "pack"),
    [
        # Что работало и должно работать дальше.
        ("Show S01E02", (1, 2), False),
        ("Show s02", (2, None), True),
        ("Show 1x05", (1, 5), False),
        ("Show.1x05.720p", (1, 5), False),
        ("Show S01-S05", (1, None), True),
        ("Show.S02E01-08.1080p", (2, None), True),
        ("Сериал / Сезон 3", (3, None), True),
        ("Сезон 1-3", (1, None), True),
        ("Сериал 3 сезон", (3, None), True),
        ("Сериал 3-й сезон", (3, None), True),
        ("Show Season 2 Episode 4", (2, 4), False),
        ("Trailer.Park.Boys.S01.1080p", (1, None), True),
        # Форма «N сезон M серия»: сезон стоит перед словом, а не после.
        ("Сериал 3 сезон 5 серия", (3, 5), False),
        ("Сериал 3-й сезон 5-я серия", (3, 5), False),
        ("Сериал / Сезон 3 / Серия 5", (3, 5), False),
        ("Сериал Сезон 3 Серия 5", (3, 5), False),
        ("Сериал 5 серия 3 сезон", (3, 5), False),
        # Звук и разрешение не сезон и не серия.
        ("Rogue One 2016 1080p BluRay DTS5.1", (None, None), False),
        ("Movie.2021.2160p.UHD.BluRay.TrueHD.Atmos7.1-GRP", (None, None), False),
        ("Movie 2021 DDP5.1 Atmos7.1", (None, None), False),
        ("Сериал / Сезон 3 [2016, 1920x1080]", (3, None), True),
        ("Show 1280x720", (None, None), False),
        ("Show 720x576 PAL", (None, None), False),
        ("Movie 2021 3840x2160", (None, None), False),
        # Разрешение после слова «сезон» не номер сезона.
        ("Сериал сезон 1080p", (None, None), False),
        ("Сериал 3 сезон 1080p", (3, None), True),
        ("Show Season 1080p", (None, None), False),
        # Год перед словом «сезон» не номер сезона.
        ("Сериал 2016 сезон", (None, None), False),
        ("Сериал (2016) 2 сезон", (2, None), True),
        # Настоящая серия в формате 1x05 рядом с разрешением остаётся серией.
        ("Show 1x05 1920x1080", (1, 5), False),
        ("Show S01E02 DTS5.1 Atmos7.1", (1, 2), False),
    ],
)
def test_season_and_episode_from_real_titles(title, expected, pack):
    assert season_episode(title) == expected
    assert is_pack(title) is pack


def test_new_russian_guards_stay_linear():
    """SEC-05 не откатан: длинные пробельные хвосты не держат event loop."""
    for title in (
        "3" + " " * 1900 + "сезон",
        "сезон 3" + " " * 1900 + "серия",
        "5" + " " * 1900 + "серия 3 сезон",
        "сезон " + " " * 1900 + "x" + "1" * 50,
    ):
        started = time.perf_counter()
        season_episode(title)
        is_pack(title)
        assert time.perf_counter() - started < 0.5


# ------------------------------------------------------------------- LOGIC-11


def _release(title="Movie.2021.1080p.WEB-DL", *, seeders=100, size_gb=5.0, origin="prowlarr", **kw):
    return SearchResult(
        guid=title,
        title=title,
        indexer="i",
        size=int(size_gb * 1024**3),
        seeders=seeders,
        origin=origin,
        quality=QualityInfo(resolution="1080p", source="WEB-DL"),
        **kw,
    )


@pytest.mark.parametrize("seeders", [0])
def test_zero_seeders_never_pass_the_best_button(seeders):
    dead = _release(seeders=seeders)
    dead.calculated_score = 99  # выше любого порога

    assert grab_best_candidate([dead], auto_grab_enabled=True, threshold=80) is None


def test_seeded_and_unknown_seeders_still_pass():
    alive = _release(seeders=5)
    alive.calculated_score = 99
    unknown = _release(seeders=None)
    unknown.calculated_score = 99

    assert grab_best_candidate([alive], auto_grab_enabled=True, threshold=80) is alive
    assert grab_best_candidate([unknown], auto_grab_enabled=True, threshold=80) is unknown


def test_arr_verdict_stays_authoritative_for_usenet_with_zero_seeders():
    """У usenet-раздач *arr сидов нет вовсе (0 по умолчанию): вердикт *arr решает."""
    nzb = _release(seeders=0, origin="arr", protocol="usenet", custom_format_score=10)

    assert grab_best_candidate([nzb], auto_grab_enabled=True, threshold=80) is nzb


def _score(title: str, content_type=ContentType.MOVIE, **kw) -> int:
    return ScoringService().calculate_score(_release(title, **kw), content_type)


def test_title_beginning_with_trailer_is_not_penalised():
    assert _score("Trailer.Park.Boys.S01.1080p.WEB-DL") == _score("Park.Boys.S01.1080p.WEB-DL")
    assert _score("Trailer Park Boys S01E01 1080p WEB-DL") == _score("Park Boys S01E01 1080p WEB-DL")
    assert _score("The Trailer Park Boys Movie 2006 1080p") == _score("The Park Boys Movie 2006 1080p")


@pytest.mark.parametrize(
    "marked",
    [
        "Dune.2021.Trailer.1080p.WEB-DL",
        "Dune (2021) Official Trailer",
        "Dune 2021 Trailer",
        "Dune [Trailer] 1080p",
        "Dune.2021.HD.Trailer",
        "Dune 2021 Trailer HD",
        "Movie.CAM.SAMPLE.TRAILER",
        "Dune.2021.TRAILER-GRP",
    ],
)
def test_real_trailer_markers_are_still_penalised(marked):
    clean = marked.lower().replace("trailer", "x").replace("official ", "")
    assert _score(marked) < _score("Dune.2021.1080p.WEB-DL") - 100, (marked, clean)


def test_sample_and_teaser_penalties_untouched():
    base = _score("Dune.2021.1080p.WEB-DL")
    assert _score("Dune.2021.SAMPLE.1080p.WEB-DL") < base - 100
    assert _score("Dune.2021.Teaser.1080p.WEB-DL") < base - 100


def test_anime_gets_series_size_thresholds():
    service = ScoringService()
    # Серия на 400 МБ: для фильма это «подозрительно мало», для сериала нормально.
    episode = _release("Anime.S01E05.1080p", size_gb=0.4)
    assert service.calculate_score(episode, ContentType.ANIME) == service.calculate_score(episode, ContentType.SERIES)
    assert service.calculate_score(episode, ContentType.ANIME) > service.calculate_score(episode, ContentType.MOVIE)

    # Сезонный пак на 120 ГБ: для фильма «слишком много», для сериала нормально.
    pack = _release("Anime.S01.1080p", size_gb=120.0, is_season_pack=True)
    assert service.calculate_score(pack, ContentType.ANIME) == service.calculate_score(pack, ContentType.SERIES)
    assert service.calculate_score(pack, ContentType.ANIME) > service.calculate_score(pack, ContentType.MOVIE)


# --------------------------------------------------------------------- SEC-02


def _slskd_with(seen: list[httpx.Request]) -> SlskdClient:
    client = SlskdClient("http://slskd.example.com:5030", "key", search_timeout=0.1)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    client._client = httpx.AsyncClient(
        base_url=client.base_url, transport=httpx.MockTransport(handler), timeout=httpx.Timeout(5.0),
    )
    return client


@pytest.mark.parametrize(
    ("username", "segment"),
    [
        ("a/../../x", b"a%2F..%2F..%2Fx"),
        ("a?b", b"a%3Fb"),
        ("a#b", b"a%23b"),
        ("a b", b"a%20b"),
        ("100%", b"100%25"),
        ("a\\b", b"a%5Cb"),
        ("Иван", b"%D0%98%D0%B2%D0%B0%D0%BD"),
        ("plain_user-1", b"plain_user-1"),
    ],
)
async def test_username_is_one_path_segment(username, segment):
    from bot.models import SlskdFile

    seen: list[httpx.Request] = []
    client = _slskd_with(seen)
    try:
        ok = await client.enqueue(
            username, [SlskdFile(filename="a\\b\\01.flac", name="01.flac", size=1, extension="flac")],
        )
    finally:
        assert client._client is not None
        await client._client.aclose()

    assert ok is True
    assert len(seen) == 1
    request = seen[0]
    # Что реально уйдёт на провод: один сегмент, без выхода из /transfers/downloads/.
    assert request.url.raw_path == b"/api/v0/transfers/downloads/" + segment
    assert request.url.query == b""
    assert request.url.fragment == ""
    assert request.method == "POST"


# --------------------------------------------------------------------- LOGIC-06


def _file(path: str, size: int = 30_000_000) -> dict:
    return {"filename": path, "size": size, "extension": "flac"}


def _flatten(responses: list[dict]):
    return SlskdClient("http://slskd.example.com:5030", "key")._flatten_responses(responses)


def test_same_folder_name_under_different_artists_is_not_merged():
    responses = [{
        "username": "peer", "hasFreeUploadSlot": True, "queueLength": 0, "uploadSpeed": 1,
        "files": [
            _file("Music\\Queen\\Greatest Hits\\01 Bohemian Rhapsody.flac"),
            _file("Music\\Queen\\Greatest Hits\\02 Another One Bites the Dust.flac"),
            _file("Music\\ABBA\\Greatest Hits\\01 Waterloo.flac"),
            _file("Music\\ABBA\\Greatest Hits\\02 Fernando.flac"),
        ],
    }]

    results = _flatten(responses)

    assert len(results) == 2
    by_first = {r.files[0].name: r for r in results}
    assert {len(r.files) for r in results} == {2}
    queen = by_first["01 Bohemian Rhapsody.flac"]
    assert all("Queen" in f.filename for f in queen.files)
    assert queen.folder == "Greatest Hits"


def test_one_album_folder_stays_one_candidate():
    responses = [{
        "username": "peer",
        "files": [_file(f"Music\\Queen\\Greatest Hits\\{n:02d} Track.flac") for n in range(1, 4)],
    }]

    results = _flatten(responses)

    assert len(results) == 1
    assert len(results[0].files) == 3


def test_same_path_from_different_peers_stays_separate():
    path = "Music\\Queen\\Greatest Hits\\01 Track.flac"
    results = _flatten([
        {"username": "one", "files": [_file(path)]},
        {"username": "two", "files": [_file(path)]},
    ])

    assert sorted(r.username for r in results) == ["one", "two"]


def test_slash_and_backslash_paths_of_one_folder_are_one_candidate():
    results = _flatten([{
        "username": "peer",
        "files": [
            _file("Music\\Queen\\Greatest Hits\\01 A.flac"),
            _file("Music/Queen/Greatest Hits/02 B.flac"),
        ],
    }])

    assert len(results) == 1


def test_disc_subfolders_of_one_album_become_one_candidate():
    """«Album\\CD1» и «Album\\CD2» — один альбом: раньше они были двумя кандидатами
    «CD1» и «CD2», каждый выглядел неполным, а название кандидата ничего не говорило."""
    results = _flatten([{
        "username": "peer",
        "files": [
            _file("Music\\Pink Floyd\\The Wall\\CD1\\01 In the Flesh.flac"),
            _file("Music\\Pink Floyd\\The Wall\\CD1\\02 The Thin Ice.flac"),
            _file("Music\\Pink Floyd\\The Wall\\CD2\\01 Hey You.flac"),
            _file("Music\\Pink Floyd\\The Wall\\Disc 2\\02 Comfortably Numb.flac"),
        ],
    }])

    assert len(results) == 1
    assert results[0].folder == "The Wall"
    assert len(results[0].files) == 4


def test_two_disc_albums_of_different_artists_do_not_mix():
    results = _flatten([{
        "username": "peer",
        "files": [
            _file("Music\\A\\Live\\CD1\\01 x.flac"),
            _file("Music\\B\\Live\\CD1\\01 y.flac"),
        ],
    }])

    assert len(results) == 2


def test_disc_folder_does_not_swallow_a_share_root_file():
    """«CD1» прямо в корне шары: родителя нет, остаётся как есть."""
    results = _flatten([{"username": "peer", "files": [_file("CD1\\01 x.flac")]}])

    assert len(results) == 1
    assert results[0].folder == "CD1"


def test_music_notify_collapses_discs_the_same_way():
    """Партия уведомлений ключуется на папку альбома и сама схлопывает диски:
    группировка поиска и уведомления не должны расходиться в том, что такое диск."""
    from bot.clients.slskd import _DISC_DIR as search_disc
    from bot.services.music_notify import _DISC_DIR as notify_disc

    for name in ("CD1", "CD 2", "Disc 1", "disk-3", "Диск 2", "CD1 [FLAC]", "The Wall", "Discography", "CDs"):
        assert bool(search_disc.match(name)) == bool(notify_disc.match(name)), name


# --------------------------------------------------------------------- BUG-11


def _arr_wired(client, payload: dict, seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=payload)

    client._client = httpx.AsyncClient(
        base_url=client.base_url, transport=httpx.MockTransport(handler), timeout=httpx.Timeout(5.0),
    )
    return client


async def test_wanted_clients_carry_the_true_total():
    from bot.clients.radarr import RadarrClient
    from bot.clients.sonarr import SonarrClient

    seen: list[httpx.Request] = []
    payload = {"page": 1, "pageSize": 200, "totalRecords": 450, "records": [{"id": 1}, {"id": 2}]}
    radarr = _arr_wired(RadarrClient("http://radarr.example.com", "k"), payload, seen)
    sonarr = _arr_wired(SonarrClient("http://sonarr.example.com", "k"), payload, seen)
    try:
        movies = await radarr.get_wanted_movies(page_size=200)
        episodes = await sonarr.get_wanted_episodes(page_size=200)
    finally:
        await radarr._client.aclose()
        await sonarr._client.aclose()

    assert list(movies) == [{"id": 1}, {"id": 2}]
    assert movies.total_records == 450
    assert episodes.total_records == 450


async def test_wanted_total_falls_back_to_the_page_when_arr_omits_it():
    from bot.clients.radarr import RadarrClient

    seen: list[httpx.Request] = []
    radarr = _arr_wired(RadarrClient("http://radarr.example.com", "k"), {"records": [{"id": 1}]}, seen)
    try:
        movies = await radarr.get_wanted_movies()
    finally:
        await radarr._client.aclose()

    assert movies.total_records == 1


async def _run_wanted(movies, episodes) -> str:
    from bot.handlers import status as status_handler

    radarr = AsyncMock()
    radarr.get_wanted_movies = AsyncMock(return_value=movies)
    sonarr = AsyncMock()
    sonarr.get_wanted_episodes = AsyncMock(return_value=episodes)
    message = MagicMock()
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=status_msg)

    with patch.object(status_handler, "get_radarr", AsyncMock(return_value=radarr)), \
         patch.object(status_handler, "get_sonarr", AsyncMock(return_value=sonarr)):
        await status_handler.cmd_wanted(message)
    return status_msg.edit_text.await_args.args[0]


def _wanted(records: list[dict], total: int):
    from bot.clients.base import WantedList

    return WantedList(records, total_records=total)


async def test_wanted_shows_the_true_total_not_the_page_size():
    movies = _wanted([{"id": i, "title": f"Movie {i}", "year": 2020} for i in range(200)], 480)
    episodes = _wanted(
        [{"id": i, "title": "E", "seasonNumber": 1, "series": {"title": "Show"}} for i in range(200)], 1500,
    )

    text = await _run_wanted(movies, episodes)

    assert "Ищется: 1980 позиций" in text
    assert "Фильмы (480)" in text
    assert "и ещё 465 фильмов" in text  # 480 - 15 показанных
    assert "1500 эп." in text
    assert "очередь большая" in text.lower()


async def test_wanted_small_queue_is_unchanged():
    movies = _wanted([{"id": 1, "title": "Only", "year": 2024}], 1)
    episodes = _wanted(
        [{"id": 2, "title": "E", "seasonNumber": 1, "series": {"title": "Paw Patrol"}},
         {"id": 3, "title": "E", "seasonNumber": 2, "series": {"title": "Paw Patrol"}}], 2,
    )

    text = await _run_wanted(movies, episodes)

    assert "Ищется: 3 позиций" in text
    assert "Фильмы (1)" in text
    assert "Сериалы (1 тайтлов, 2 эп.)" in text


async def test_wanted_plain_lists_still_work():
    """Мок или старый вызывающий вернул обычный список: итог равен его длине."""
    text = await _run_wanted([{"id": 1, "title": "Only", "year": 2024}], [])

    assert "Ищется: 1 позиций" in text
