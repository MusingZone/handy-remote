"""Unit tests for voice_feedback.py: no network beyond loopback, no Tk windows."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import socketserver
import struct
import sys
import tempfile
import threading
import unittest
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import voice_feedback as vf  # noqa: E402


class Scratch(unittest.TestCase):
    """Each test gets its own Context rooted in a scratch folder next to this file."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(dir=HERE, prefix="scratch-")
        self.root = self._td.name
        self.appdata = os.path.join(self.root, "appdata")
        self.legacy = os.path.join(self.root, "legacy-state")
        os.makedirs(os.path.join(self.appdata, vf.APP_IDS[0]))
        self.make_ctx({})

    def tearDown(self):
        vf.CTX = None
        self._td.cleanup()

    def make_ctx(self, cfg: dict):
        cfg = vf.deep_merge({"language": "en", "reports_dir": os.path.join(self.root, "fb")}, cfg)
        vf.CTX = vf.Context(cfg, state_dir=os.path.join(self.root, "state"), legacy_state_dir=self.legacy,
                            appdata=self.appdata)
        return vf.CTX


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).isoformat(timespec="milliseconds")


class AuditMatching(unittest.TestCase):
    T = 1_800_000_000.0

    def recs(self):
        return [
            {"ts": iso(self.T - 300), "kind": "unit", "text": "old unit"},
            {"ts": iso(self.T - 299), "kind": "stream", "text": "something else entirely"},
            {"ts": iso(self.T - 5), "kind": "preview", "text": "ignored"},
            {"ts": iso(self.T - 4), "kind": "unit", "text": "hello open"},
            {"ts": iso(self.T - 2), "kind": "unit", "text": "vino world"},
            {"ts": iso(self.T - 1), "kind": "stream", "text": "hello open vino world"},
            {"ts": iso(self.T + 30), "kind": "run", "text": "hello open vino word"},
        ]

    def test_best_match_and_preceding_units(self):
        r = vf.match_audit(self.recs(), self.T, "hello open vino world")
        self.assertTrue(r["matched"])
        self.assertEqual(r["final"]["kind"], "stream")
        self.assertEqual([u["text"] for u in r["units"]], ["hello open", "vino world"])

    def test_time_breaks_ties_and_window(self):
        recs = [{"ts": iso(self.T + 400), "kind": "run", "text": "same"},
                {"ts": iso(self.T + 10), "kind": "run", "text": "same"},
                {"ts": iso(self.T + 700), "kind": "run", "text": "same"}]
        r = vf.match_audit(recs, self.T, "same")
        self.assertIs(r["final"], recs[1])
        self.assertEqual(r["units"], [])
        self.assertFalse(vf.match_audit(recs[2:], self.T, "same")["matched"])

    def test_no_similar_text(self):
        self.assertFalse(vf.match_audit(self.recs(), self.T, "zzzz qqqq xxxx")["matched"])
        self.assertFalse(vf.match_audit([{"kind": "run"}, "junk", {"ts": "bad", "kind": "run"}], self.T, "a")["matched"])


