"""Release scoring service."""

import re
from dataclasses import dataclass
from typing import Optional

from bot.models import ContentType, SearchResult


@dataclass
class ScoringWeights:
    """Configurable scoring weights."""

    # Resolution bonuses
    resolution_2160p: int = 25
    resolution_1080p: int = 20
    resolution_720p: int = 10
    resolution_480p: int = 0

    # Source bonuses
    source_remux: int = 30
    source_bluray: int = 20
    source_webdl: int = 15
    source_webrip: int = 10
    source_hdtv: int = 5
    source_dvdrip: int = 0

    # Source penalties
    source_cam: int = -50
    source_ts: int = -40
    source_tc: int = -30

    # Codec bonuses
    codec_x265: int = 10
    codec_av1: int = 15
    codec_x264: int = 5

    # HDR bonuses
    hdr_dolby_vision: int = 15
    hdr_hdr10plus: int = 12
    hdr_hdr10: int = 10
    hdr_hdr: int = 8

    # Audio bonuses
    audio_atmos: int = 10
    audio_truehd: int = 8
    audio_dtshd: int = 7
    audio_dts: int = 5
    audio_dd51: int = 3

    # Subtitle bonuses
    russian_subtitle_bonus: int = 15

    # Repack/Proper bonuses
    repack_bonus: int = 5
    proper_bonus: int = 5

    # Seeder bonuses (per 10 seeders, capped)
    seeder_bonus_per_10: int = 2
    seeder_bonus_cap: int = 20

    # Size penalties
    size_too_small_gb: float = 1.0  # Below this is suspicious for movies
    size_too_small_penalty: int = -20
    size_too_large_gb: float = 80.0  # Above this might be problematic
    size_too_large_penalty: int = -10

    # Bad keywords penalties
    bad_keywords: dict[str, int] = None

    def __post_init__(self):
        if self.bad_keywords is None:
            self.bad_keywords = {
                "sample": -200,
                "trailer": -200,
                "teaser": -200,
                "screener": -30,
                "workprint": -40,
                "r5": -20,
                "hardcoded": -10,
                "hc": -10,
                "korsub": -10,
                "dubbed": -5,
                "ita": -3,
                "french": -3,
                "spanish": -3,
                "german": -3,
                "hindi": -3,
                "korean": -3,
                "chinese": -3,
            }


