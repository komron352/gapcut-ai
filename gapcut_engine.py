"""Deterministic, source-timeline-first edit decision list for GAPCUT AI.

The director never invents transcript text or timestamps. It converts verified
word timings, scene analysis and selected B-roll cards into a serializable plan.
"""
from __future__ import annotations

import math
from typing import Any, Iterable

SCHEMA_VERSION = "1.0"


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _valid_words(words: Iterable[dict], duration: float) -> list[dict]:
    valid = []
    previous_start = -1.0
    previous_end = -1.0
    for item in words or []:
        if not isinstance(item, dict):
            continue
        word = str(item.get("word", "")).strip()
        start, end = _finite(item.get("start")), _finite(item.get("end"))
        if not word or start is None or end is None:
            continue
        if start < 0 or end <= start or end > duration + 0.25:
            continue
        if start < previous_start or start < previous_end - 0.08:
            # Ambiguous overlapping timing is not safe director input.
            continue
        valid.append({**item, "word": word, "start": start, "end": min(end, duration)})
        previous_start, previous_end = start, end
    return [item for item in valid if item["end"] > item["start"]]


def _keep_map(duration: float, keep_ranges: list | None) -> list[dict]:
    ranges = keep_ranges or [(0.0, duration)]
    cleaned = []
    cursor = 0.0
    output_cursor = 0.0
    for pair in ranges:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        start, end = _finite(pair[0]), _finite(pair[1])
        if start is None or end is None:
            continue
        start, end = max(cursor, start, 0.0), min(duration, end)
        if end <= start:
            continue
        length = end - start
        cleaned.append({
            "source_start": round(start, 3),
            "source_end": round(end, 3),
            "output_start": round(output_cursor, 3),
            "output_end": round(output_cursor + length, 3),
        })
        cursor = end
        output_cursor += length
    return cleaned or [{
        "source_start": 0.0, "source_end": round(duration, 3),
        "output_start": 0.0, "output_end": round(duration, 3),
    }]


def _map_time(moment: float, timeline: list[dict]) -> float | None:
    for part in timeline:
        if part["source_start"] <= moment <= part["source_end"]:
            return round(part["output_start"] + moment - part["source_start"], 3)
    return None


def _normalize_scenes(scenes: Iterable[dict], duration: float) -> list[dict]:
    source = []
    for scene in scenes or []:
        if not isinstance(scene, dict):
            continue
        start, end = _finite(scene.get("start")), _finite(scene.get("end"))
        if start is None or end is None:
            continue
        start, end = max(0.0, start), min(duration, end)
        if end - start < 0.35:
            continue
        role = str(scene.get("role", "point"))
        if role not in {"hook", "point", "example", "question", "cta", "outro", "emphasis"}:
            role = "point"
        importance = _finite(scene.get("importance"))
        importance = 0.4 if importance is None else max(0.0, min(1.0, importance))
        source.append({"start": start, "end": end, "role": role, "importance": importance})
    source.sort(key=lambda item: (item["start"], item["end"]))
    result = []
    cursor = 0.0
    for scene in source:
        start = max(cursor, scene["start"])
        if start > cursor + 0.35:
            result.append({"start": cursor, "end": start, "role": "point", "importance": 0.35})
        end = scene["end"]
        if end > start + 0.35:
            result.append({**scene, "start": round(start, 3), "end": round(end, 3)})
            cursor = end
    if not result:
        return [{"start": 0.0, "end": round(duration, 3), "role": "point", "importance": 0.35}]
    if cursor < duration - 0.35:
        result.append({"start": round(cursor, 3), "end": round(duration, 3),
                       "role": "point", "importance": 0.35})
    return result


def _caption_cues(words: list[dict], timeline: list[dict]) -> list[dict]:
    cues, current = [], []

    def flush():
        if not current:
            return
        start, end = current[0]["start"], current[-1]["end"]
        mapped_start, mapped_end = _map_time(start, timeline), _map_time(end, timeline)
        if mapped_start is not None and mapped_end is not None and mapped_end > mapped_start:
            cues.append({
                "start": mapped_start,
                "end": mapped_end,
                "source_start": round(start, 3),
                "source_end": round(end, 3),
                "text": " ".join(item["word"] for item in current),
                "words": [item["word"] for item in current],
                "highlight_index": next((i for i, item in enumerate(current) if item.get("key")), None),
            })
        current.clear()

    for index, item in enumerate(words):
        if current:
            gap = item["start"] - current[-1]["end"]
            trial = current + [item]
            char_count = sum(len(w["word"]) + 1 for w in trial)
            if gap > 0.7 or len(trial) > 4 or char_count > 34:
                flush()
        current.append(item)
        if item["word"].rstrip().endswith((".", "?", "!", "…")):
            flush()
    flush()
    return cues