class Validation(Scratch):
    def run_validate(self, changes, lex=None):
        return vf.validate(changes, lex or {"hotwords": ["OpenVINO"], "replacements": [{"from": "x", "to": "y"}]},
                           {"recognized": "open vino rocks"}, corpus=["open vino", "nothing"])

    def test_stream_bounds(self):
        out = self.run_validate([
            {"op": "set_stream", "key": "comma_gap_s", "value": 1.2},
            {"op": "set_stream", "key": "comma_gap_s", "value": 3.5},
            {"op": "set_stream", "key": "comma_gap_s", "value": 0.29},
            {"op": "set_stream", "key": "grammar_punct", "value": True},
            {"op": "set_stream", "key": "grammar_punct", "value": 0},
            {"op": "set_stream", "key": "no_such_key", "value": 1},
            {"op": "set_stream", "key": "unit_min_s", "value": "4"},
        ])
        self.assertEqual([c["invalid"] is None for c in out], [True, False, False, False, True, False, False])
        self.assertIn("out of range", out[1]["invalid"])

    def test_lexicon_ops(self):
        out = self.run_validate([
            {"op": "add_hotword", "value": "openvino"},
            {"op": "add_hotword", "value": " Kubernetes "},
            {"op": "remove_hotword", "value": "missing"},
            {"op": "add_replacement", "from": r"(?i)\bopen ?vino\b", "to": "OpenVINO"},
            {"op": "add_replacement", "from": "a*", "to": "b"},
            {"op": "add_replacement", "from": "(", "to": "b"},
            {"op": "backlog", "value": ""},
            {"op": "bogus"},
            "not a dict",
        ])
        self.assertEqual(len(out), 8)
        self.assertEqual(out[0]["invalid"], "already present")
        self.assertIsNone(out[1]["invalid"])
        self.assertEqual(out[1]["value"], "Kubernetes")
        self.assertEqual(out[2]["invalid"], "not present")
        self.assertIsNone(out[3]["invalid"])
        self.assertEqual(out[3]["preview"], "OpenVINO rocks")
        self.assertEqual((out[3]["corpus_hits"], out[3]["corpus_size"]), (1, 2))
        self.assertEqual(out[4]["invalid"], "matches the empty string")
        self.assertIn("invalid regular expression", out[5]["invalid"])
        self.assertEqual(out[6]["invalid"], "empty")
        self.assertEqual(out[7]["invalid"], "unknown operation")

    def test_localized_errors(self):
        self.make_ctx({"language": "zh-CN"})
        out = self.run_validate([{"op": "set_stream", "key": "nope", "value": 1}])
        self.assertEqual(out[0]["invalid"], "未知参数")


class PendingState(Scratch):
    def add_report(self, rid, proposal=True, decision=False, origin=None):
        d = os.path.join(vf.ctx().reports, rid)
        vf.write_json(os.path.join(d, "report.json"), {"id": rid, "origin": origin or vf.PC, "recognized": "x"})
        if proposal:
            vf.write_json(os.path.join(d, "proposal.json"), {"changes": []})
        if decision:
            vf.write_json(os.path.join(d, "decision.json"), {"kind": "reject"})

    def read_pending(self):
        with open(vf.ctx().pending, encoding="utf-8") as f:
            return json.load(f)

    def test_pending_json_contract(self):
        self.add_report("r1")
        self.add_report("r2", decision=True)
        self.add_report("r3", proposal=False)
        self.add_report("r4", origin="ANOTHER-PC")
        vf.update_pending(analyzing=True)
        p = self.read_pending()
        self.assertEqual(p["count"], 1)
        self.assertIs(p["analyzing"], True)
        self.assertEqual(p["script"], vf.SCRIPT)
        self.assertTrue(os.path.isabs(p["script"]) and os.path.isfile(p["script"]))
        self.assertFalse([n for n in os.listdir(vf.ctx().state) if n.endswith(".tmp")])
        with open(vf.ctx().pending, "rb") as f:
            raw = f.read()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r\n", raw)

    def test_notices_count_and_legacy_fallback(self):
        vf.write_json(os.path.join(self.legacy, "notices.json"), [{"title": "old", "text": "t"}])
        vf.update_pending()
        self.assertEqual(self.read_pending()["count"], 1)
        self.assertFalse(os.path.exists(os.path.join(self.legacy, "pending.json")))
        vf.notify("title", "text")
        self.assertEqual(len(vf.read_json(vf.ctx().notices)), 2)
        p = self.read_pending()
        self.assertEqual((p["count"], p["analyzing"]), (2, False))

    def test_pending_reports_and_target(self):
        self.add_report("a", proposal=False)
        self.add_report("b", proposal=False)
        self.assertEqual([os.path.basename(d) for d, _ in vf.pending_reports()], ["a", "b"])
        target = os.path.join(vf.ctx().reports, "b")
        self.assertEqual([os.path.basename(d) for d, _ in vf.pending_reports(target)], ["b"])


