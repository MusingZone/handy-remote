import importlib.util
import io
from pathlib import Path
import re
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location("scrub_check", Path(__file__).parents[1] / "scrub_check.py")
scrub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scrub)


class ScrubTest(unittest.TestCase):
    def test_scans_archives_and_utf16_without_echoing_content(self):
        patterns = [re.compile("private-sentinel", re.I)]
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as out:
            out.writestr("server/config.toml", 'label = "private-sentinel"')
        hits = scrub.scan_data("server.zip", archive.getvalue(), patterns)
        self.assertIn("server.zip!server/config.toml:1", hits)
        binary = b"\0prefix" + "private-sentinel".encode("utf-16-le")
        self.assertTrue(scrub.scan_data("client.exe", binary, patterns))
        self.assertNotIn("private-sentinel", "\n".join(hits))

    def test_history_checks_messages_and_removals_but_not_unpublished_branches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                return scrub.git(root, *args).strip()

            git("init", "-b", "main")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.com")
            target = root / "example.txt"
            target.write_text("private-sentinel\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "chore: base")
            base = git("rev-parse", "HEAD")
            target.write_text("generic\n", encoding="utf-8")
            git("commit", "-am", "chore: clean")
            patterns = [re.compile("private-sentinel")]
            self.assertTrue(scrub.scan_history(root, patterns, base))
            git("checkout", "-b", "unpublished")
            git("commit", "--allow-empty", "-m", "private-message")
            git("checkout", "main")
            self.assertFalse(scrub.scan_history(root, [re.compile("private-message")], base))
            git("commit", "--allow-empty", "-m", "private-message")
            self.assertTrue(scrub.scan_history(root, [re.compile("private-message")], base))

    def test_generic_user_paths_work_with_both_separators(self):
        patterns = [re.compile(p, re.I) for p in scrub.GENERIC]
        for path in [r"C:\Users" + r"\sample-account\project", "C:/Users" + "/sample-account/project"]:
            self.assertTrue(scrub.scan_data("example.txt", path.encode(), patterns))


if __name__ == "__main__":
    unittest.main()
