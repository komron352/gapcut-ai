import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# bot.py validates BOT_TOKEN during import; this syntactically valid dummy token
# keeps these pure-function tests independent of Telegram credentials.
os.environ.setdefault("BOT_TOKEN", "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")

import unittest

from bot import clean_options, get_video_filter, merge_mixed_transcripts, plan_cuts


class MixedTranscriptTests(unittest.TestCase):
    def test_alternate_pass_recovers_missing_language_word(self):
        tg = {
            "text": "Салом имрӯз",
            "avg_logprob": -0.4,
            "words": [
                {"word": "Салом", "start": 0.0, "end": 0.25},
                {"word": "имрӯз", "start": 0.75, "end": 1.0},
            ],
        }
        ru = {
            "text": "Привет сегодня",
            "avg_logprob": -0.1,
            "words": [
                {"word": "Привет", "start": 0.0, "end": 0.25},
                {"word": "сегодня", "start": 0.4, "end": 0.65},
            ],
        }
        merged = merge_mixed_transcripts(tg, ru)
        self.assertEqual([w["word"] for w in merged], ["Салом", "сегодня", "имрӯз"])
        self.assertEqual([w["lang"] for w in merged], ["tg", "ru", "tg"])

    def test_same_time_alternate_word_is_not_duplicated(self):
        tg = {"text": "Салом имрӯз", "avg_logprob": -0.4, "words": [
            {"word": "Салом", "start": 0.0, "end": 0.3},
        ]}
        ru = {"text": "Привет", "avg_logprob": -0.1, "words": [
            {"word": "Привет", "start": 0.0, "end": 0.3},
        ]}
        merged = merge_mixed_transcripts(tg, ru)
        self.assertEqual(len(merged), 1)


class ReframingTests(unittest.TestCase):
    def test_landscape_to_vertical_preserves_full_frame_with_blurred_fill(self):
        chain = get_video_filter("9:16", 720, 1280, 1920, 1080)
        self.assertIn("split=2", chain)
        self.assertIn("force_original_aspect_ratio=decrease", chain)
        self.assertIn("overlay=", chain)

    def test_native_portrait_source_does_not_need_blurred_fill(self):
        chain = get_video_filter("9:16", 720, 1280, 1080, 1920)
        self.assertNotIn("split=2", chain)
        self.assertIn("crop=720:1280", chain)


class RenderPolicyTests(unittest.TestCase):
    def test_music_is_off_and_voice_cleanup_is_conservative_by_default(self):
        options = clean_options("", "m", "auto", "1")
        self.assertEqual(options["music"], "")
        self.assertEqual(options["clean_audio"], "auto")


class SafeCutTests(unittest.TestCase):
    def test_invalid_timestamp_disables_cut(self):
        words = [
            {"word": "a", "start": 0.2, "end": 0.4},
            {"word": "b", "start": 1.0, "end": 0.8},
            {"word": "c", "start": 2.0, "end": 2.2},
        ]
        self.assertIsNone(plan_cuts(words, 3.0, "normal"))

    def test_cut_keeps_every_recognized_word_inside_kept_ranges(self):
        words = []
        for i in range(100):
            start = 0.2 + i * 0.18 + (1.0 if i >= 50 else 0.0)
            words.append({"word": f"w{i}", "start": start, "end": start + 0.12})
        plan = plan_cuts(words, 20.0, "normal")
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan["words"]), len(words))
        self.assertTrue(all(w["end"] > w["start"] for w in plan["words"]))
        self.assertTrue(all(
            any(a <= src["start"] and src["end"] <= b for a, b in plan["segments"])
            for src in words
        ))


if __name__ == "__main__":
    unittest.main()