class ScoringService:
    """Service for calculating release scores."""

    def __init__(self, weights: Optional[ScoringWeights] = None):
        self.weights = weights or ScoringWeights()

    def calculate_score(self, result: SearchResult, content_type: ContentType = ContentType.UNKNOWN) -> int:
        """
        Calculate a quality score for a search result.

        Args:
            result: SearchResult to score
            content_type: Type of content (affects size penalties)

        Returns:
            Calculated score (0-100 base, can go higher or negative)
        """
        score = 50  # Base score

        # Add Prowlarr score if available (normalized to our scale)
        if result.prowlarr_score is not None:
            prowlarr_contribution = min(result.prowlarr_score // 100, 20)
            score += prowlarr_contribution

        quality = result.quality

        # Resolution scoring
        if quality.resolution:
            if quality.resolution == "2160p":
                score += self.weights.resolution_2160p
            elif quality.resolution == "1080p":
                score += self.weights.resolution_1080p
            elif quality.resolution == "720p":
                score += self.weights.resolution_720p
            elif quality.resolution == "480p":
                score += self.weights.resolution_480p

        # Source scoring
        if quality.source:
            source = quality.source.lower()
            if quality.is_remux:
                score += self.weights.source_remux
            elif "bluray" in source:
                score += self.weights.source_bluray
            elif "web-dl" in source or "webdl" in source:
                score += self.weights.source_webdl
            elif "webrip" in source:
                score += self.weights.source_webrip
            elif "hdtv" in source:
                score += self.weights.source_hdtv
            elif "dvdrip" in source:
                score += self.weights.source_dvdrip
            elif "cam" in source:
                score += self.weights.source_cam
            elif source in ("ts", "telesync"):
                score += self.weights.source_ts
            elif source in ("tc", "telecine"):
                score += self.weights.source_tc

        # Codec scoring
        if quality.codec:
            codec = quality.codec.lower()
            if "x265" in codec or "hevc" in codec:
                score += self.weights.codec_x265
            elif "av1" in codec:
                score += self.weights.codec_av1
            elif "x264" in codec:
                score += self.weights.codec_x264

        # HDR scoring
        if quality.hdr:
            hdr = quality.hdr.lower()
            if "dv" in hdr or "dolby" in hdr:
                score += self.weights.hdr_dolby_vision
            if "hdr10+" in hdr:
                score += self.weights.hdr_hdr10plus
            elif "hdr10" in hdr:
                score += self.weights.hdr_hdr10
            elif "hdr" in hdr:
                score += self.weights.hdr_hdr

        # Audio scoring
        if quality.audio:
            audio = quality.audio.lower()
            if "atmos" in audio:
                score += self.weights.audio_atmos
            elif "truehd" in audio:
                score += self.weights.audio_truehd
            elif "dts-hd" in audio or "dtshd" in audio:
                score += self.weights.audio_dtshd
            elif "dts" in audio:
                score += self.weights.audio_dts
            elif "dd5.1" in audio or "dd 5.1" in audio:
                score += self.weights.audio_dd51

        # Repack/Proper bonuses
        if quality.is_repack:
            score += self.weights.repack_bonus
        if quality.is_proper:
            score += self.weights.proper_bonus

        # Russian subtitle/audio bonus
        if quality.subtitle:
            score += self.weights.russian_subtitle_bonus

        # Seeder bonus
        if result.seeders is not None and result.seeders > 0:
            seeder_bonus = min(
                (result.seeders // 10) * self.weights.seeder_bonus_per_10,
                self.weights.seeder_bonus_cap
            )
            score += seeder_bonus

        # Size penalties
        size_gb = result.get_size_gb()
        if size_gb > 0:
            # Adjust thresholds based on content type
            min_size = self.weights.size_too_small_gb
            max_size = self.weights.size_too_large_gb

            if content_type == ContentType.SERIES:
                # Series episodes are typically smaller
                if result.is_season_pack:
                    min_size = 2.0  # Season pack should be at least 2GB
                    max_size = 200.0  # Season pack can be large
                else:
                    min_size = 0.2  # Single episode minimum
                    max_size = 10.0  # Single episode max

            if size_gb < min_size:
                score += self.weights.size_too_small_penalty
            elif size_gb > max_size:
                score += self.weights.size_too_large_penalty

        # Bad keywords penalties
        title_lower = result.title.lower()
        for keyword, penalty in self.weights.bad_keywords.items():
            # Use word boundary matching to avoid false positives
            pattern = rf"\b{re.escape(keyword)}\b"
            if re.search(pattern, title_lower):
                score += penalty

        # Ensure score stays within reasonable bounds
        return max(-100, min(150, score))

    def sort_results(
        self,
        results: list[SearchResult],
        content_type: ContentType = ContentType.UNKNOWN,
    ) -> list[SearchResult]:
        """
        Sort results by calculated score (descending).

        Args:
            results: List of SearchResult objects
            content_type: Type of content for scoring adjustments

        Returns:
            Sorted list with calculated_score populated
        """
        for result in results:
            result.calculated_score = self.calculate_score(result, content_type)

        return sorted(results, key=lambda x: x.calculated_score, reverse=True)

    def get_best_result(
        self,
        results: list[SearchResult],
        content_type: ContentType = ContentType.UNKNOWN,
        min_score: int = 0,
    ) -> Optional[SearchResult]:
        """
        Get the best result above minimum score.

        Args:
            results: List of SearchResult objects
            content_type: Type of content for scoring adjustments
            min_score: Minimum acceptable score

        Returns:
            Best result or None if none meet criteria
        """
        if not results:
            return None

        sorted_results = self.sort_results(results, content_type)
        best = sorted_results[0]

        if best.calculated_score >= min_score:
            return best

        return None

    def filter_by_quality(
        self,
        results: list[SearchResult],
        preferred_resolution: Optional[str] = None,
        min_seeders: int = 0,
        exclude_cam_ts: bool = True,
    ) -> list[SearchResult]:
        """
        Filter results by quality criteria.

        Args:
            results: List of SearchResult objects
            preferred_resolution: Preferred resolution (e.g., "1080p")
            min_seeders: Minimum number of seeders
            exclude_cam_ts: Exclude CAM and TS releases

        Returns:
            Filtered list of results
        """
        filtered = []

        for result in results:
            # Check seeders
            if result.seeders is not None and result.seeders < min_seeders:
                continue

            # Check CAM/TS
            if exclude_cam_ts and result.quality.source:
                source = result.quality.source.lower()
                if source in ("cam", "ts", "telesync", "tc", "telecine"):
                    continue

            # Check resolution preference
            if preferred_resolution and result.quality.resolution:
                if result.quality.resolution != preferred_resolution:
                    continue

            filtered.append(result)

        return filtered
