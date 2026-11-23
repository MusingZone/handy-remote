"""Handy's worker protocol over TCP, with the models replaced by fakes (no model files needed)."""
import json
import socket
import struct
import threading
import unittest

import numpy as np

import _path  # noqa: F401
import asr_server as srv

SR = srv.SR


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


if __name__ == "__main__":
    unittest.main()
