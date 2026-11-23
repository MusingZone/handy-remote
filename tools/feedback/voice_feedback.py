"""Dictation feedback loop for Handy Remote and its speech server.

  report    pick a recent dictation from Handy's history, type the correct text; a report folder is
            created (audio + texts) and analysis starts in the background. Optionally replaces the
            just-typed text in the original window with the corrected version.
  analyze   [report-dir|report-id] turn new reports into lexicon / tuning proposals with the configured
            analyzer (Copilot CLI, an OpenAI-compatible API, or off). Never opens a window: it only
            updates pending.json, which Handy Remote's tray icon shows (blue dot = analysing,
            orange dot = suggestions or notices waiting).
  review    show pending proposals; apply the accepted ones to the server lexicon, then re-transcribe
            the reported audio to verify. Only a worse result or a failure leaves a notice.
  status    list reports and their state
  guide     open the user guide

All server access goes through the worker protocol on one connection (TCP, or `ssh -W` when the client's
remote_connection.json names an SSH target). Python 3.10+, stdlib only; numpy is used when available.
"""
from __future__ import annotations

import argparse
import array
import copy
import datetime as dt
import difflib
import json
import locale
import os
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import wave

APP_IDS = ("com.musingzone.handy-remote", "com.pais.handy")
STATE_NAME = "handy-remote"
LEGACY_STATE_NAME = "voice-asr"
DEFAULT_PORT = 47821
SCRIPT = os.path.abspath(__file__)
HERE = os.path.dirname(SCRIPT)
PC = os.environ.get("COMPUTERNAME", socket.gethostname()).upper()

STREAM_BOUNDS = {"vad_min_silence_s": (0.2, 1.5), "unit_min_s": (1.0, 10.0),
                 "unit_pause_s": (0.5, 3.0), "comma_gap_s": (0.3, 3.0),
                 "preview_interval_s": (0.0, 5.0), "preview_max_s": (3.0, 25.0),
                 "unit_context_chars": (0, 200), "draft_para_tail": (0, 1),
                 "preview_device": (0, 1), "preview_min_s": (0.5, 3.0), "npu_yield": (0, 1), "npu_rollback": (0, 20),
                 "para_draft": (0, 1), "spec_draft": (0, 1), "prefix_cache": (0, 1),
                 "grammar_punct": (0, 1), "drop_fillers": (0, 1), "stop_gap_s": (0.8, 5.0),
                 "unit_lookback_s": (0.0, 20.0), "soft_vad_threshold": (0.0, 0.5), "head_check": (0, 1)}
TAGS = ["term", "homophone", "punctuation", "missing_extra", "mixed_language", "latency", "other"]
NO_WINDOW = 0x08000000
DETACHED = 0x00000008

DEFAULTS: dict = {
    "reports_dir": "",
    "language": "auto",
    "handy_dir": "",
    "connection": {},
    "analyzer": {
        "backend": "copilot",
        "timeout_s": 600,
        "copilot": {"path": "", "model": "", "extra_args": []},
        "openai": {"base_url": "https://api.openai.com/v1", "model": "", "api_key_env": "OPENAI_API_KEY",
                   "api_key_credential": "", "temperature": 0.2, "json_mode": True},
    },
}

# ---------------------------------------------------------------- UI strings

STRINGS = {
    "en": {
        "app": "Dictation feedback",
        "history_empty": "Handy's history is empty: there is nothing to report.",
        "history_error": "Could not read Handy's history: {e}",
        "report_title": "Dictation feedback",
        "pick": "Which dictation was wrong:",
        "correct": "Correct it (only fix the wrong parts; submitting it unchanged is fine too):",
        "note": "Note (optional):",
        "replace": "After submitting, replace the just-typed text with the corrected version "
                   "(the caret must still be at its end; Ctrl+Z undoes it)",
        "hint": "No need to wait: the analysis runs in the background (about a minute). The tray icon shows a "
                "blue dot while analysing and an orange dot when suggestions are ready; click it to review. "
                "See \"Dictation feedback guide\" in the tray menu.",
        "hint_off": "Automatic analysis is turned off (analyzer backend \"off\"): the report is only saved.",
        "submit": "Submit (Ctrl+Enter)",
        "cancel": "Cancel (Esc)",
        "clip_busy": "The clipboard is busy: the text was not replaced.",
        "window_closed": "The original window is gone; the corrected text is on the clipboard.",
        "no_focus": "Could not switch back to the original window; the corrected text is on the clipboard "
                    "(paste it with Ctrl+V).",
        "review_title": "Dictation improvement suggestions ({n} reports)",
        "recognized": "Recognized: {v}",
        "expected": "Expected: {v}",
        "note_l": "Note: {v}",
        "analysis": "Analysis: {v}",
        "no_changes": "(no suggestion that can be applied automatically)",
        "apply_sel": "Apply selected",
        "reject_all": "Reject all",
        "later": "Later",
        "d_add_hotword": "Add hotword: {v}",
        "d_remove_hotword": "Remove hotword: {v}",
        "d_add_replacement": "Replacement: {f}  →  {t}  (matches {h}/{n} past dictations)",
        "d_effect": "Result: {v}",
        "d_set_stream": "Stream setting: {k} = {v}",
        "d_backlog": "Backlog (needs a code change or manual work): {v}",
        "d_unknown": "Unknown suggestion: {v}",
        "d_why": "Why: {v}",
        "d_invalid": "⚠ Cannot apply: {v}",
        "e_empty_long": "empty or too long",
        "e_exists": "already present",
        "e_missing": "not present",
        "e_regex": "invalid regular expression: {e}",
        "e_empty_match": "matches the empty string",
        "e_unknown_key": "unknown setting",
        "e_range": "out of range {r}",
        "e_empty": "empty",
        "e_unknown_op": "unknown operation",
        "analysis_failed": "Automatic analysis failed; the report is kept and can be analysed manually.",
        "deploy_failed": "The lexicon change could not be sent to the server: {e}",
        "verify_title": "Changes applied, but verification found a problem",
        "verify_failed": "{rid}: re-transcription failed ({e})",
        "verify_empty": "{rid}: re-transcription returned nothing, so the change could not be verified.",
        "verify_worse": "{rid}: after the change the result is further from your correction "
                        "(similarity {a:.0%} → {b:.0%}).\nNew result: {new}",
        "notices_title": "Dictation feedback: {n} notices",
        "ok": "OK",
        "tag_term": "Term / name",
        "tag_homophone": "Homophone",
        "tag_punctuation": "Punctuation",
        "tag_missing_extra": "Missing / extra words",
        "tag_mixed_language": "Mixed languages",
        "tag_latency": "Latency",
        "tag_other": "Other",
        "out_language": "English",
    },
    "zh-CN": {
        "app": "语音反馈",
        "history_empty": "Handy 历史记录为空，没有可反馈的内容。",
        "history_error": "无法读取 Handy 历史记录：{e}",
        "report_title": "语音识别反馈",
        "pick": "哪一条识别有问题：",
        "correct": "改成正确内容（只改错的地方即可；也可以不改直接提交）：",
        "note": "备注（可选）：",
        "replace": "提交后把刚才上屏的文字替换为改正版（需光标仍在那段文字末尾；不对可 Ctrl+Z）",
        "hint": "提交后无需等待：后台约 1 分钟分析，托盘图标先出现蓝点（分析中），有建议时变橙点；"
                "有空时单击图标审阅、勾选应用即可。详见托盘菜单中的使用说明。",
        "hint_off": "自动分析已关闭（analyzer backend = \"off\"）：只保存报告。",
        "submit": "提交 (Ctrl+Enter)",
        "cancel": "取消 (Esc)",
        "clip_busy": "剪贴板被占用，未能替换。",
        "window_closed": "原窗口已关闭；正确文本已复制到剪贴板。",
        "no_focus": "未能切回原窗口；正确文本已复制到剪贴板，可手动 Ctrl+V。",
        "review_title": "语音识别改进建议（{n} 条反馈）",
        "recognized": "识别：{v}",
        "expected": "期望：{v}",
        "note_l": "备注：{v}",
        "analysis": "分析：{v}",
        "no_changes": "（没有可自动应用的建议）",
        "apply_sel": "应用所选",
        "reject_all": "全部拒绝",
        "later": "稍后再看",
        "d_add_hotword": "加入热词：{v}",
        "d_remove_hotword": "移除热词：{v}",
        "d_add_replacement": "替换规则：{f}  →  {t}（历史记录命中 {h}/{n}）",
        "d_effect": "效果：{v}",
        "d_set_stream": "流式参数：{k} = {v}",
        "d_backlog": "记入待办（需改代码或人工处理）：{v}",
        "d_unknown": "未知建议：{v}",
        "d_why": "理由：{v}",
        "d_invalid": "⚠ 无法应用：{v}",
        "e_empty_long": "空或过长",
        "e_exists": "已存在",
        "e_missing": "不存在",
        "e_regex": "正则无效：{e}",
        "e_empty_match": "会匹配空串",
        "e_unknown_key": "未知参数",
        "e_range": "超出范围 {r}",
        "e_empty": "空",
        "e_unknown_op": "未知操作",
        "analysis_failed": "自动分析失败，已保留报告，可手动分析。",
        "deploy_failed": "词库改动未能发送到服务端：{e}",
        "verify_title": "改进已应用，但验证发现问题",
        "verify_failed": "{rid}：重新识别失败（{e}）",
        "verify_empty": "{rid}：重新识别结果为空，无法验证改动效果。",
        "verify_worse": "{rid}：改动后与你的修正版更不接近了（相似度 {a:.0%} → {b:.0%}）。\n新结果：{new}",
        "notices_title": "语音反馈：{n} 条提醒",
        "ok": "知道了",
        "tag_term": "术语/专有名词",
        "tag_homophone": "同音字",
        "tag_punctuation": "标点/断句",
        "tag_missing_extra": "漏字/多字",
        "tag_mixed_language": "中英混排",
        "tag_latency": "延迟/卡顿",
        "tag_other": "其他",
        "out_language": "Simplified Chinese",
    },
}