class ConnectionConfig(Scratch):
    def write_rc(self, data):
        vf.write_json(os.path.join(self.appdata, vf.APP_IDS[0], "remote_connection.json"), data)

    def test_defaults_tcp(self):
        s = vf.connection_settings()
        self.assertEqual((s["host"], s["server_port"]), ("127.0.0.1", vf.DEFAULT_PORT))
        self.assertIsNone(vf.connect_argv(s))

    def test_client_file_ssh(self):
        self.write_rc({"enabled": True, "ssh": "C:/x/ssh.exe", "ssh_args": ["-p", "2222", "-o", "BatchMode=yes"],
                       "target": "me@server", "server_port": 50000})
        s = vf.connection_settings()
        self.assertEqual(vf.connect_argv(s), ["C:/x/ssh.exe", "-p", "2222", "-o", "BatchMode=yes",
                                              "-W", "127.0.0.1:50000", "me@server"])

    def test_null_target_means_tcp(self):
        self.write_rc({"enabled": False, "ssh": "C:/x/ssh.exe", "ssh_args": [], "target": None, "server_port": 47000})
        s = vf.connection_settings()
        self.assertIsNone(vf.connect_argv(s))
        self.assertEqual(s["server_port"], 47000)

    def test_config_overrides(self):
        self.write_rc({"ssh": "C:/x/ssh.exe", "target": "me@server", "server_port": 50000})
        self.make_ctx({"connection": {"target": "", "host": "10.0.0.5", "server_port": 47999}})
        s = vf.connection_settings()
        self.assertIsNone(vf.connect_argv(s))
        self.assertEqual((s["host"], s["server_port"]), ("10.0.0.5", 47999))

    def test_settings_file_override_and_validation(self):
        alt = os.path.join(self.root, "alt.json")
        vf.write_json(alt, {"server_port": 0})
        self.make_ctx({"connection": {"settings_file": alt}})
        with self.assertRaises(ValueError):
            vf.connection_settings()
        vf.write_json(alt, {"ssh_args": "-p 22"})
        with self.assertRaises(ValueError):
            vf.connection_settings()

    def test_config_file_loading(self):
        path = os.path.join(self.root, "feedback.toml")
        with open(path, "w", encoding="utf-8") as f:
            f.write('language = "zh-CN"\n[analyzer]\nbackend = "off"\n[analyzer.openai]\nmodel = "m"\n')
        cfg = vf.load_config(path)
        self.assertEqual(cfg["analyzer"]["backend"], "off")
        self.assertEqual(cfg["analyzer"]["openai"]["model"], "m")
        self.assertEqual(cfg["analyzer"]["openai"]["api_key_env"], "OPENAI_API_KEY")
        self.assertEqual(vf.load_config(os.path.join(self.root, "missing.toml")), vf.DEFAULTS)


# ---------------------------------------------------------------- fake worker-protocol server

def _read_frame(f):
    head = f.read(4)
    if len(head) < 4:
        return None
    msg = json.loads(f.read(struct.unpack("<I", head)[0]))
    payload = f.read(struct.unpack("<I", f.read(4))[0])
    return msg, payload


