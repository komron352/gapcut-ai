import unittest

import webrtcvad

from gapcut_engine import build_edit_decision_list, build_transcript_qa, vad_flags_to_silences


class WebRTCVADBoundaryTests(unittest.TestCase):
    def test_installed_webrtcvad_accepts_30ms_16khz_pcm(self):
        detector = webrtcvad.Vad(1)
        silence_frame = b"\x00" * 960
        self.assertFalse(detector.is_speech(silence_frame, 16000))


    def test_bridges_short_unvoiced_gap_inside_a_phrase(self):
        flags = [False] * 3 + [True] * 10 + [False] * 5 + [True] * 10 + [False] * 3
        silences = vad_flags_to_silences(flags, len(flags) * 0.03)
        self.assertEqual(silences, [])

    def test_reports_long_internal_pause_with_padding(self):
        flags = [False] * 3 + [True] * 10 + [False] * 25 + [True] * 10 + [False] * 3
        silences = vad_flags_to_silences(flags, len(flags) * 0.03)
        self.assertEqual(len(silences), 1)
        self.assertGreater(silences[0][1] - silences[0][0], 0.25)


class EditDecisionListTests(unittest.TestCase):
    def test_builds_source_grounded_hook_and_scene_motion(self):
        words = [
            {"word": "Чаро", "start": 0.2, "end": 0.5},
            {"word": "ин", "start": 0.52, "end": 0.65},
            {"word": "муҳим", "start": 0.66, "end": 1.0, "key": True},
            {"word": "аст?", "start": 1.02, "end": 1.3},
        ]
        scenes = [{"start": 0.0, "end": 2.0, "role": "question", "importance": 0.9}]
        plan = build_edit_decision_list(words, scenes, [], 5.0)
        self.assertEqual(plan["hook"]["text"], "Чаро ин муҳим аст?")
        self.assertAlmostEqual(plan["hook"]["start"], 0.2)
        self.assertAlmostEqual(plan["hook"]["end"], 1.3)
        self.assertEqual(plan["motion"][0]["kind"], "push_in")
        self.assertEqual(plan["captions"][0]["highlight_index"], 2)
        self.assertEqual(plan["policy"]["music"], "none")

    def test_does_not_select_late_scene_as_opening_hook(self):
        words = [{"word": "Биёед", "start": 3.1, "end": 3.5}]
        scenes = [{"start": 3.0, "end": 5.0, "role": "question", "importance": 0.9}]
        plan = build_edit_decision_list(words, scenes, [], 6.0)
        self.assertIsNone(plan["hook"])

    def test_never_fabricates_hook_when_no_scene_is_marked(self):
        words = [{"word": "Салом", "start": 0.1, "end": 0.4}]
        plan = build_edit_decision_list(
            words, [{"start": 0, "end": 2, "role": "point", "importance": 0.4}], [], 2
        )
        self.assertIsNone(plan["hook"])

    def test_maps_timeline_after_pause_cut(self):
        words = [
            {"word": "Аввал", "start": 0.2, "end": 0.5},
            {"word": "баъд", "start": 2.2, "end": 2.6},
        ]
        plan = build_edit_decision_list(
            words, [{"start": 0, "end": 4, "role": "point", "importance": 0.5}],
            [], 4, keep_ranges=[(0, 1), (2, 4)]
        )
        self.assertAlmostEqual(plan["output_duration"], 3.0)
        self.assertAlmostEqual(plan["captions"][1]["start"], 1.2)

    def test_rejects_invalid_duration(self):
        with self.assertRaises(ValueError):
            build_edit_decision_list([], [], [], float("nan"))

    def test_transcript_qa_reports_segment_flags_without_claiming_word_confidence(self):
        words = [
            {"word": "Салом", "start": 0.1, "end": 0.4, "lang": "tg", "low": True},
            {"word": "hello", "start": 2.0, "end": 2.3, "lang": "en",
             "fixed": True, "orig": "helo"},
        ]
        qa = build_transcript_qa(words, 3.0)
        self.assertEqual(qa["low_segment_logprob_count"], 1)
        self.assertEqual(qa["correction_count"], 1)
        self.assertEqual(qa["language_switches"][0]["to"], "en")
        self.assertIn("not calibrated per-word", qa["confidence_note"])


if __name__ == "__main__":
    unittest.main()