def system_language() -> str:
    """'zh-CN' when the Windows UI language (or the locale elsewhere) is Chinese, else 'en'."""
    try:
        import ctypes
        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        return "zh-CN" if langid & 0x3FF == 0x04 else "en"
    except Exception:
        loc = (locale.getlocale()[0] or os.environ.get("LANG") or "").lower()
        return "zh-CN" if loc.startswith("zh") or loc.startswith("chinese") else "en"


def resolve_language(value: str | None) -> str:
    v = (value or "auto").strip()
    if v.lower() in ("zh", "zh-cn", "zh_cn", "zh-hans"):
        return "zh-CN"
    if v.lower().startswith("en"):
        return "en"
    return system_language()


# ---------------------------------------------------------------- configuration / context

def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def appdata_dir() -> str:
    return os.environ.get("APPDATA") or os.path.expanduser("~")


def local_appdata_dir() -> str:
    return os.environ.get("LOCALAPPDATA") or appdata_dir()


def config_path() -> str:
    return os.environ.get("HANDY_REMOTE_FEEDBACK_CONFIG") or os.path.join(appdata_dir(), STATE_NAME, "feedback.toml")


def load_config(path: str | None = None) -> dict:
    path = path or config_path()
    if not os.path.exists(path):
        return copy.deepcopy(DEFAULTS)
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        try:
            import tomli as tomllib  # type: ignore
        except ModuleNotFoundError:
            log(f"cannot read {path}: needs Python 3.11+ (or the tomli package); using defaults")
            return copy.deepcopy(DEFAULTS)
    with open(path, "rb") as f:
        return deep_merge(DEFAULTS, tomllib.load(f))


def _expand(p) -> str:
    return os.path.expandvars(os.path.expanduser(p)) if isinstance(p, str) and p.strip() else ""


class Context:
    """Every path and setting the tool uses; tests build their own."""

    def __init__(self, cfg: dict | None = None, *, state_dir: str | None = None,
                 legacy_state_dir: str | None = None, appdata: str | None = None):
        self.cfg = deep_merge(DEFAULTS, cfg or {})
        self.appdata = appdata or appdata_dir()
        local = local_appdata_dir()
        self.state = state_dir or os.path.join(local, STATE_NAME)
        self.legacy_state = legacy_state_dir if legacy_state_dir is not None else os.path.join(local, LEGACY_STATE_NAME)
        self.feedback = _expand(self.cfg.get("reports_dir")) or os.path.join(self.state, "feedback")
        self.reports = os.path.join(self.feedback, "reports")
        self.lex_history = os.path.join(self.feedback, "lexicon-history")
        self.changelog = os.path.join(self.feedback, "changelog.jsonl")
        self.backlog = os.path.join(self.feedback, "backlog.md")
        self.log = os.path.join(self.state, "feedback.log")
        self.pending = os.path.join(self.state, "pending.json")
        self.notices = os.path.join(self.state, "notices.json")
        self.lang = resolve_language(self.cfg.get("language"))
        self._handy = _expand(self.cfg.get("handy_dir"))

    @property
    def handy(self) -> str:
        """Handy's app-data dir: Handy Remote first, then upstream Handy."""
        if self._handy:
            return self._handy
        dirs = [os.path.join(self.appdata, a) for a in APP_IDS]
        for d in dirs:
            if os.path.exists(os.path.join(d, "history.db")):
                return d
        return next((d for d in dirs if os.path.isdir(d)), dirs[0])

    def state_file(self, name: str) -> str:
        """State file to read: the current dir, or the old voice-asr dir when only that one has it."""
        p = os.path.join(self.state, name)
        if not os.path.exists(p) and self.legacy_state:
            old = os.path.join(self.legacy_state, name)
            if os.path.exists(old):
                return old
        return p


CTX: Context | None = None


def ctx() -> Context:
    global CTX
    if CTX is None:
        try:
            cfg = load_config()
        except Exception as e:  # a broken config must not break the tray contract
            cfg = copy.deepcopy(DEFAULTS)
            CTX = Context(cfg)
            log(f"config error ({config_path()}): {e}; using defaults")
            return CTX
        CTX = Context(cfg)
    return CTX


