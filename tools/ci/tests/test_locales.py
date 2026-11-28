import json
from pathlib import Path
import re
import unittest

REPO_ROOT = Path(__file__).resolve().parents[3]
LOCALES_DIR = REPO_ROOT / "src" / "i18n" / "locales"
SHARED_KEYS = ("synced", "pending", "unsupported", "failed")
EXPECTED_PLACEHOLDERS = {
    "synced": ["{{count}}"],
    "pending": [],
    "unsupported": [],
    "failed": ["{{error}}"],
}
PLACEHOLDER_RE = re.compile(r"\{\{[^}]+\}\}")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def check_shared_custom_words(lang: str, data: dict) -> list[str]:
    issues: list[str] = []
    shared = (
        data.get("settings", {})
        .get("advanced", {})
        .get("customWords", {})
        .get("shared")
    )
    if not isinstance(shared, dict):
        return [f"{lang}: missing settings.advanced.customWords.shared object"]
    for key in SHARED_KEYS:
        label = f"{lang}: settings.advanced.customWords.shared.{key}"
        val = shared.get(key)
        if not isinstance(val, str) or not val.strip():
            issues.append(f"{label}: expected non-empty string")
            continue
        if "?" in val or "\ufffd" in val:
            issues.append(f"{label}: contains corrupted '?' or replacement char: {val!r}")
        if key == "pending" and not val.endswith("\u2026"):
            issues.append(f"{label}: must end with Unicode ellipsis: {val!r}")
        if lang in ("zh", "zh-TW") and not CJK_RE.search(val):
            issues.append(f"{label}: expected CJK characters for {lang}: {val!r}")
        actual = sorted(PLACEHOLDER_RE.findall(val))
        expected = sorted(EXPECTED_PLACEHOLDERS[key])
        if actual != expected:
            issues.append(f"{label}: placeholder mismatch (expected {expected}, got {actual})")
    return issues


def check_locale_bytes(lang: str, raw: bytes) -> list[str]:
    if raw.startswith(b"\xef\xbb\xbf"):
        return [f"{lang}: UTF-8 BOM is not allowed"]
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"{lang}: invalid UTF-8 JSON ({exc})"]
    return check_shared_custom_words(lang, data)


class LocaleSharedCustomWordsTest(unittest.TestCase):
    def test_all_locales_valid_utf8_no_bom_and_clean_shared_custom_words(self):
        locale_files = sorted(LOCALES_DIR.glob("*/translation.json"))
        self.assertEqual(len(locale_files), 29)
        all_issues: list[str] = []
        for path in locale_files:
            lang = path.parent.name
            all_issues.extend(check_locale_bytes(lang, path.read_bytes()))
        self.assertEqual(all_issues, [], "\n".join(all_issues))

    def test_catches_question_mark_corruption_in_zh_and_pending(self):
        corrupted_zh = {
            "settings": {
                "advanced": {
                    "customWords": {
                        "shared": {
                            "synced": "??? Handy Remote ??????? {{count}} ????????????",
                            "pending": "??? Handy Remote ??????",
                            "unsupported": "?? Handy Remote ??????????????????",
                            "failed": "?????????{{error}}??????????????????",
                        }
                    }
                }
            }
        }
        issues = check_shared_custom_words("zh", corrupted_zh)
        self.assertGreaterEqual(len(issues), 4)

        corrupted_en = {
            "settings": {
                "advanced": {
                    "customWords": {
                        "shared": {
                            "synced": "Shared: {{count}} words.",
                            "pending": "Syncing with the Handy Remote server?",
                            "unsupported": "Update the server.",
                            "failed": "Failed ({{error}}).",
                        }
                    }
                }
            }
        }
        issues_en = check_shared_custom_words("en", corrupted_en)
        self.assertTrue(any("pending" in msg for msg in issues_en))

    def test_catches_placeholder_drift_and_utf8_bom(self):
        bad_placeholders = {
            "settings": {
                "advanced": {
                    "customWords": {
                        "shared": {
                            "synced": "Shared words without placeholder.",
                            "pending": "Syncing\u2026",
                            "unsupported": "Update server.",
                            "failed": "Failed without error.",
                        }
                    }
                }
            }
        }
        issues = check_shared_custom_words("en", bad_placeholders)
        self.assertTrue(any("synced" in msg and "placeholder" in msg for msg in issues))
        self.assertTrue(any("failed" in msg and "placeholder" in msg for msg in issues))

        clean_payload = {
            "settings": {
                "advanced": {
                    "customWords": {
                        "shared": {
                            "synced": "Shared: {{count}} words.",
                            "pending": "Syncing\u2026",
                            "unsupported": "Update server.",
                            "failed": "Failed ({{error}}).",
                        }
                    }
                }
            }
        }
        bom_bytes = b"\xef\xbb\xbf" + json.dumps(clean_payload).encode("utf-8")
        self.assertEqual(check_locale_bytes("en", bom_bytes), ["en: UTF-8 BOM is not allowed"])

    def test_allows_legitimate_question_marks_in_other_keys(self):
        payload = {
            "other": {"confirmDelete": "Delete this recording?"},
            "settings": {
                "advanced": {
                    "customWords": {
                        "shared": {
                            "synced": "Shared: {{count}} words.",
                            "pending": "Syncing\u2026",
                            "unsupported": "Update server.",
                            "failed": "Failed ({{error}}).",
                        }
                    }
                }
            },
        }
        self.assertEqual(check_shared_custom_words("en", payload), [])


if __name__ == "__main__":
    unittest.main()
