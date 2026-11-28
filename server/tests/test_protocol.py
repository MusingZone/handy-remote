"""Handy's worker protocol over TCP, with the models replaced by fakes (no model files needed)."""
import json
import socket
import struct
import threading
import unittest
import subprocess
import sys
import time
from unittest.mock import patch

import numpy as np

import _path  # noqa: F401
import asr_server as srv

SR = srv.SR


class InferenceDeadlineTest(unittest.TestCase):
    def test_device_deadlines_preserve_slow_cpu_inference(self):
        for device, seconds, deadline in (("GPU", 20, 60), ("NPU", 1, 45),
                                          ("CPU", 5, 300), ("CPU retry", 30, 600)):
            with self.subTest(device=device, seconds=seconds), \
                    patch.object(srv.threading, "Timer") as timer:
                srv.inference_call(device, lambda pcm: None, np.zeros(SR * seconds))
                self.assertEqual(timer.call_args.args[0], deadline)

    def test_completed_and_failed_calls_cancel_the_watchdog(self):
        with patch.object(srv, "INFERENCE_TIMEOUT_S", 0.03), \
                patch.object(srv.os, "_exit") as exit_process:
            audio = np.zeros(0, np.float32)
            self.assertEqual(srv.inference_call("GPU", lambda pcm: "done", audio), "done")
            def fail(pcm):
                raise ValueError("device lost")
            with self.assertRaisesRegex(ValueError, "device lost"):
                srv.inference_call("NPU", fail, audio)
            time.sleep(0.06)
            exit_process.assert_not_called()

    def test_stuck_native_call_exits_process_instead_of_blocking_the_queue(self):
        for device in ("GPU", "NPU"):
            with self.subTest(device=device):
                result = subprocess.run(
                    [sys.executable, "-c",
                     "import threading, numpy as np; import asr_server as s; "
                     "s.INFERENCE_TIMEOUT_S = 0.05; "
                     f"s.inference_call('{device}', lambda pcm: threading.Event().wait(), np.zeros(0))"],
                    cwd=srv.ROOT, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 4, result.stderr.decode(errors="replace"))
                self.assertIn(b"restarting the server process", result.stderr)


class FakeStream:
    instances = []

    def __init__(self, eng, run):
        self.chunks = []
        FakeStream.instances.append(self)

    def feed(self, pcm):
        self.chunks.append(len(pcm))
        return {"Fed": {"update": {"revision": len(self.chunks)}, "text": None}}

    def finalize(self, want_language):
        return {"Finalized": {"update": {}, "text": {"full": f"{sum(self.chunks)} samples"},
                              "language": "en" if want_language else None}}


class Client:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.f = self.sock.makefile("rwb")

    def call(self, msg, pcm=None):
        j = json.dumps(msg).encode()
        b = b"" if pcm is None else pcm.astype("<f4").tobytes()
        self.f.write(struct.pack("<I", len(j)) + j + struct.pack("<I", len(b)) + b)
        self.f.flush()
        (n,) = struct.unpack("<I", self.f.read(4))
        resp = json.loads(self.f.read(n))
        (m,) = struct.unpack("<I", self.f.read(4))
        self.f.read(m)
        return resp

    def close(self):
        self.f.close()
        self.sock.close()


class ProtocolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = srv.Server(("127.0.0.1", 0), srv.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_address[1]
        cls._stream = srv.Stream
        srv.Stream = FakeStream

    @classmethod
    def tearDownClass(cls):
        srv.Stream = cls._stream
        cls.server.shutdown()
        cls.server.server_close()

    def set_loaded(self, loaded):
        # "loading" stops ensure_engine() from starting a real model load
        with srv.LIFE_LOCK:
            srv.LIFE["loading"] = not loaded
            srv.ENGINE = object() if loaded else None
            (srv.ENGINE_READY.set if loaded else srv.ENGINE_READY.clear)()

    def setUp(self):
        FakeStream.instances.clear()
        self.set_loaded(True)
        self.c = Client(self.port)

    def tearDown(self):
        self.c.close()

    def test_hello_and_load(self):
        self.assertIn("Hello", self.c.call({"Hello": {}}))
        info = self.c.call({"Load": {"path": "remote-qwen3-asr-1.7b", "backend": "remote"}})["Loaded"]
        self.assertEqual(info["arch"], "qwen3_asr")
        self.assertTrue(info["capabilities"]["supports_streaming"])

    def test_status_does_not_need_the_models(self):
        self.set_loaded(False)
        status = self.c.call({"Status": {}})["Status"]
        self.assertFalse(status["loaded"])
        self.assertTrue(status["openvino"])

    def test_requests_before_load_are_rejected(self):
        self.assertEqual(self.c.call({"Feed": {}}, np.zeros(10, np.float32)), {"Error": "no model loaded"})

    def test_stream_while_models_wake_up(self):
        self.c.call({"Load": {}})
        self.set_loaded(False)
        self.assertEqual(self.c.call({"StreamBegin": {"run": {}}}), "Ok")
        r = self.c.call({"Feed": {}}, np.zeros(SR // 2, np.float32))["Fed"]
        self.assertEqual(r["text"]["tentative"], srv.STATUS_WAKING)
        self.assertIsNone(self.c.call({"Feed": {}}, np.zeros(SR // 2, np.float32))["Fed"]["text"])
        self.set_loaded(True)
        self.c.call({"Feed": {}}, np.zeros(SR // 4, np.float32))
        # buffered audio is replayed in 0.1 s slices, then live audio follows
        self.assertEqual(FakeStream.instances[0].chunks, [SR // 10] * 10 + [SR // 4])
        r = self.c.call({"Finalize": {"want_language": True}})["Finalized"]
        self.assertEqual(r["text"]["full"], f"{SR + SR // 4} samples")
        self.assertEqual(srv.LIFE["busy"], 0)

    def test_finalize_without_stream(self):
        self.c.call({"Load": {}})
        self.assertEqual(self.c.call({"Finalize": {}}), {"Error": "no active stream"})

    def test_unknown_request(self):
        self.c.call({"Load": {}})
        self.assertEqual(self.c.call({"Bogus": {}}), {"Error": "unknown request Bogus"})

    def test_lexicon_round_trip_detects_conflicts(self):
        first = self.c.call({"LexiconGet": {}})["Lexicon"]
        data = {"hotwords": ["OpenVINO"], "replacements": [{"from": "open vino", "to": "OpenVINO"}]}
        new = self.c.call({"LexiconPut": {"data": data, "base_sha256": first["sha256"]}})["Lexicon"]
        self.assertEqual(new["data"], data)
        stale = self.c.call({"LexiconPut": {"data": {"hotwords": []}, "base_sha256": first["sha256"]}})
        self.assertIn("changed on the server", stale["BackendError"])
        bad = self.c.call({"LexiconPut": {"data": {"replacements": [{"from": "(", "to": ""}]},
                                          "base_sha256": new["sha256"]}})
        self.assertIn("BackendError", bad)
        self.assertEqual(self.c.call({"LexiconGet": {}})["Lexicon"]["sha256"], new["sha256"])

    def test_audit_query_returns_records_near_the_time(self):
        import time
        srv.audit("stream", text="hello there")
        records = self.c.call({"AuditQuery": {"ts": time.time(), "window_s": 60}})["Audit"]["records"]
        self.assertEqual(records[-1]["text"], "hello there")
        self.assertEqual(self.c.call({"AuditQuery": {"ts": time.time() - 86400 * 30}})["Audit"]["records"], [])

    def test_hello_while_unloaded_is_immediate_and_does_not_wake_models(self):
        with srv.LIFE_LOCK:
            srv.LIFE["loading"] = False
            srv.ENGINE = None
            srv.ENGINE_READY.clear()
        with patch.object(srv, "ensure_engine") as ensure:
            resp = self.c.call({"Hello": {"list_devices": True}})
            self.assertEqual(len(resp["Hello"]["devices"]), 1)
            ensure.assert_not_called()

    def test_load_while_unloaded_returns_immediately_and_starts_loading(self):
        with srv.LIFE_LOCK:
            srv.LIFE["loading"] = False
            srv.ENGINE = None
            srv.ENGINE_READY.clear()
        with patch.object(srv, "ensure_engine") as ensure:
            resp = self.c.call({"Load": {}})
            self.assertEqual(resp["Loaded"]["arch"], "qwen3_asr")
            ensure.assert_called_once()

    def test_lazy_stream_clears_waking_banner_and_skips_replay_previews(self):
        class TrackingStream:
            instances = []

            def __init__(self, eng, run):
                self.calls = []
                self.allow_preview = True
                self.last_text = ("", "")
                self.revision = 0
                TrackingStream.instances.append(self)

            def feed(self, pcm):
                self.calls.append((len(pcm), self.allow_preview))
                snap = ("", "")
                changed = snap != self.last_text
                if changed:
                    self.revision += 1
                self.last_text = snap
                text = {"full": "", "committed": "", "tentative": ""}
                return {"Fed": {"update": {"result_changed": changed, "revision": self.revision},
                                "text": text if changed else None}}

            def finalize(self, want_language):
                total = sum(n for n, _ in self.calls)
                return {"Finalized": {"update": {}, "text": {"full": f"{total} samples"}, "language": None}}

        with patch.object(srv, "Stream", TrackingStream):
            self.c.call({"Load": {}})
            self.set_loaded(False)
            self.assertEqual(self.c.call({"StreamBegin": {"run": {}}}), "Ok")
            r1 = self.c.call({"Feed": {}}, np.zeros(SR // 5, np.float32))["Fed"]
            self.assertEqual(r1["text"]["tentative"], srv.STATUS_WAKING)
            self.set_loaded(True)
            # Even with empty/silent snapshot after replay, the live feed must replace WAKING.
            r2 = self.c.call({"Feed": {}}, np.zeros(SR // 10, np.float32))["Fed"]
            self.assertTrue(r2["update"]["result_changed"])
            self.assertEqual(r2["text"]["tentative"], "")
            inst = TrackingStream.instances[0]
            self.assertEqual(inst.calls, [(SR // 10, False), (SR // 10, False), (SR // 10, True)])
            fin = self.c.call({"Finalize": {"want_language": False}})["Finalized"]
            self.assertEqual(fin["text"]["full"], f"{SR // 5 + SR // 10} samples")

            # If Finalize arrives before load completes, wait and replay without previews.
            TrackingStream.instances.clear()
            self.set_loaded(False)
            self.assertEqual(self.c.call({"StreamBegin": {"run": {}}}), "Ok")
            self.c.call({"Feed": {}}, np.zeros(SR // 5, np.float32))
            threading.Timer(0.05, lambda: self.set_loaded(True)).start()
            fin2 = self.c.call({"Finalize": {"want_language": False}})["Finalized"]
            self.assertEqual(fin2["text"]["full"], f"{SR // 5} samples")
            self.assertEqual(TrackingStream.instances[0].calls, [(SR // 10, False), (SR // 10, False)])
            self.assertEqual(srv.LIFE["busy"], 0)

    def test_default_load_wait_leaves_decode_headroom(self):
        self.assertEqual(srv.LOAD_WAIT_S, 75.0)

    def test_finalize_and_run_bound_load_wait(self):
        self.c.call({"Load": {}})
        self.set_loaded(False)
        with patch.object(srv, "LOAD_WAIT_S", 0.05):
            self.assertEqual(self.c.call({"StreamBegin": {"run": {}}}), "Ok")
            self.c.call({"Feed": {}}, np.zeros(SR // 10, np.float32))
            t0 = time.time()
            fin = self.c.call({"Finalize": {"want_language": False}})
            self.assertIn("BackendError", fin)
            self.assertLess(time.time() - t0, 1.0)
            self.assertEqual(srv.LIFE["busy"], 0)

            t0 = time.time()
            run = self.c.call({"Run": {"options": {}}}, np.zeros(SR // 10, np.float32))
            self.assertIn("BackendError", run)
            self.assertLess(time.time() - t0, 1.0)
            self.assertEqual(srv.LIFE["busy"], 0)


class BootLifecycleTest(unittest.TestCase):
    def test_boot_checks_busy_after_load_before_warming_up(self):
        seen = []

        class FakeEngine:
            def __init__(self, warm_up=True, should_warm_up=None):
                # Client starts dictating during model load (after _boot started)
                with srv.LIFE_LOCK:
                    srv.LIFE["busy"] += 1
                seen.append(bool(warm_up and (should_warm_up is None or should_warm_up())))
                with srv.LIFE_LOCK:
                    srv.LIFE["busy"] -= 1

        old_eng = srv.ENGINE
        old_loading = srv.LIFE["loading"]
        old_busy = srv.LIFE["busy"]
        old_ready = srv.ENGINE_READY.is_set()
        try:
            with srv.LIFE_LOCK:
                srv.ENGINE = None
                srv.LIFE["loading"] = True
                srv.LIFE["busy"] = 0
                srv.ENGINE_READY.clear()
            with patch.object(srv, "Engine", FakeEngine):
                srv._boot()
            self.assertEqual(seen, [False])
            self.assertTrue(srv.ENGINE_READY.is_set())
            self.assertFalse(srv.LIFE["loading"])
        finally:
            with srv.LIFE_LOCK:
                srv.ENGINE = old_eng
                srv.LIFE["loading"] = old_loading
                srv.LIFE["busy"] = old_busy
                (srv.ENGINE_READY.set if old_ready else srv.ENGINE_READY.clear)()

    def test_stuck_load_exits_process_via_watchdog(self):
        result = subprocess.run(
            [sys.executable, "-c",
             "import threading; import asr_server as s; "
             "s.LOAD_TIMEOUT_S = 0.05; "
             "s.Engine = lambda **kw: threading.Event().wait(); "
             "s._boot()"],
            cwd=srv.ROOT, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 3, result.stderr.decode(errors="replace"))
        self.assertIn(b"engine load exceeded", result.stderr)


if __name__ == "__main__":
    unittest.main()