def t(key: str, **kw) -> str:
    s = STRINGS.get(ctx().lang, STRINGS["en"]).get(key) or STRINGS["en"].get(key, key)
    return s.format(**kw) if kw else s


# ---------------------------------------------------------------- small helpers

def log(msg: str):
    try:
        c = CTX or Context()
        os.makedirs(c.state, exist_ok=True)
        with open(c.log, "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- Handy history

def history(limit=8):
    db = os.path.join(ctx().handy, "history.db")
    if not os.path.exists(db):
        raise FileNotFoundError(db)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        rows = con.execute("select id, file_name, timestamp, transcription_text, post_processed_text, language "
                           "from transcription_history order by id desc limit ?", (limit,)).fetchall()
    finally:
        con.close()
    return [dict(zip(["id", "file_name", "timestamp", "text", "post_processed", "language"], r)) for r in rows]


def corpus_texts(limit=2000):
    try:
        return [h["text"] for h in history(limit) if h["text"]]
    except (sqlite3.Error, OSError):
        return []


def handy_vocab() -> list[str]:
    s = read_json(os.path.join(ctx().handy, "settings_store.json"), {}) or {}
    s = s.get("settings", s) if isinstance(s, dict) else {}
    return [w for w in s.get("custom_words", []) if isinstance(w, str)]


# ---------------------------------------------------------------- server connection

class ServerError(RuntimeError):
    pass


def remote_connection_file(c: Context) -> str:
    over = _expand((c.cfg.get("connection") or {}).get("settings_file"))
    if over:
        return over
    paths = [os.path.join(c.appdata, a, "remote_connection.json") for a in APP_IDS]
    return next((p for p in paths if os.path.exists(p)), paths[0])


def connection_settings(c: Context | None = None) -> dict:
    """remote_connection.json (written by the client), overridden key by key by [connection] in feedback.toml."""
    c = c or ctx()
    s = {"enabled": False, "ssh": None, "ssh_args": [], "target": None, "server_port": DEFAULT_PORT,
         "host": "127.0.0.1", "timeout_s": 60}
    rc = read_json(remote_connection_file(c), {}) or {}
    if isinstance(rc, dict):
        for k in ("enabled", "ssh", "ssh_args", "target", "server_port"):
            if k in rc:
                s[k] = rc[k]
    for k, v in (c.cfg.get("connection") or {}).items():
        if k != "settings_file":
            s[k] = v
    s["ssh"] = _expand(s["ssh"]) or None
    s["target"] = s["target"].strip() or None if isinstance(s["target"], str) else None
    if not isinstance(s["ssh_args"], list) or not all(isinstance(a, str) for a in s["ssh_args"]):
        raise ValueError("ssh_args must be a list of strings")
    s["ssh_args"] = [os.path.expandvars(a) for a in s["ssh_args"]]
    port = s["server_port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise ValueError(f"invalid server_port {port!r}")
    s["host"] = str(s.get("host") or "127.0.0.1")
    s["timeout_s"] = float(s.get("timeout_s") or 60)
    return s


def connect_argv(s: dict) -> list[str] | None:
    """ssh command line for an SSH connection, None for direct TCP."""
    if s.get("ssh") and s.get("target"):
        return [s["ssh"], *s["ssh_args"], "-W", f"{s['host']}:{s['server_port']}", s["target"]]
    return None


class Worker:
    """Client for the worker protocol: u32 LE json length + json + u32 LE payload length + payload."""

    def __init__(self, settings: dict | None = None):
        s = settings or connection_settings()
        self.timeout = s["timeout_s"]
        self.proc = self.sock = None
        argv = connect_argv(s)
        if argv:
            self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, creationflags=NO_WINDOW if os.name == "nt" else 0)
            self.rd, self.wr = self.proc.stdout, self.proc.stdin
        else:
            self.sock = socket.create_connection((s["host"], s["server_port"]), timeout=self.timeout)
            self.rd, self.wr = self.sock.makefile("rb"), self.sock.makefile("wb")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _read(self, n: int) -> bytes:
        b = self.rd.read(n) if n else b""
        if b is None or len(b) < n:
            raise ConnectionError("server closed the connection")
        return b

    def call(self, msg, payload: bytes = b"", timeout: float | None = None):
        # ssh pipes cannot time out by themselves: a watchdog kills the tunnel instead
        timer = None
        if self.proc:
            timer = threading.Timer(timeout or self.timeout, self.proc.kill)
            timer.daemon = True
            timer.start()
        elif timeout:
            self.sock.settimeout(timeout)
        try:
            j = json.dumps(msg, ensure_ascii=False).encode("utf-8")
            self.wr.write(struct.pack("<I", len(j)) + j + struct.pack("<I", len(payload)) + payload)
            self.wr.flush()
            r = json.loads(self._read(struct.unpack("<I", self._read(4))[0]).decode("utf-8"))
            self._read(struct.unpack("<I", self._read(4))[0])
        except (OSError, ValueError, struct.error) as e:
            raise ConnectionError(f"server connection failed: {e}") from e
        finally:
            if timer:
                timer.cancel()
            elif timeout and self.sock:
                self.sock.settimeout(self.timeout)
        if isinstance(r, dict) and ("Error" in r or "BackendError" in r):
            raise ServerError(str(r.get("Error") or r.get("BackendError")))
        return r

    def expect(self, msg, key: str, payload: bytes = b"", timeout: float | None = None):
        r = self.call(msg, payload, timeout)
        if not isinstance(r, dict) or key not in r:
            raise ServerError(f"unexpected reply: {json.dumps(r, ensure_ascii=False)[:300]}")
        return r[key]

    def lexicon_get(self) -> dict:
        return self.expect({"LexiconGet": {}}, "Lexicon")

    def lexicon_put(self, data: dict, base_sha256: str) -> dict:
        return self.expect({"LexiconPut": {"data": data, "base_sha256": base_sha256}}, "Lexicon")

    def audit_query(self, ts: float, window_s: float = 600) -> list:
        return self.expect({"AuditQuery": {"ts": ts, "window_s": window_s}}, "Audit").get("records") or []

    def transcribe(self, pcm_f32: bytes, vocab: list[str]) -> str:
        self.call({"Hello": {"list_devices": False}}, timeout=120)
        self.call({"Load": {"path": "feedback.gguf", "backend": "Cpu", "device": "Auto"}}, timeout=240)
        return self.expect({"Run": {"options": {"vocabulary": vocab}}}, "Transcript", pcm_f32, timeout=600)["text"]

    def close(self):
        for f in (self.wr, self.rd):
            try:
                f.close()
            except (OSError, AttributeError):
                pass
        if self.sock:
            self.sock.close()
        if self.proc:
            self.proc.kill()


def match_audit(recs: list, ts: float, text: str) -> dict:
    """The 'stream'/'run' record that best matches this dictation (text similarity, then time), plus the
    'unit' records written just before it."""
    best, best_score = None, 0.3
    for i, r in enumerate(recs):
        if not isinstance(r, dict) or r.get("kind") not in ("stream", "run"):
            continue
        try:
            rt = dt.datetime.fromisoformat(r["ts"]).timestamp()
        except (KeyError, ValueError, TypeError):
            continue
        if abs(rt - ts) > 600:
            continue
        score = difflib.SequenceMatcher(None, r.get("text") or "", text or "").ratio() - abs(rt - ts) / 6000
        if score > best_score:
            best, best_score = i, score
    if best is None:
        return {"matched": False}
    units, j = [], best - 1
    while j >= 0 and isinstance(recs[j], dict) and recs[j].get("kind") == "unit":
        units.insert(0, recs[j])
        j -= 1
    return {"matched": True, "score": round(best_score, 3), "final": recs[best], "units": units}


def audit_records(w: Worker, ts: float, text: str) -> dict:
    try:
        return match_audit(w.audit_query(ts, 600), ts, text)
    except Exception as e:
        log(f"audit query failed: {e}")
        return {"matched": False, "error": str(e)[:300]}


def wav_f32(path: str) -> bytes:
    """16 kHz PCM16 wav -> float32 LE mono."""
    with wave.open(path, "rb") as w:
        if w.getframerate() != 16000 or w.getsampwidth() != 2:
            raise ValueError("expected a 16 kHz PCM16 wav")
        ch = w.getnchannels()
        raw = w.readframes(w.getnframes())
    try:
        import numpy as np
        a = np.frombuffer(raw, dtype="<i2").astype("<f4") / 32768.0
        if ch > 1:
            a = a[: len(a) // ch * ch].reshape(-1, ch).mean(axis=1)
        return a.astype("<f4").tobytes()
    except ImportError:
        s = array.array("h")
        s.frombytes(raw)
        if sys.byteorder != "little":
            s.byteswap()
        if ch > 1:
            vals = [sum(s[i:i + ch]) / (ch * 32768.0) for i in range(0, len(s) - ch + 1, ch)]
        else:
            vals = [x / 32768.0 for x in s]
        out = array.array("f", vals)
        if sys.byteorder != "little":
            out.byteswap()
        return out.tobytes()


def retranscribe(wav: str, vocab: list[str]) -> str:
    with Worker() as w:
        return w.transcribe(wav_f32(wav), vocab)


# ---------------------------------------------------------------- Windows UI helpers

def spawn(*args):
    """Run this script again, detached and windowless."""
    exe = sys.executable
    if exe.lower().endswith("python.exe") and os.path.exists(exe[:-10] + "pythonw.exe"):
        exe = exe[:-10] + "pythonw.exe"
    subprocess.Popen([exe, SCRIPT, *args], creationflags=NO_WINDOW | DETACHED, close_fds=True)


def ui_font() -> str:
    return "Microsoft YaHei UI" if ctx().lang == "zh-CN" else "Segoe UI"


def tk_root(title: str):
    """Per-monitor DPI aware Tk root (otherwise Windows stretches it: blurry text).
    Returns (root, s): s scales the few fixed pixel sizes."""
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    import tkinter as tk
    import tkinter.font as tkfont
    root = tk.Tk()
    s = max(1.0, root.winfo_fpixels("1i") / 96.0)
    root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
    for n in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont"):
        try:
            tkfont.nametofont(n).configure(family=ui_font(), size=10)
        except tk.TclError:
            pass
    root.title(title)
    return root, s


_WIN = None


def win():
    global _WIN
    if _WIN is None:
        import ctypes

        class W:
            u32 = ctypes.windll.user32
            k32 = ctypes.windll.kernel32
        W.u32.GetForegroundWindow.restype = ctypes.c_void_p
        W.u32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        W.u32.IsWindow.argtypes = [ctypes.c_void_p]
        W.u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        W.u32.SetClipboardData.restype = ctypes.c_void_p
        W.k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        W.k32.GlobalAlloc.restype = ctypes.c_void_p
        W.k32.GlobalLock.argtypes = [ctypes.c_void_p]
        W.k32.GlobalLock.restype = ctypes.c_void_p
        W.k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        _WIN = W
    return _WIN


def set_clipboard(text: str) -> bool:
    import ctypes
    w = win()
    data = text.encode("utf-16-le") + b"\0\0"
    for _ in range(10):
        if w.u32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return False
    try:
        w.u32.EmptyClipboard()
        h = w.k32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        p = w.k32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        w.k32.GlobalUnlock(h)
        return bool(w.u32.SetClipboardData(13, h))  # CF_UNICODETEXT
    finally:
        w.u32.CloseClipboard()


def _key(vk: int, up: bool = False, ext: bool = False):
    win().u32.keybd_event(vk, 0, (0x2 if up else 0) | (0x1 if ext else 0), 0)


def replace_last_paste(hwnd, old: str, new: str) -> str:
    """Select `old` backwards from the caret in the original window and paste `new` over it.
    Only correct while the caret still sits right after the dictated text; Ctrl+Z undoes it."""
    w = win()
    if not set_clipboard(new):
        return t("clip_busy")
    if not hwnd or not w.u32.IsWindow(hwnd):
        return t("window_closed")
    t0 = time.time()
    while any(w.u32.GetAsyncKeyState(vk) & 0x8000 for vk in (0x10, 0x11, 0x12, 0x0D)) and time.time() - t0 < 3:
        time.sleep(0.03)  # wait for Ctrl+Enter etc. to be released
    w.u32.SetForegroundWindow(hwnd)
    time.sleep(0.25)
    if w.u32.GetForegroundWindow() != hwnd:
        return t("no_focus")
    n = len(old.replace("\r\n", "\n"))
    _key(0x10)  # Shift
    for _ in range(n):
        _key(0x25, ext=True)  # Left
        _key(0x25, up=True, ext=True)
    _key(0x10, up=True)
    time.sleep(0.05)
    _key(0x11)  # Ctrl+V
    _key(0x56)
    _key(0x56, up=True)
    _key(0x11, up=True)
    return ""


# ---------------------------------------------------------------- report

def analyzer_backend() -> str:
    return str((ctx().cfg.get("analyzer") or {}).get("backend") or "copilot").strip().lower()


def cmd_report(_args):
    import tkinter as tk
    from tkinter import ttk
    try:
        entries = history(8)
    except Exception as e:
        notify(t("app"), t("history_error", e=e))
        return
    if not entries:
        notify(t("app"), t("history_empty"))
        return
    target = win().u32.GetForegroundWindow()  # where the dictation went (before our dialog exists)
    font = ui_font()
    root, s = tk_root(t("report_title"))
    root.attributes("-topmost", True)
    root.geometry("+{}+{}".format(root.winfo_screenwidth() // 2 - int(340 * s), root.winfo_screenheight() // 4))
    frm = ttk.Frame(root, padding=10)
    frm.grid(sticky="nsew")
    labels = [f"{dt.datetime.fromtimestamp(e['timestamp']):%m-%d %H:%M:%S}  {(e['text'] or '')[:40]}" for e in entries]
    ttk.Label(frm, text=t("pick")).grid(row=0, column=0, sticky="w")
    pick = ttk.Combobox(frm, values=labels, state="readonly", width=72)
    pick.current(0)
    pick.grid(row=1, column=0, sticky="we")
    ttk.Label(frm, text=t("correct")).grid(row=2, column=0, sticky="w", pady=(8, 0))
    box = tk.Text(frm, width=72, height=6, wrap="char", font=(font, 11), undo=True)
    box.grid(row=3, column=0, sticky="nsew")
    replace_var = tk.BooleanVar(value=True)

    def load(_e=None):
        box.delete("1.0", "end")
        box.insert("1.0", entries[pick.current()]["text"] or "")
        replace_var.set(pick.current() == 0)
    pick.bind("<<ComboboxSelected>>", load)
    load()
    tag_vars = {tag: tk.BooleanVar() for tag in TAGS}
    tf = ttk.Frame(frm)
    tf.grid(row=4, column=0, sticky="w", pady=6)
    for i, tag in enumerate(TAGS):
        ttk.Checkbutton(tf, text=t("tag_" + tag), variable=tag_vars[tag]).grid(row=0, column=i, padx=2)
    ttk.Label(frm, text=t("note")).grid(row=5, column=0, sticky="w")
    note = ttk.Entry(frm, width=72)
    note.grid(row=6, column=0, sticky="we")
    ttk.Checkbutton(frm, variable=replace_var, text=t("replace")).grid(row=7, column=0, sticky="w", pady=(6, 0))
    ttk.Label(frm, foreground="#666", wraplength=int(640 * s),
              text=t("hint_off") if analyzer_backend() == "off" else t("hint")
              ).grid(row=9, column=0, sticky="w", pady=(8, 0))
    result = {}

    def submit(_e=None):
        result.update(entry=entries[pick.current()], expected=box.get("1.0", "end").strip(),
                      tags=[tag for tag, v in tag_vars.items() if v.get()], note=note.get().strip(),
                      replace=replace_var.get())
        root.destroy()
        return "break"
    bf = ttk.Frame(frm)
    bf.grid(row=8, column=0, sticky="e", pady=(8, 0))
    ttk.Button(bf, text=t("submit"), command=submit).grid(row=0, column=0, padx=4)
    ttk.Button(bf, text=t("cancel"), command=root.destroy).grid(row=0, column=1)
    root.bind("<Control-Return>", submit)
    root.bind("<Escape>", lambda _e: root.destroy())
    root.after(150, lambda: (root.focus_force(), box.focus_set(), box.mark_set("insert", "end")))
    root.mainloop()
    if not result:
        return
    e = result["entry"]
    replaced = None
    if result["expected"] and result["expected"] != (e["text"] or ""):
        if result["replace"]:
            err = replace_last_paste(target, e["text"] or "", result["expected"])
            replaced = not err
            if err:
                notify(t("app"), err)
        else:
            set_clipboard(result["expected"])
    d = create_report(e, result["expected"], result["tags"], result["note"], replaced)
    if analyzer_backend() != "off":
        update_pending(analyzing=True)
        spawn("analyze", d)


def create_report(entry: dict, expected: str, tags: list, note: str, replaced=None) -> str:
    c = ctx()
    rid = f"{dt.datetime.now():%Y%m%d-%H%M%S}-{PC}"
    d = os.path.join(c.reports, rid)
    os.makedirs(d, exist_ok=True)
    wav = os.path.join(c.handy, "recordings", entry.get("file_name") or "")
    has_wav = bool(entry.get("file_name")) and os.path.isfile(wav)
    if has_wav:
        shutil.copyfile(wav, os.path.join(d, "audio.wav"))
    write_json(os.path.join(d, "report.json"), {
        "id": rid, "created": dt.datetime.now().isoformat(timespec="seconds"), "origin": PC,
        "handy": entry, "recognized": entry.get("text"), "expected": expected,
        "changed": expected != (entry.get("text") or ""), "tags": tags, "note": note,
        "audio": has_wav, "replaced_in_place": replaced})
    log(f"report {rid}")
    return d


# ---------------------------------------------------------------- analyze

def pending_reports(only: str | None = None):
    c = ctx()
    out = []
    if not os.path.isdir(c.reports):
        return out
    want = os.path.basename(os.path.normpath(only)) if only else None
    for rid in sorted(os.listdir(c.reports)):
        if want and rid != want:
            continue
        d = os.path.join(c.reports, rid)
        rep = read_json(os.path.join(d, "report.json"))
        if rep and rep.get("origin") == PC and not os.path.exists(os.path.join(d, "proposal.json")):
            out.append((d, rep))
    return out


def analyze_guide() -> str:
    with open(os.path.join(HERE, "ANALYZE.md"), encoding="utf-8") as f:
        return f.read()


def build_prompt(rep: dict, audit: dict, lex: dict) -> str:
    c = ctx()
    recent = []
    if os.path.exists(c.changelog):
        with open(c.changelog, encoding="utf-8") as f:
            for x in f.read().splitlines()[-15:]:
                try:
                    recent.append(json.loads(x))
                except ValueError:
                    pass
    fin = audit.get("final") or {}
    data = {
        "report": {k: rep.get(k) for k in ("recognized", "expected", "changed", "tags", "note")},
        "server_record": {"matched": audit.get("matched"), "kind": fin.get("kind"), "audio_s": fin.get("audio_s"),
                          "wall_s": fin.get("wall_s"),
                          "units": [{k: u.get(k) for k in ("dur", "gap", "draft", "raw", "text", "lang", "latency")}
                                    for u in audit.get("units", [])]},
        "lexicon": lex,
        "handy_custom_words": handy_vocab(),
        "recent_changes": recent,
    }
    return (analyze_guide() + f"\n\nWrite `diagnosis`, `why` and backlog text in {t('out_language')}.\n"
            "\n## Input\n\n```json\n" + json.dumps(data, ensure_ascii=False, indent=1) + "\n```\n")


def extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError(f"no JSON object in the reply: {(text or '')[-400:]}")
    obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("the reply is not a JSON object")
    return obj


def find_copilot(path: str = "") -> str:
    p = _expand(path)
    if p:
        if not os.path.exists(p):
            raise FileNotFoundError(f"copilot not found at {p}")
        return p
    found = shutil.which("copilot")
    if not found:
        raise FileNotFoundError("the Copilot CLI (copilot) is not on PATH; set analyzer.copilot.path")
    return found


def ask_copilot(prompt: str, acfg: dict) -> dict:
    cc = acfg.get("copilot") or {}
    exe = find_copilot(cc.get("path") or "")
    with tempfile.TemporaryDirectory(prefix="handy-feedback-") as td:
        pf = os.path.join(td, "prompt.md")
        with open(pf, "w", encoding="utf-8") as f:
            f.write(prompt)
        argv = [exe, "-p", f"Read {pf} and follow it exactly. Reply with the JSON object only.",
                "-s", "--no-ask-user", "--add-dir", td, "--deny-tool", "shell", "--deny-tool", "write"]
        if cc.get("model"):
            argv += ["--model", str(cc["model"])]
        argv += [str(a) for a in cc.get("extra_args") or []]
        r = subprocess.run(argv, cwd=td, capture_output=True, timeout=float(acfg.get("timeout_s") or 600),
                           creationflags=NO_WINDOW if os.name == "nt" else 0)
    out = r.stdout.decode("utf-8", errors="replace")
    try:
        return extract_json(out)
    except ValueError as e:
        raise RuntimeError(f"copilot exit {r.returncode}: {e} {r.stderr.decode(errors='replace')[-300:]}") from e


def read_credential(target: str) -> str | None:
    """Password of a generic credential in Windows Credential Manager (e.g. added with cmdkey /generic:)."""
    if os.name != "nt" or not target:
        return None
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                    ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR)]
    adv = ctypes.windll.advapi32
    adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    adv.CredReadW.restype = wintypes.BOOL
    adv.CredFree.argtypes = [ctypes.c_void_p]
    p = ctypes.POINTER(CREDENTIAL)()
    if not adv.CredReadW(target, 1, 0, ctypes.byref(p)):  # CRED_TYPE_GENERIC
        return None
    try:
        blob = bytes(p.contents.CredentialBlob[:p.contents.CredentialBlobSize])
    finally:
        adv.CredFree(p)
    if len(blob) >= 2 and len(blob) % 2 == 0 and not any(blob[1::2]):
        return blob.decode("utf-16-le")
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        return blob.decode("utf-16-le", errors="replace")


def openai_key(oc: dict) -> str | None:
    env = str(oc.get("api_key_env") or "").strip()
    if env and os.environ.get(env):
        return os.environ[env]
    return read_credential(str(oc.get("api_key_credential") or "").strip())


def ask_openai(prompt: str, acfg: dict) -> dict:
    oc = acfg.get("openai") or {}
    if not oc.get("model"):
        raise ValueError("analyzer.openai.model is not set")
    body = {"model": oc["model"], "temperature": float(oc.get("temperature", 0.2)),
            "messages": [{"role": "system", "content": "You reply with exactly one JSON object and nothing else."},
                         {"role": "user", "content": prompt}]}
    if oc.get("json_mode", True):
        body["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json"}
    key = openai_key(oc)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    url = str(oc.get("base_url") or DEFAULTS["analyzer"]["openai"]["base_url"]).rstrip("/") + "/chat/completions"
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=float(acfg.get("timeout_s") or 600)) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} from {url}: {e.read().decode(errors='replace')[:300]}") from e
    return extract_json(data["choices"][0]["message"]["content"])


