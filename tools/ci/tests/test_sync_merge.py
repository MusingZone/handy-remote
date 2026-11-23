import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("sync_merge", Path(__file__).parents[1] / "sync_merge.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)

CONFLICTED = (
    "fn a() {}\r\n"
    "<<<<<<< HEAD\r\n"
    "let x = fork();\r\n"
    "||||||| base\r\n"
    "let x = old();\r\n"
    "=======\r\n"
    "let x = upstream();\r\n"
    ">>>>>>> v9.9.9\r\n"
    "fn b() {}\r\n"
    "<<<<<<< HEAD\r\n"
    "one\r\n"
    "||||||| base\r\n"
    "=======\r\n"
    "two\r\n"
    ">>>>>>> v9.9.9\r\n"
)


class ResolveTest(unittest.TestCase):
    def test_replaces_every_hunk_with_the_reply_and_keeps_crlf(self):
        prompts = []

        def ask(prompt):
            prompts.append(prompt)
            body = "merged-2\n" if "two" in prompt.split("THEIRS")[1] else "merged-1a\nmerged-1b\n"
            return "Sure.\n```resolved\n" + body + "```\n"

        merged, count = sync.resolve_text("src/x.rs", CONFLICTED, ask)
        self.assertEqual(count, 2)
        self.assertEqual(merged, "fn a() {}\r\nmerged-1a\r\nmerged-1b\r\nfn b() {}\r\nmerged-2\r\n")
        self.assertIn("let x = fork();", prompts[1])
        self.assertIn("let x = upstream();", prompts[1])

    def test_rejects_replies_that_keep_markers_or_lack_the_block(self):
        with self.assertRaisesRegex(ValueError, "markers"):
            sync.resolve_text("x", CONFLICTED, lambda p: "```resolved\n<<<<<<< HEAD\n```")
        with self.assertRaisesRegex(ValueError, "no ```resolved"):
            sync.resolve_text("x", CONFLICTED, lambda p: "merged")

    def test_files_without_hunks_or_with_huge_hunks_are_not_resolved(self):
        with self.assertRaisesRegex(ValueError, "no conflict hunks"):
            sync.resolve_text("x", "plain\n", lambda p: "")
        huge = "<<<<<<< a\n" + "x\n" * sync.MAX_PROMPT_CHARS + "=======\ny\n>>>>>>> b\n"
        with self.assertRaisesRegex(ValueError, "too large"):
            sync.resolve_text("x", huge, lambda p: "")


class LocalesTest(unittest.TestCase):
    def test_fills_missing_keys_with_english_and_keeps_existing_translations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for lang, data in {
                "en": {"a": "A", "group": {"b": "B", "c": "C"}},
                "th": {"a": "ก", "group": {"b": "ข"}},
                "zh": {"a": "甲", "group": {"b": "乙", "c": "丙"}},
            }.items():
                (root / lang).mkdir()
                (root / lang / "translation.json").write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
            zh_before = (root / "zh" / "translation.json").read_bytes()
            sync.main(["locales", "--dir", directory])
            th = json.loads((root / "th" / "translation.json").read_text(encoding="utf-8"))
            self.assertEqual(th, {"a": "ก", "group": {"b": "ข", "c": "C"}})
            self.assertEqual((root / "zh" / "translation.json").read_bytes(), zh_before)


if __name__ == "__main__":
    unittest.main()
