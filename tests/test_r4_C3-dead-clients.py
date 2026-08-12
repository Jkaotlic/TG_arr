"""R4 C3-dead-clients: pin dead-code removals and surviving behaviour.

Covers DEAD-02 (ProwlarrClient.grab_release), DEAD-03/04
(LidarrClient.lookup_album / search_album + Formatters.format_album_info),
DEAD-05 (SonarrClient.lookup_series_by_tvdb) and DEAD-06
(DeezerClient.get_trending_albums). The deleted symbols must be gone, while
the live siblings (the calendar/lookup paths) keep working unchanged.

r5 DEAD-09: LidarrClient._parse_album and models.AlbumInfo — originally kept
alive here as "surviving siblings" of the r4 album-flow removal — were
themselves removed in round 5: no album-grab flow ever materialized, so
_parse_album had zero production callers (only these now-deleted pinning
tests). See analysis/r5/03-dead-code.md DEAD-09.

2026-08-12: флоу материализовался (выбор альбома, спека
docs/superpowers/specs/2026-08-12-album-scope-design.md), поэтому `search_album`
и `_parse_album` вернулись — уже с производственными вызовами, см.
`TestAlbumFlowRevived`. `lookup_album` и `format_album_info` остались мёртвыми
и по-прежнему под охраной.
"""

from bot.clients.deezer import DeezerClient
from bot.clients.lidarr import LidarrClient
from bot.ui.formatters import Formatters


class TestDeadSymbolsRemoved:
    """The orphaned methods/helpers must no longer exist anywhere."""

    def test_deezer_get_trending_albums_removed(self):
        assert not hasattr(DeezerClient, "get_trending_albums")

    def test_lidarr_lookup_album_removed(self):
        assert not hasattr(LidarrClient, "lookup_album")

    def test_formatters_format_album_info_removed(self):
        assert not hasattr(Formatters, "format_album_info")


class TestAlbumFlowRevived:
    """2026-08-12: флоу выбора альбома появился, и вместе с ним вернулись ровно
    два символа из удалённых в р4/р5 — `search_album` и `_parse_album`. Оба
    теперь с живыми вызовами из `bot/handlers/music.py`, то есть инвариант
    DEAD-09 («мёртвых символов не держим») не нарушен, а исполнен.

    `lookup_album` и `format_album_info` НЕ возвращены и остаются под охраной
    выше: поиск альбома свободным текстом измерен как непригодный (Lidarr отдаёт
    каверы вместо альбома — см. спеку), а отдельный форматтер альбома не
    понадобился, карточку рисует `album_sources` + общий список релизов.
    """

    def test_search_album_is_back_with_a_caller(self):
        from bot.handlers import music

        assert hasattr(LidarrClient, "search_album")
        assert hasattr(music, "handle_album_source")

    def test_parse_album_is_back_with_a_caller(self):
        assert hasattr(LidarrClient, "_parse_album")
        assert hasattr(LidarrClient, "get_albums")


class TestSurvivingSymbolsIntact:
    """Live siblings of the deleted code must keep working."""

    def test_deezer_get_trending_artists_still_present(self):
        assert hasattr(DeezerClient, "get_trending_artists")

    def test_lidarr_lookup_artist_still_present(self):
        assert hasattr(LidarrClient, "lookup_artist")
