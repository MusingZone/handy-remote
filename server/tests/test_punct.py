"""Rule-based part of punctuation: how units are joined, with and without the punctuation model."""
import unittest

import _path  # noqa: F401
from punct import commit_unit


class FakeModel:
    """Stands in for the CT-Transformer: always proposes the given join mark."""

    def __init__(self, join):
        self.join = join

    def fix(self, committed, text, held, gap, comma_gap):
        return self.join, text


class CommitUnitTest(unittest.TestCase):
    def test_first_unit_holds_its_full_stop(self):
        self.assertEqual(commit_unit(None, "", "", "今天天气很好。", None, 1.0), ("今天天气很好", "。"))

    def test_short_pause_turns_full_stop_into_comma(self):
        out = commit_unit(None, "今天天气很好", "。", "我们出去走走。", 0.3, 1.0)
        self.assertEqual(out, ("今天天气很好，我们出去走走", "。"))

    def test_long_pause_keeps_full_stop(self):
        out = commit_unit(None, "今天天气很好", "。", "我们出去走走。", 1.5, 1.0)
        self.assertEqual(out, ("今天天气很好。我们出去走走", "。"))

    def test_english_units_are_spaced(self):
        self.assertEqual(commit_unit(None, "hello", ".", "world.", 0.3, 1.0), ("hello, world", "."))

    def test_filler_only_unit_is_dropped(self):
        self.assertEqual(commit_unit(None, "我们走吧", "。", "嗯。", 0.5, 1.0), ("我们走吧", "。"))

    def test_leading_filler_is_removed(self):
        committed, _ = commit_unit(None, "", "", "嗯，我们走吧。", None, 1.0)
        self.assertEqual(committed, "我们走吧")

    def test_model_join_is_used(self):
        out = commit_unit(FakeModel("，"), "我想试一下", "", "这个插件", 0.4, 1.0)
        self.assertEqual(out, ("我想试一下，这个插件", ""))

    def test_pause_after_finished_unit_needs_a_mark(self):
        committed, _ = commit_unit(FakeModel(""), "今天天气很好", "。", "我们出去走走", 1.5, 1.0)
        self.assertEqual(committed, "今天天气很好，我们出去走走")

    def test_open_ending_never_gets_a_mark(self):
        committed, _ = commit_unit(FakeModel(""), "我想用一下这个", "。", "插件", 1.5, 1.0)
        self.assertEqual(committed, "我想用一下这个插件")

    def test_very_long_pause_keeps_full_stop(self):
        committed, _ = commit_unit(FakeModel("，"), "今天天气很好", "。", "我们出去走走", 2.0, 1.0, stop_gap=1.4)
        self.assertEqual(committed, "今天天气很好。我们出去走走")


if __name__ == "__main__":
    unittest.main()
