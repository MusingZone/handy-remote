import os
import tempfile
import unittest

import _path  # noqa: F401
import config


class ConfigTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = temporary.name
        os.makedirs(os.path.join(self.home, "config"))

    def write(self, text):
        with open(os.path.join(self.home, "config", "config.toml"), "w", encoding="utf-8") as f:
            f.write(text)

    def test_defaults_live_under_home(self):
        c = config.load(self.home, env={})
        self.assertEqual(c.port, 47821)
        self.assertEqual(c.bind, "127.0.0.1")
        self.assertIsNone(c.offline_hours)
        self.assertEqual(c.models_dir, os.path.join(self.home, "models"))
        self.assertEqual(c.lexicon, os.path.join(self.home, "config", "lexicon.json"))
        self.assertEqual(c.warmup_wav, os.path.join(self.home, "config", "warmup.wav"))

    def test_sections_are_flattened_and_env_wins(self):
        self.write('[server]\nport = 5000\n[lifecycle]\noffline_window = "1-9"\ndraft_enabled = false\n')
        c = config.load(self.home, env={"HANDY_REMOTE_PORT": "6000"})
        self.assertEqual(c.port, 6000)
        self.assertEqual(c.offline_hours, (1, 9))
        self.assertFalse(c.draft_enabled)

    def test_acceleration_and_audio_capture_require_configuration(self):
        c = config.load(self.home, env={})
        self.assertEqual(c.final_device, "CPU")
        self.assertFalse(c.draft_enabled)
        self.assertFalse(c.warmup_capture)
        self.write('[models]\nfinal_device = "GPU"\ndraft_enabled = true\n[lifecycle]\nwarmup_capture = true\n')
        c = config.load(self.home, env={})
        self.assertEqual(c.final_device, "GPU")
        self.assertTrue(c.draft_enabled)
        self.assertTrue(c.warmup_capture)

    def test_legacy_env_names_still_work(self):
        c = config.load(self.home, env={"VOICE_ASR_NIGHT": "0-6", "VOICE_ASR_NPU": "0", "VOICE_ASR_DEVICE": "CPU"})
        self.assertEqual(c.offline_hours, (0, 6))
        self.assertFalse(c.draft_enabled)
        self.assertEqual(c.final_device, "CPU")
        c = config.load(self.home, env={"VOICE_ASR_PORT": "1", "HANDY_REMOTE_PORT": "2"})
        self.assertEqual(c.port, 2)

    def test_unknown_key_is_an_error(self):
        self.write("prot = 1\n")
        with self.assertRaisesRegex(ValueError, "prot"):
            config.load(self.home, env={})

    def test_bad_offline_window(self):
        with self.assertRaises(ValueError):
            config.load(self.home, env={"HANDY_REMOTE_OFFLINE_WINDOW": "8-30"})

    def test_env_flag(self):
        self.assertTrue(config.env_flag("X_TEST_FLAG"))
        os.environ["HANDY_REMOTE_X_TEST_FLAG"] = "off"
        try:
            self.assertFalse(config.env_flag("X_TEST_FLAG"))
        finally:
            del os.environ["HANDY_REMOTE_X_TEST_FLAG"]


if __name__ == "__main__":
    unittest.main()