class FakeServer(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def __init__(self, records):
        super().__init__(("127.0.0.1", 0), FakeHandler)
        self.records = records
        self.lexicon, self.raw = {}, b""
        self.store({"hotwords": ["OpenVINO"], "replacements": [], "stream": {}})
        self.puts = 0
        self.race = 0  # puts to reject as concurrent edits

    def store(self, data):
        self.raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.lexicon = data

    def state(self):
        return {"data": self.lexicon, "sha256": hashlib.sha256(self.raw).hexdigest()}


class FakeHandler(socketserver.StreamRequestHandler):
    def handle(self):
        srv: FakeServer = self.server
        while True:
            fr = _read_frame(self.rfile)
            if fr is None:
                return
            msg, payload = fr
            k = next(iter(msg))
            body = msg[k] or {}
            if k == "AuditQuery":
                ts, win = float(body["ts"]), float(body.get("window_s", 600))
                recs = [r for r in srv.records
                        if abs(dt.datetime.fromisoformat(r["ts"]).timestamp() - ts) <= win]
                resp = {"Audit": {"records": recs}}
            elif k == "LexiconGet":
                resp = {"Lexicon": srv.state()}
            elif k == "LexiconPut":
                if srv.race:
                    srv.race -= 1
                    srv.store(dict(srv.lexicon, hotwords=srv.lexicon["hotwords"] + ["Concurrent"]))
                if body.get("base_sha256") != srv.state()["sha256"]:
                    resp = {"BackendError": "ASR server: the lexicon changed on the server meanwhile; read it again"}
                else:
                    srv.puts += 1
                    srv.store(body["data"])
                    resp = {"Lexicon": srv.state()}
            elif k == "Hello":
                resp = {"Hello": {"devices": None}}
            elif k == "Load":
                resp = {"Loaded": {}}
            elif k == "Run":
                vocab = ",".join(body["options"]["vocabulary"])
                resp = {"Transcript": {"text": f"{len(payload) // 4} samples, vocab={vocab}"}}
            else:
                resp = {"Error": f"unknown request {k}"}
            j = json.dumps(resp, ensure_ascii=False).encode("utf-8")
            self.wfile.write(struct.pack("<I", len(j)) + j + struct.pack("<I", 0))
            self.wfile.flush()


class ProtocolRoundTrip(Scratch):
    T = 1_800_000_000.0

    def setUp(self):
        super().setUp()
        self.srv = FakeServer([
            {"ts": iso(self.T - 3), "kind": "unit", "text": "你好"},
            {"ts": iso(self.T - 2), "kind": "stream", "text": "你好世界"},
            {"ts": iso(self.T - 5000), "kind": "stream", "text": "你好世界"},
        ])
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.make_ctx({"connection": {"server_port": self.srv.server_address[1], "timeout_s": 10}})

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def test_lexicon_audit_and_run(self):
        with vf.Worker() as w:
            st = w.lexicon_get()
            self.assertEqual(st["data"]["hotwords"], ["OpenVINO"])
            new = vf.apply_to_lexicon(st["data"], [{"op": "add_hotword", "value": "Kubernetes"},
                                                   {"op": "set_stream", "key": "comma_gap_s", "value": 1.2}])
            st2 = w.lexicon_put(new, st["sha256"])
            self.assertEqual(st2["data"]["hotwords"], ["OpenVINO", "Kubernetes"])
            self.assertEqual(st2["data"]["stream"], {"comma_gap_s": 1.2})
            with self.assertRaises(vf.ServerError):
                w.lexicon_put(new, st["sha256"])  # stale base
            with self.assertRaises(vf.ServerError):
                w.call({"Bogus": {}})
            a = vf.audit_records(w, self.T, "你好世界")
            self.assertTrue(a["matched"])
            self.assertEqual(a["final"]["ts"], iso(self.T - 2))
            self.assertEqual([u["text"] for u in a["units"]], ["你好"])
            self.assertEqual(w.transcribe(b"\0" * 16, ["Foo"]), "4 samples, vocab=Foo")

    def test_push_retries_after_concurrent_edit(self):
        self.srv.race = 1
        res = vf.apply_changes([{"op": "add_hotword", "value": "Kubernetes"},
                                {"op": "backlog", "value": "punctuation idea"}])
        self.assertTrue(res["deployed"])
        self.assertEqual(self.srv.lexicon["hotwords"], ["OpenVINO", "Concurrent", "Kubernetes"])
        self.assertEqual(self.srv.puts, 1)
        self.assertTrue(os.listdir(vf.ctx().lex_history))
        with open(vf.ctx().backlog, encoding="utf-8") as f:
            self.assertIn("punctuation idea", f.read())

    def test_unreachable_server_queues_notice(self):
        self.make_ctx({"connection": {"server_port": 1, "timeout_s": 2}})
        res = vf.apply_changes([{"op": "add_hotword", "value": "X"}])
        self.assertFalse(res["deployed"])
        self.assertEqual(len(vf.read_json(vf.ctx().notices)), 1)


class WavConversion(Scratch):
    def test_stereo_pcm16(self):
        path = os.path.join(self.root, "a.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(struct.pack("<4h", 16384, 0, -32768, -32768))
        vals = struct.unpack("<2f", vf.wav_f32(path))
        self.assertAlmostEqual(vals[0], 0.25)
        self.assertAlmostEqual(vals[1], -1.0)


if __name__ == "__main__":
    unittest.main()