def ask_analyzer(prompt: str) -> dict:
    acfg = ctx().cfg.get("analyzer") or {}
    backend = analyzer_backend()
    if backend == "copilot":
        return ask_copilot(prompt, acfg)
    if backend == "openai":
        return ask_openai(prompt, acfg)
    raise ValueError(f"unknown analyzer backend {backend!r}")


def validate(changes, lex: dict, rep: dict, corpus: list[str] | None = None) -> list:
    corpus = corpus_texts() if corpus is None else corpus
    hot = {str(w).lower() for w in lex.get("hotwords", [])}
    out = []
    for c in changes if isinstance(changes, list) else []:
        if not isinstance(c, dict):
            continue
        c = dict(c)
        op, err = c.get("op"), None
        if op in ("add_hotword", "remove_hotword", "backlog"):
            c["value"] = str(c.get("value") or "").strip()
        if op == "add_hotword":
            if not c["value"] or len(c["value"]) > 40:
                err = t("e_empty_long")
            elif c["value"].lower() in hot:
                err = t("e_exists")
        elif op == "remove_hotword":
            if c["value"].lower() not in hot:
                err = t("e_missing")
        elif op == "add_replacement":
            try:
                pat = re.compile(c["from"])
                c["to"] = str(c.get("to", ""))
                c["preview"] = pat.sub(c["to"], rep.get("recognized") or "")
                c["corpus_hits"] = sum(1 for x in corpus if pat.search(x))
                c["corpus_size"] = len(corpus)
                if pat.search("") is not None:
                    err = t("e_empty_match")
                elif any(r.get("from") == c["from"] for r in lex.get("replacements", []) if isinstance(r, dict)):
                    err = t("e_exists")
            except (re.error, KeyError, TypeError) as e:
                err = t("e_regex", e=e)
        elif op == "set_stream":
            k, v = c.get("key"), c.get("value")
            if k not in STREAM_BOUNDS or isinstance(v, bool) or not isinstance(v, (int, float)):
                err = t("e_unknown_key")
            elif not STREAM_BOUNDS[k][0] <= v <= STREAM_BOUNDS[k][1]:
                err = t("e_range", r=STREAM_BOUNDS[k])
        elif op == "backlog":
            if not c["value"]:
                err = t("e_empty")
        else:
            err = t("e_unknown_op")
        c["invalid"] = err
        out.append(c)
    return out


