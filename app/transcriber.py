from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from youtube_transcript_api import YouTubeTranscriptApi


@dataclass
class TranscriptResult:
    status: str  # ok | bad_quality | missing
    segments: list[dict]
    reason: str
    metrics: dict


class TranscriptFetcher:
    RUSSIAN_CODES = ["ru", "ru-RU", "ru-UA", "ru-BY", "ru-KZ"]

    def __init__(self, settings):
        self.settings = settings

    def _normalize_items(self, raw_items: list[Any]) -> list[dict]:
        items = []
        for item in raw_items:
            if hasattr(item, "text"):
                text = getattr(item, "text", "")
                start = float(getattr(item, "start", 0.0) or 0.0)
                duration = float(getattr(item, "duration", 0.0) or 0.0)
            else:
                text = item.get("text", "")
                start = float(item.get("start", 0.0) or 0.0)
                duration = float(item.get("duration", 0.0) or 0.0)

            items.append(
                {
                    "text": text,
                    "start": start,
                    "end": start + duration,
                    "duration": duration,
                }
            )
        return items

    def _fetch_with_new_api(self, video_id: str, languages: list[str]) -> tuple[list[dict] | None, dict]:
        try:
            api = YouTubeTranscriptApi()
            fetched = api.fetch(video_id, languages=languages)
            return self._normalize_items(list(fetched)), {
                "fetch_method": "new_api_fetch",
                "languages_requested": languages,
            }
        except Exception:
            return None, {}

    def _fetch_with_old_api(self, video_id: str, languages: list[str]) -> tuple[list[dict] | None, dict]:
        try:
            data = YouTubeTranscriptApi.get_transcript(video_id, languages=languages)
            return self._normalize_items(data), {
                "fetch_method": "old_api_get_transcript",
                "languages_requested": languages,
            }
        except Exception:
            return None, {}

    def _get_transcript_list(self, video_id: str):
        # Пробуем новый стиль
        try:
            api = YouTubeTranscriptApi()
            return api.list(video_id)
        except Exception:
            pass

        # Пробуем старый стиль
        try:
            return YouTubeTranscriptApi.list_transcripts(video_id)
        except Exception:
            return None

    def _translation_languages_contains_ru(self, transcript) -> bool:
        langs = getattr(transcript, "translation_languages", []) or []
        for lang in langs:
            if isinstance(lang, dict):
                code = lang.get("language_code", "")
            else:
                code = getattr(lang, "language_code", "")
            if code == "ru":
                return True
        return False

    def _fetch_from_list(self, video_id: str) -> tuple[list[dict] | None, dict]:
        transcript_list = self._get_transcript_list(video_id)
        if not transcript_list:
            return None, {}

        candidates = []

        try:
            iterable = list(transcript_list)
        except Exception:
            iterable = []

        for transcript in iterable:
            lang_code = getattr(transcript, "language_code", "") or ""
            is_generated = bool(getattr(transcript, "is_generated", False))
            is_translatable = bool(getattr(transcript, "is_translatable", False))
            can_translate_to_ru = is_translatable and self._translation_languages_contains_ru(transcript)

            score = 0
            if lang_code in self.RUSSIAN_CODES or lang_code.startswith("ru"):
                score += 100
            if not is_generated:
                score += 10
            else:
                score += 5
            if can_translate_to_ru and not (lang_code in self.RUSSIAN_CODES or lang_code.startswith("ru")):
                score += 40

            candidates.append(
                {
                    "transcript": transcript,
                    "lang_code": lang_code,
                    "is_generated": is_generated,
                    "can_translate_to_ru": can_translate_to_ru,
                    "score": score,
                }
            )

        candidates.sort(key=lambda x: x["score"], reverse=True)

        for candidate in candidates:
            transcript = candidate["transcript"]
            lang_code = candidate["lang_code"]

            # 1. Если уже русский — просто fetch
            if lang_code in self.RUSSIAN_CODES or lang_code.startswith("ru"):
                try:
                    fetched = transcript.fetch()
                    return self._normalize_items(list(fetched)), {
                        "fetch_method": "list_fetch_ru",
                        "language_code": lang_code,
                        "is_generated": candidate["is_generated"],
                    }
                except Exception:
                    pass

            # 2. Если не русский, но можно перевести на русский — переводим
            if candidate["can_translate_to_ru"]:
                try:
                    translated = transcript.translate("ru")
                    fetched = translated.fetch()
                    return self._normalize_items(list(fetched)), {
                        "fetch_method": "list_translate_to_ru",
                        "language_code": lang_code,
                        "translated_to": "ru",
                        "is_generated": candidate["is_generated"],
                    }
                except Exception:
                    pass

        return None, {}

    def _fetch_raw(self, video_id: str) -> tuple[list[dict] | None, dict]:
        # 1. Пробуем запросить русский через новый API
        data, meta = self._fetch_with_new_api(video_id, self.RUSSIAN_CODES)
        if data:
            return data, meta

        # 2. Пробуем запросить русский через старый API
        data, meta = self._fetch_with_old_api(video_id, self.RUSSIAN_CODES)
        if data:
            return data, meta

        # 3. Пробуем через список доступных транскриптов:
        #    русские ручные / русские авто / перевод в русский
        data, meta = self._fetch_from_list(video_id)
        if data:
            return data, meta

        return None, {}

    @staticmethod
    def _transcript_metrics(segments: list[dict], video_duration_sec: int) -> dict:
        text = " ".join(seg["text"] for seg in segments)

        total_chars = len(text)
        total_alpha = len(re.findall(r"[A-Za-zА-Яа-яЁё]", text))
        total_cyr = len(re.findall(r"[А-Яа-яЁё]", text))
        cyrillic_ratio = (total_cyr / total_alpha) if total_alpha else 0.0

        covered_seconds = sum(
            max(0.0, seg.get("duration", seg["end"] - seg["start"]))
            for seg in segments
        )
        coverage_ratio = (covered_seconds / video_duration_sec) if video_duration_sec else 0.0

        avg_words_per_segment = (
            sum(len(seg["text"].split()) for seg in segments) / len(segments)
            if segments else 0.0
        )

        music_count = len(re.findall(r"\[(?:музыка|music)\]", text, flags=re.IGNORECASE))

        duplicate_segments = 0
        seen = set()
        for seg in segments:
            norm = re.sub(r"\s+", " ", seg["text"].strip().lower())
            if not norm:
                continue
            if norm in seen:
                duplicate_segments += 1
            seen.add(norm)

        duplicate_ratio = (duplicate_segments / len(segments)) if segments else 0.0

        return {
            "total_chars": total_chars,
            "coverage_ratio": round(coverage_ratio, 3),
            "cyrillic_ratio": round(cyrillic_ratio, 3),
            "avg_words_per_segment": round(avg_words_per_segment, 2),
            "music_count": music_count,
            "duplicate_ratio": round(duplicate_ratio, 3),
            "segment_count": len(segments),
        }

    def _is_quality_good(self, metrics: dict) -> tuple[bool, str]:
        if metrics["total_chars"] < self.settings.min_transcript_chars:
            return False, "too_short"
        if metrics["coverage_ratio"] < self.settings.min_coverage_ratio:
            return False, "low_coverage"
        if metrics["cyrillic_ratio"] < self.settings.min_cyrillic_ratio:
            return False, "low_cyrillic_ratio"
        if metrics["avg_words_per_segment"] < self.settings.min_avg_words_per_segment:
            return False, "segments_too_short"
        if metrics["duplicate_ratio"] > 0.20:
            return False, "too_many_duplicates"
        return True, "ok"

    def fetch_transcript(self, video_id: str, video_duration_sec: int, clean_fn) -> TranscriptResult:
        raw_segments, source_meta = self._fetch_raw(video_id)
        if not raw_segments:
            return TranscriptResult(
                status="missing",
                segments=[],
                reason="transcript_not_found",
                metrics={},
            )

        segments = []
        for seg in raw_segments:
            text = clean_fn(seg["text"])
            if not text:
                continue
            segments.append(
                {
                    "start": float(seg["start"]),
                    "end": float(seg["end"]),
                    "text": text,
                    "duration": float(seg.get("duration", seg["end"] - seg["start"])),
                }
            )

        metrics = self._transcript_metrics(segments, video_duration_sec)
        metrics.update(source_meta)

        ok, reason = self._is_quality_good(metrics)

        return TranscriptResult(
            status="ok" if ok else "bad_quality",
            segments=segments,
            reason=reason,
            metrics=metrics,
        )
