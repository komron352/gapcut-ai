import unittest

from gapcut_engine import build_edit_decision_list


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
        self.assertEqual(plan["motion"][0]["kind"], "subtle_zoom")
        self.assertEqual(plan["captions"][0]["highlight_index"], 2)
        self.assertEqual(plan["policy"]["music"], "none")

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


if __name__ == "__main__":
    unittest.main()