def cmd_analyze(args):
    if analyzer_backend() == "off":
        update_pending()
        return
    todo = pending_reports(getattr(args, "target", None)) or pending_reports()
    if not todo:
        update_pending()
        return
    update_pending(analyzing=True)
    w, lex, lex_sha = None, {}, None
    try:
        w = Worker()
        st = w.lexicon_get()
        lex, lex_sha = st.get("data") or {}, st.get("sha256")
    except Exception as e:
        log(f"server unavailable for analysis: {e}")
    try:
        while todo:
            analyze_reports(todo, w, lex, lex_sha)
            todo = pending_reports()  # reports filed while this run was busy (their own run found the lock)
    finally:
        if w:
            w.close()
        update_pending()


def analyze_reports(todo, w, lex, lex_sha):
    for d, rep in todo:
        try:
            ts = (rep.get("handy") or {}).get("timestamp") or time.time()
            audit = audit_records(w, ts, rep.get("recognized", "")) if w else {"matched": False}
            write_json(os.path.join(d, "server_record.json"), audit)
            res = ask_analyzer(build_prompt(rep, audit, lex))
            res["changes"] = validate(res.get("changes"), lex, rep)
            res["lexicon_sha256"] = lex_sha
            res["analyzed"] = dt.datetime.now().isoformat(timespec="seconds")
            write_json(os.path.join(d, "proposal.json"), res)
            log(f"analyzed {os.path.basename(d)}: {len(res['changes'])} changes")
        except Exception:
            log(f"analyze {os.path.basename(d)} failed:\n{traceback.format_exc()}")
            write_json(os.path.join(d, "proposal.json"), {
                "diagnosis": t("analysis_failed"), "error": traceback.format_exc()[-1500:], "changes": [],
                "analyzed": dt.datetime.now().isoformat(timespec="seconds")})


