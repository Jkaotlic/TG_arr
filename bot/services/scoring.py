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

    # Audio bonuses
    audio_atmos: int = 10
    audio_truehd: int = 8
    audio_dtshd: int = 7
    audio_dts: int = 5
    audio_dd51: int = 3

    # Subtitle bonuses
    russian_subtitle_bonus: int = 15

    # DEAD-06: bonus when a release's resolution matches the user's
    # preferred_resolution setting (previously collected but never consumed).
    preferred_resolution_bonus: int = 15

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
    bad_keywords: Optional[dict[str, int]] = None

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

        # Pre-compile bad-keyword regex patterns (PERF-05), IGNORECASE.
        #
        # LOGIC-03: language tags (FRENCH, ITA, KOREAN, ...) are scene markers
        # like "Movie.2021.FRENCH.1080p". A plain \bword\b also matches legit
        # titles ("The French Dispatch", "The Italian Job") and wrongly penalises
        # them. For language keywords, require a scene separator (. - _) right
        # before the token so only release tags — not title words — are hit.
        language_kw = {"ita", "french", "spanish", "german", "hindi", "korean", "chinese"}
        patterns: list[tuple[re.Pattern[str], int]] = []
        for kw, penalty in self.bad_keywords.items():
            esc = re.escape(kw)
            if kw in language_kw:
                pat = re.compile(rf"(?<=[.\-_]){esc}\b", re.IGNORECASE)
            else:
                pat = re.compile(rf"\b{esc}\b", re.IGNORECASE)
            patterns.append((pat, penalty))
        self._bad_keyword_patterns = patterns


class ScoringService:
    """Service for calculating release scores."""

    def __init__(self, weights: Optional[ScoringWeights] = None):
        self.weights = weights or ScoringWeights()

    def calculate_score(
        self,
        result: SearchResult,
        content_type: ContentType = ContentType.UNKNOWN,
        preferred_resolution: Optional[str] = None,
    ) -> int:
        """
        Calculate a quality score for a search result.

        Args:
            result: SearchResult to score
            content_type: Type of content (affects size penalties)
            preferred_resolution: DEAD-06 — user's "Качество" setting
                (e.g. "1080p"). When the release's resolution matches, it gets
                a bonus on top of the base resolution scoring, so equally-good
                releases at the preferred resolution rank above others.

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

            # DEAD-06: user preference bonus — "Качество" setting was collected
            # but never fed into scoring; the pipeline now honours it.
            if preferred_resolution and quality.resolution == preferred_resolution:
                score += self.weights.preferred_resolution_bonus

        # Source scoring — LOGIC-01: REMUX is independent of whether a source
        # token was parsed (a "Title.2160p.REMUX" with no BluRay/WEB token still
        # deserves the remux bonus), so check it before the source ladder.
        if quality.is_remux:
            score += self.weights.source_remux
        elif quality.source:
            source = quality.source.lower()
            if "bluray" in source:
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

        # HDR scoring (mutually exclusive)
        if quality.hdr:
            hdr = quality.hdr.lower()
            if "dv" in hdr or "dolby vision" in hdr:
                score += self.weights.hdr_dolby_vision
            elif "hdr10+" in hdr:
                score += self.weights.hdr_hdr10plus
            elif "hdr10" in hdr or "hdr" in hdr:
                score += self.weights.hdr_hdr10

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

        # Bad keywords penalties — use pre-compiled patterns (PERF-05)
        title = result.title
        for pattern, penalty in self.weights._bad_keyword_patterns:
            if pattern.search(title):
                score += penalty

        # Ensure score stays within reasonable bounds
        return max(-100, min(150, score))

    def sort_results(
        self,
        results: list[SearchResult],
        content_type: ContentType = ContentType.UNKNOWN,
        preferred_resolution: Optional[str] = None,
    ) -> list[SearchResult]:
        """
        Sort results by calculated score (descending).

        Args:
            results: List of SearchResult objects
            content_type: Type of content for scoring adjustments
            preferred_resolution: DEAD-06 — user's "Качество" setting, passed
                through to calculate_score for the resolution-match bonus.

        Returns:
            Sorted list with calculated_score populated

        PERF-08: scores are written in-place (`r.calculated_score = score`)
        instead of `model_copy`-ing every ~25-field result just to attach a
        score — ~100 pydantic copies per search, for nothing the caller needed.
        """
        for r in results:
            r.calculated_score = self.calculate_score(r, content_type, preferred_resolution)
        results.sort(key=lambda x: x.calculated_score, reverse=True)
        return results

    # DEAD-06: get_best_result / filter_by_quality removed — no production
    # caller (only their own tests exercised them). The one thing they were
    # "on the way to" — preferred_resolution actually affecting ranking — is
    # now handled properly inside calculate_score/sort_results above.