def build_edit_decision_list(
    words: Iterable[dict],
    scenes: Iterable[dict],
    cards: Iterable[dict],
    duration: float,
    style: str = "viral",
    video_format: str = "9:16",
    keep_ranges: list | None = None,
) -> dict:
    """Build a validated EDL without changing or fabricating source content."""
    duration = _finite(duration)
    if duration is None or duration <= 0:
        raise ValueError("duration must be a finite positive number")

    clean_words = _valid_words(words, duration)
    timeline = _keep_map(duration, keep_ranges)
    output_duration = timeline[-1]["output_end"]
    clean_scenes = _normalize_scenes(scenes, duration)

    mapped_scenes = []
    for scene in clean_scenes:
        for part in timeline:
            start = max(scene["start"], part["source_start"])
            end = min(scene["end"], part["source_end"])
            if end - start < 0.35:
                continue
            out_start = part["output_start"] + start - part["source_start"]
            out_end = part["output_start"] + end - part["source_start"]
            mapped_scenes.append({
                "source_start": round(start, 3),
                "source_end": round(end, 3),
                "start": round(out_start, 3),
                "end": round(out_end, 3),
                "role": scene["role"],
                "importance": scene["importance"],
            })

    hook_scene = next(
        (scene for scene in clean_scenes
         if scene["role"] in ("hook", "question") or scene["importance"] >= 0.85),
        None,
    )
    hook = None
    if hook_scene:
        hook_words = [
            item for item in clean_words
            if hook_scene["start"] <= item["start"] and item["end"] <= hook_scene["end"]
        ][:9]
        if hook_words:
            hook = {
                "text": " ".join(item["word"] for item in hook_words),
                "source_start": round(hook_words[0]["start"], 3),
                "source_end": round(hook_words[-1]["end"], 3),
                "note": "selected_from_source_transcript",
            }

    motion = []
    for scene in mapped_scenes:
        role, importance = scene["role"], scene["importance"]
        if role in ("hook", "question", "emphasis", "cta") or importance >= 0.85:
            motion.append({
                "at": scene["start"],
                "until": min(output_duration, scene["start"] + 0.55),
                "kind": "push_in" if role in ("hook", "emphasis", "cta") or importance >= 0.85 else "subtle_zoom",
                "reason": role,
                "intensity": round(min(0.14, 0.06 + importance * 0.08), 3),
            })

    broll = []
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        moment = _finite(card.get("at"))
        if moment is None:
            moment = _finite(card.get("start"))
        if moment is None or not 0 <= moment < duration:
            continue
        mapped = _map_time(moment, timeline)
        if mapped is None:
            continue
        broll.append({
            "at": mapped,
            "source_at": round(moment, 3),
            "card_id": str(card.get("id", ""))[:80],
            "topic": str(card.get("query") or card.get("topic") or card.get("title") or "")[:120],
            "requires_asset_validation": True,
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "engine": "hybrid",
        "style": str(style or "viral"),
        "format": str(video_format or "9:16"),
        "source_duration": round(duration, 3),
        "output_duration": round(output_duration, 3),
        "timeline": timeline,
        "scenes": mapped_scenes,
        "hook": hook,
        "captions": _caption_cues(clean_words, timeline),
        "motion": motion,
        "broll": broll,
        "policy": {
            "preserve_original_voice": True,
            "music": "none",
            "never_invent_transcript": True,
            "never_cut_unvalidated_speech": True,
        },
    }


def build_transcript_qa(words: Iterable[dict], duration: float, detected: Iterable[dict] = None) -> dict:
    """Summarize transcript integrity without pretending to have calibrated word confidence."""
    duration_value = _finite(duration)
    if duration_value is None or duration_value <= 0:
        raise ValueError("duration must be a finite positive number")

    source = list(words or [])
    valid = _valid_words(source, duration_value)
    low_flags = []
    corrections = []
    languages = {}
    switches = []
    long_pauses = []

    previous = None
    for item in valid:
        language = item.get("lang") or "unknown"
        languages[language] = languages.get(language, 0) + 1
        if item.get("low"):
            low_flags.append({
                "word": item["word"],
                "start": round(item["start"], 3),
                "end": round(item["end"], 3),
                "signal": "low_segment_logprob",
            })
        if item.get("fixed") and item.get("orig"):
            corrections.append({
                "from": str(item["orig"])[:80],
                "to": item["word"][:80],
                "start": round(item["start"], 3),
            })
        if previous is not None:
            gap = item["start"] - previous["end"]
            if gap >= 1.5:
                long_pauses.append({
                    "start": round(previous["end"], 3),
                    "end": round(item["start"], 3),
                    "duration": round(gap, 3),
                })
            if language != previous.get("lang", "unknown"):
                switches.append({
                    "at": round(item["start"], 3),
                    "from": previous.get("lang", "unknown"),
                    "to": language,
                })
        previous = item

    invalid_count = max(0, len(source) - len(valid))
    detected_ranges = []
    for item in detected or []:
        if not isinstance(item, dict):
            continue
        start, end = _finite(item.get("start")), _finite(item.get("end"))
        if start is None or end is None or end <= start:
            continue
        detected_ranges.append({
            "start": round(max(0.0, start), 3),
            "end": round(min(duration_value, end), 3),
            "lang": str(item.get("lang") or "unknown"),
        })

    return {
        "word_count": len(valid),
        "invalid_or_ambiguous_word_count": invalid_count,
        "low_segment_logprob_flags": low_flags,
        "low_segment_logprob_count": len(low_flags),
        "correction_count": len(corrections),
        "corrections": corrections,
        "language_word_counts": languages,
        "language_switches": switches,
        "long_pauses": long_pauses[:50],
        "detected_language_ranges": detected_ranges,
        "confidence_note": (
            "low flags are derived from provider segment-level average log probability; "
            "they are not calibrated per-word confidence scores"
        ),
    }


def vad_flags_to_silences(
    flags: Iterable[bool],
    duration: float,
    frame_ms: int = 30,
    padding_ms: int = 150,
    min_silence_ms: int = 250,
) -> list[tuple[float, float]]:
    """Convert WebRTC VAD frame decisions to padded internal silence ranges."""
    duration_value = _finite(duration)
    if duration_value is None or duration_value <= 0 or frame_ms not in (10, 20, 30):
        return []
    decisions = [bool(value) for value in flags]
    if not decisions:
        return []

    # Bridge only very short unvoiced gaps between voiced frames so breaths and
    # consonant closures are not treated as safe chunk boundaries.
    max_gap_frames = max(1, int(min_silence_ms // frame_ms))
    index = 0
    while index < len(decisions):
        if decisions[index]:
            index += 1
            continue
        start = index
        while index < len(decisions) and not decisions[index]:
            index += 1
        end = index
        if (
            start > 0 and end < len(decisions)
            and decisions[start - 1] and decisions[end]
            and end - start <= max_gap_frames
        ):
            for pos in range(start, end):
                decisions[pos] = True

    frame_seconds = frame_ms / 1000.0
    padding = padding_ms / 1000.0
    min_silence = min_silence_ms / 1000.0
    speech_ranges = []
    index = 0
    while index < len(decisions):
        if not decisions[index]:
            index += 1
            continue
        start = index
        while index < len(decisions) and decisions[index]:
            index += 1
        end = index
        a = max(0.0, start * frame_seconds - padding)
        b = min(duration_value, end * frame_seconds + padding)
        if speech_ranges and a - speech_ranges[-1][1] <= min_silence:
            speech_ranges[-1][1] = max(speech_ranges[-1][1], b)
        else:
            speech_ranges.append([a, b])

    # Only internal pauses are useful chunk boundaries; leading/trailing
    # silence should not create empty Whisper chunks.
    silences = []
    for left, right in zip(speech_ranges, speech_ranges[1:]):
        if right[0] - left[1] >= min_silence:
            silences.append((round(left[1], 3), round(right[0], 3)))
    return silences