# ---------------------------------------------------------------- tray state

def update_pending(analyzing: bool = False):
    """Tray state for Handy Remote (orange dot = to review, blue dot = analysing); never pops a window.
    "script" lets the tray menu run review / report / guide through this file."""
    c = ctx()
    try:
        os.makedirs(c.state, exist_ok=True)
        proposals, notices = len(to_review()), len(read_json(c.state_file("notices.json"), []) or [])
        write_json(c.pending, {"count": proposals + notices, "analyzing": bool(analyzing), "script": SCRIPT,
                               "proposals": proposals, "notices": notices,
                               "updated": dt.datetime.now().isoformat(timespec="seconds")})
    except Exception:
        log(f"update_pending failed:\n{traceback.format_exc()}")


def notify(title: str, text: str):
    """Never pops up: queued as a tray notice (orange dot), shown on the next tray click."""
    c = ctx()
    log(f"notice: {title}: {text[:300]}")
    try:
        items = read_json(c.state_file("notices.json"), []) or []
        items.append({"at": dt.datetime.now().isoformat(timespec="seconds"), "title": title, "text": text[:2000]})
        write_json(c.notices, items[-20:])
    except Exception:
        log(f"notice write failed:\n{traceback.format_exc()}")
    update_pending()


# ---------------------------------------------------------------- review / apply

