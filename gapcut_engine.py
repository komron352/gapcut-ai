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