def to_review():
    c = ctx()
    out = []
    if not os.path.isdir(c.reports):
        return out
    for rid in sorted(os.listdir(c.reports)):
        d = os.path.join(c.reports, rid)
        rep, prop = read_json(os.path.join(d, "report.json")), read_json(os.path.join(d, "proposal.json"))
        if rep and prop and rep.get("origin") == PC and not os.path.exists(os.path.join(d, "decision.json")):
            out.append((d, rep, prop))
    return out


def describe(c: dict) -> str:
    op = c.get("op")
    if op == "add_hotword":
        s = t("d_add_hotword", v=c["value"])
    elif op == "remove_hotword":
        s = t("d_remove_hotword", v=c["value"])
    elif op == "add_replacement":
        s = t("d_add_replacement", f=c.get("from"), t=c.get("to", ""), h=c.get("corpus_hits"), n=c.get("corpus_size"))
        if c.get("preview"):
            s += "\n      " + t("d_effect", v=c["preview"])
    elif op == "set_stream":
        s = t("d_set_stream", k=c.get("key"), v=c.get("value"))
    elif op == "backlog":
        s = t("d_backlog", v=c.get("value", ""))
    else:
        s = t("d_unknown", v=json.dumps(c, ensure_ascii=False)[:120])
    if c.get("why"):
        s += "\n      " + t("d_why", v=c["why"])
    if c.get("invalid"):
        s += "\n      " + t("d_invalid", v=c["invalid"])
    return s


def default_checked(c: dict) -> bool:
    try:
        conf = float(c.get("confidence", 0.7) or 0)
    except (TypeError, ValueError):
        conf = 0.0
    return not c.get("invalid") and conf >= 0.6


def cmd_review(_args):
    show_notices()
    items = to_review()
    if not items:
        update_pending()
        return
    import tkinter as tk
    from tkinter import ttk
    font = ui_font()
    root, s = tk_root(t("review_title", n=len(items)))
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    canvas = tk.Canvas(root, width=int(840 * s), height=int(480 * s), highlightthickness=0)
    sb = ttk.Scrollbar(root, orient="vertical", command=canvas.yview)
    body = ttk.Frame(canvas, padding=10)
    body.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=body, anchor="nw")
    canvas.configure(yscrollcommand=sb.set)
    canvas.grid(row=0, column=0, sticky="nsew")
    sb.grid(row=0, column=1, sticky="ns")
    picks, row, wrap = [], 0, int(800 * s)

    def line(text, **kw):
        nonlocal row
        ttk.Label(body, text=text, wraplength=wrap, **kw).grid(row=row, column=0, sticky="w")
        row += 1
    for d, rep, prop in items:
        tags = " / ".join(t("tag_" + x) if x in TAGS else str(x) for x in rep.get("tags") or [])
        line(f"■ {rep['id']}   {tags}", font=(font, 10, "bold"))
        line(t("recognized", v=rep.get("recognized")))
        if rep.get("changed"):
            line(t("expected", v=rep.get("expected")))
        if rep.get("note"):
            line(t("note_l", v=rep["note"]))
        line(t("analysis", v=prop.get("diagnosis", "")), foreground="#2a4a7a")
        changes = prop.get("changes", [])
        if not changes:
            line(t("no_changes"), foreground="#777")
        for c in changes:
            v = tk.BooleanVar(value=default_checked(c))
            cb = ttk.Checkbutton(body, text=describe(c), variable=v)
            if c.get("invalid"):
                cb.state(["disabled"])
            cb.grid(row=row, column=0, sticky="w", padx=16)
            row += 1
            picks.append((d, c, v))
        ttk.Separator(body).grid(row=row, column=0, sticky="we", pady=6)
        row += 1
    decision = {}

    def done(kind):
        decision["kind"] = kind
        decision["picked"] = [(d, c) for d, c, v in picks if v.get() and not c.get("invalid")]
        root.destroy()
    bf = ttk.Frame(root, padding=8)
    bf.grid(row=1, column=0, columnspan=2, sticky="e")
    ttk.Button(bf, text=t("apply_sel"), command=lambda: done("apply")).grid(row=0, column=0, padx=4)
    ttk.Button(bf, text=t("reject_all"), command=lambda: done("reject")).grid(row=0, column=1, padx=4)
    ttk.Button(bf, text=t("later"), command=root.destroy).grid(row=0, column=2, padx=4)
    root.after(100, lambda: (root.lift(), root.focus_force()))
    root.mainloop()
    if not decision:
        return
    finish_review(items, decision["kind"], decision["picked"] if decision["kind"] == "apply" else [])


def finish_review(items, kind: str, picked: list):
    c = ctx()
    applied = apply_changes([ch for _d, ch in picked]) if picked else {}
    now = dt.datetime.now().isoformat(timespec="seconds")
    os.makedirs(c.feedback, exist_ok=True)
    for d, rep, _prop in items:
        mine = [ch for dd, ch in picked if dd == d]
        write_json(os.path.join(d, "decision.json"), {"kind": kind, "applied": mine,
                                                      "deployed": applied.get("deployed"), "at": now})
        with open(c.changelog, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"at": now, "report": rep["id"], "decision": kind,
                                "changes": [{k: ch.get(k) for k in ("op", "value", "from", "to", "key")}
                                            for ch in mine]}, ensure_ascii=False) + "\n")
    update_pending()
    if any(ch.get("op") != "backlog" for _d, ch in picked) and applied.get("deployed"):
        verify(sorted({d for d, ch in picked if ch.get("op") != "backlog"}))


def apply_to_lexicon(lex: dict, changes: list) -> dict:
    lex = copy.deepcopy(lex) if isinstance(lex, dict) else {}
    for c in changes:
        op = c.get("op")
        if op == "add_hotword":
            if c["value"].lower() not in {str(w).lower() for w in lex.get("hotwords", [])}:
                lex.setdefault("hotwords", []).append(c["value"])
        elif op == "remove_hotword":
            lex["hotwords"] = [w for w in lex.get("hotwords", []) if str(w).lower() != c["value"].lower()]
        elif op == "add_replacement":
            if not any(isinstance(r, dict) and r.get("from") == c["from"] for r in lex.get("replacements", [])):
                lex.setdefault("replacements", []).append({"from": c["from"], "to": c.get("to", "")})
        elif op == "set_stream":
            lex.setdefault("stream", {})[c["key"]] = c["value"]
    return lex


def push_lexicon(w: Worker, changes: list, attempts: int = 2) -> dict:
    """Read-modify-write against the server; a concurrent edit makes the put fail, so read again and retry."""
    c = ctx()
    last = None
    for _ in range(attempts):
        st = w.lexicon_get()
        cur = st.get("data") or {}
        os.makedirs(c.lex_history, exist_ok=True)
        write_json(os.path.join(c.lex_history, f"lexicon-{dt.datetime.now():%Y%m%d-%H%M%S}.json"), cur)
        try:
            return w.lexicon_put(apply_to_lexicon(cur, changes), st.get("sha256") or "")
        except ServerError as e:
            last = e
            if "changed" not in str(e):
                break
    raise last or ServerError("lexicon update failed")


def apply_changes(changes: list) -> dict:
    c = ctx()
    notes = [ch["value"] for ch in changes if ch.get("op") == "backlog"]
    if notes:
        os.makedirs(c.feedback, exist_ok=True)
        with open(c.backlog, "a", encoding="utf-8", newline="\n") as f:
            for n in notes:
                f.write(f"- [ ] {dt.date.today()} {n}\n")
    lex_changes = [ch for ch in changes if ch.get("op") != "backlog"]
    if not lex_changes:
        return {"deployed": None}
    try:
        with Worker() as w:
            st = push_lexicon(w, lex_changes)
        log(f"applied {len(lex_changes)} changes, lexicon {str(st.get('sha256'))[:12]}")
        return {"deployed": True, "sha256": st.get("sha256")}
    except Exception as e:
        log(f"deploy failed: {e}")
        notify(t("app"), t("deploy_failed", e=e))
        return {"deployed": False}


def verify(dirs: list):
    """Re-transcribe with the new lexicon. Silent when it helped or held; a tray notice only on a problem."""
    time.sleep(1.5)  # the server reloads the lexicon on its next request
    problems = []
    for d in dirs:
        rep = read_json(os.path.join(d, "report.json"))
        wav = os.path.join(d, "audio.wav")
        if not rep or not os.path.exists(wav):
            continue
        rid = os.path.basename(d)
        try:
            new = retranscribe(wav, handy_vocab())
        except Exception as e:
            log(f"verify {rid}: {e}")
            problems.append(t("verify_failed", rid=rid, e=e))
            continue
        dec = read_json(os.path.join(d, "decision.json"), {})
        dec["retranscribed"] = new
        if not new.strip():
            problems.append(t("verify_empty", rid=rid))
        elif rep.get("changed"):
            target = rep["expected"]
            dec["similarity_before"] = round(difflib.SequenceMatcher(None, rep["recognized"] or "", target).ratio(), 3)
            dec["similarity_after"] = round(difflib.SequenceMatcher(None, new, target).ratio(), 3)
            if dec["similarity_after"] < dec["similarity_before"] - 0.02:
                problems.append(t("verify_worse", rid=rid, a=dec["similarity_before"], b=dec["similarity_after"],
                                  new=new[:300]))
        log(f"verify {rid}: sim {dec.get('similarity_before')} -> {dec.get('similarity_after')}")
        write_json(os.path.join(d, "decision.json"), dec)
    if problems:
        notify(t("verify_title"), "\n\n".join(problems)[:1500])


def show_notices() -> bool:
    """Tray click: show queued notices, then clear them."""
    c = ctx()
    items = read_json(c.state_file("notices.json"), []) or []
    if not items:
        return False
    from tkinter import ttk
    root, s = tk_root(t("notices_title", n=len(items)))
    body = ttk.Frame(root, padding=12)
    body.grid(row=0, column=0, sticky="nsew")
    for i, n in enumerate(items):
        ttk.Label(body, text=f"■ {n.get('at', '')}  {n.get('title', '')}",
                  font=(ui_font(), 10, "bold")).grid(row=2 * i, column=0, sticky="w")
        ttk.Label(body, text=n.get("text", ""), wraplength=int(720 * s)).grid(row=2 * i + 1, column=0,
                                                                              sticky="w", pady=(0, 8))
    ttk.Button(root, text=t("ok"), command=root.destroy).grid(row=1, column=0, sticky="e", padx=12, pady=8)
    root.after(100, lambda: (root.lift(), root.focus_force()))
    root.mainloop()
    write_json(c.notices, [])
    update_pending()
    return True


# ---------------------------------------------------------------- status / guide / main

def cmd_status(_args):
    c = ctx()
    print(f"config:      {config_path()}{'' if os.path.exists(config_path()) else ' (missing: defaults)'}")
    print(f"handy dir:   {c.handy}")
    print(f"reports:     {c.reports}")
    print(f"state:       {c.state}")
    print(f"analyzer:    {analyzer_backend()}")
    try:
        s = connection_settings()
        argv = connect_argv(s)
        print("connection:  " + (f"ssh -W {s['host']}:{s['server_port']} {s['target']}" if argv
                                 else f"tcp {s['host']}:{s['server_port']}"))
    except Exception as e:
        print(f"connection:  invalid ({e})")
    for rid in sorted(os.listdir(c.reports)) if os.path.isdir(c.reports) else []:
        d = os.path.join(c.reports, rid)
        st = "decided" if os.path.exists(os.path.join(d, "decision.json")) else (
            "proposed" if os.path.exists(os.path.join(d, "proposal.json")) else "new")
        print(rid, st)
    pend = read_json(c.pending, {}) or {}
    print(f"pending:     {pend.get('count', 0)}  analyzing={pend.get('analyzing', False)}")


def cmd_guide(_args):
    name = "guide.zh-CN.html" if ctx().lang == "zh-CN" else "guide.en.html"
    path = os.path.join(HERE, name)
    if hasattr(os, "startfile"):
        os.startfile(path)
    else:
        print(path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Dictation feedback for Handy Remote")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("report", help="report a wrong dictation")
    a = sub.add_parser("analyze", help="analyse new reports (runs in the background)")
    a.add_argument("target", nargs="?", help="report folder or id (default: every new report)")
    sub.add_parser("review", help="review pending suggestions")
    sub.add_parser("status", help="show settings and reports")
    sub.add_parser("guide", help="open the user guide")
    sub.add_parser("pending", help="rewrite pending.json (tray state) now")
    args = ap.parse_args(argv)
    c = ctx()
    lock = None
    if args.cmd in ("analyze", "review"):
        os.makedirs(c.state, exist_ok=True)
        lock = os.path.join(c.state, f"{args.cmd}.lock")
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            if time.time() - os.path.getmtime(lock) < 3600:
                return  # a running analysis rescans for new reports before it exits
            os.utime(lock)
    try:
        {"report": cmd_report, "analyze": cmd_analyze, "review": cmd_review, "status": cmd_status,
         "guide": cmd_guide, "pending": lambda _a: update_pending()}[args.cmd](args)
    except Exception:
        log(f"{args.cmd} crashed:\n{traceback.format_exc()}")
        raise
    finally:
        if lock and os.path.exists(lock):
            os.remove(lock)


if __name__ == "__main__":
    main()
