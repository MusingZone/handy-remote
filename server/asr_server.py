"""Handy Remote recognition server.

Speaks Handy's engine_supervisor worker protocol over TCP (loopback by default; clients
reach it through `ssh -W`). Final text comes from Qwen3-ASR-1.7B on an OpenVINO device
(normally the iGPU), live previews and speculative drafts from Qwen3-ASR-0.6B on the NPU
(streaming Paraformer on the CPU when there is no NPU). Configuration: config.py.
Each frame: [u32 LE json_len][json][u32 LE pcm_bytes][f32 LE pcm].

Usage: python asr_server.py [--home DIR]
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import difflib
import faulthandler
import gc
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import socketserver
import struct
import sys
import threading
import time
import traceback
import wave

import numpy as np
import sherpa_onnx

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import config  # noqa: E402
from qwen_ov import Aborted, Qwen3AsrOV  # noqa: E402
from punct import Punctuator, commit_unit  # noqa: E402


def _home_arg() -> str | None:
    if __name__ == "__main__" and "--home" in sys.argv:
        return sys.argv[sys.argv.index("--home") + 1]
    return None


CFG = config.load(_home_arg())
PORT = CFG.port
QWEN_DIR = CFG.model_path(CFG.final_model)
PARA_DIR = CFG.model_path(CFG.preview_model)
VAD_MODEL = CFG.model_path(CFG.vad_model)
LEXICON = CFG.lexicon
LOG_DIR = CFG.log_dir
SR = 16000
PAD_BEFORE = int(0.25 * SR)
PAD_AFTER = int(0.15 * SR)
MAX_SEGMENT_S = 25.0
MAX_CHUNK_S = 28.0
TRAIL_PUNCT = "。．.，,、！!？?；;：:…"
STREAM_DEFAULTS = {
    "vad_min_silence_s": 0.5,   # silence that ends a VAD speech segment
    "unit_min_s": 4.0,          # a unit closes at the first pause once it holds this much audio
    "unit_pause_s": 1.1,        # ...or after this much silence, whatever its length
    "comma_gap_s": 1.0,         # a shorter pause between units joins them with a comma
    "preview_interval_s": 1.0,  # Qwen re-decodes the open sentence this often while the GPU is idle (0 = off)
    "preview_max_s": 15.0,      # ...as long as the open sentence is not longer than this
    "unit_context_chars": 0,    # pass this much of the recording's earlier text to Qwen with each unit (0 = off)
    "draft_para_tail": 1,       # 1 = overlay appends Paraformer words heard after the newest Qwen preview; 0 = Qwen only
    "preview_device": 0,        # 0 = previews on the iGPU 1.7B (yield to finals); 1 = Qwen3-ASR-0.6B on the NPU
    "preview_min_s": 1.0,       # first preview of a sentence once it holds this much audio
    "npu_yield": 0,             # 1 = NPU previews pause while an iGPU final is queued or running
    "npu_rollback": 5,          # NPU previews reuse the previous hypothesis minus this many tokens (0 = never reuse)
    "para_draft": 1,            # 0 = never show Paraformer text: the overlay waits for the first Qwen preview
    "head_check": 1,            # 1 = re-decode a unit with the wider preview audio when its final lacks draft words
    "spec_draft": 1,            # 1 = the unit's last preview ids seed lossless speculative decoding of the final
    "prefix_cache": 1,          # 1 = reuse the hotword prompt's KV; only the new context, audio and suffix are prefilled
    "grammar_punct": 1,         # 1 = a punctuation model decides from the words whether units join with a mark
    "drop_fillers": 1,          # 1 = drop hesitation fillers (嗯/呃/um) from committed text
    "stop_gap_s": 1.4,          # a pause this long after Qwen's full stop keeps it even if the punct model says comma
    "unit_lookback_s": 10.0,    # a unit also takes soft speech the server VAD missed up to this far before it (0 = off)
    "soft_vad_threshold": 0.3,  # ...and at the end; "soft" = what a VAD this sensitive (the client's) hears (0 = off)
}
PUNCT_MODEL = CFG.model_path(CFG.punct_model)
WARMUP_WAV = CFG.warmup_wav
# Models stay loaded, except: unloaded after IDLE_UNLOAD_S without use (0 = never), and during the optional
# offline window (hours [NIGHT_START, NIGHT_END) once idle for NIGHT_IDLE_S); reloaded when the window ends
# or on the next request.
IDLE_UNLOAD_S = CFG.idle_unload_hours * 3600
NIGHT_START, NIGHT_END = CFG.offline_hours or (0, 0)
NIGHT_IDLE_S = CFG.offline_idle_minutes * 60
NPU_DIR = CFG.model_path(CFG.draft_model)
# Status shown in place of the live preview. Clients translate the token; `legacy_status_text`
# replaces it with literal text for clients that predate status tokens.
STATUS_WAKING = "{{handy-remote:waking}}"
# NPUW sizes prefill and KV statically: 512 prompt tokens fit a 15 s preview plus the hotword context
NPU_OPTS = dict(max_prompt_tokens=512, max_new_tokens=128, dec_config={"NPUW_LLM_GENERATE_HINT": "BEST_PERF"})
DRAFT_REPEAT = re.compile(r"([\u4e00-\u9fff]{2,4})\1+")  # Paraformer stutter: 是不是是不是 -> 是不是 (draft only)

LANG_NAMES = {"zh": "Chinese", "en": "English", "yue": "Cantonese", "ja": "Japanese", "ko": "Korean",
              "de": "German", "fr": "French", "es": "Spanish", "ru": "Russian", "it": "Italian",
              "pt": "Portuguese", "ar": "Arabic", "id": "Indonesian", "th": "Thai", "vi": "Vietnamese",
              "tr": "Turkish", "hi": "Hindi", "ms": "Malay", "nl": "Dutch", "sv": "Swedish", "da": "Danish",
              "fi": "Finnish", "pl": "Polish", "cs": "Czech", "fil": "Filipino", "fa": "Persian",
              "el": "Greek", "ro": "Romanian", "hu": "Hungarian", "mk": "Macedonian"}
LANG_CODES = {v: k for k, v in LANG_NAMES.items()}

os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
    handlers=[logging.FileHandler(os.path.join(LOG_DIR, "server.log"), encoding="utf-8"),
              logging.StreamHandler(sys.stderr)])
log = logging.getLogger("asr")

INFERENCE_TIMEOUT_S = 45.0
LOAD_TIMEOUT_S = float(os.environ.get("VOICE_ASR_LOAD_TIMEOUT_S", "180"))
# Leave ~15 s headroom for replay + GPU decode under Handy's 90 s REMOTE_DEADLINES.
LOAD_WAIT_S = float(os.environ.get("VOICE_ASR_LOAD_WAIT_S", "75"))


def inference_call(device, fn, audio, **kwargs):
    """A stuck native infer cannot be safely reset from another Python thread."""
    cpu = device.upper().startswith("CPU")
    timeout = max(300.0 if cpu else INFERENCE_TIMEOUT_S, len(audio) / SR * (20 if cpu else 3))
    finished = threading.Event()

    def expired():
        if finished.is_set():
            return
        log.critical("%s inference exceeded %.1fs; restarting the server process", device, timeout)
        faulthandler.dump_traceback()
        os._exit(4)

    timer = threading.Timer(timeout, expired)
    timer.daemon = True
    timer.start()
    try:
        return fn(audio, **kwargs)
    finally:
        finished.set()
        timer.cancel()


class Lexicon:
    """Hot-reloaded hotwords and regex replacements."""

    def __init__(self, path: str):
        self.path = path
        self.mtime = None
        self.hotwords: list[str] = []
        self.replacements: list[tuple[re.Pattern, str]] = []
        self.stream: dict = dict(STREAM_DEFAULTS)
        self.draft_terms: list[tuple[re.Pattern, str]] = []
        self.lock = threading.Lock()

    @staticmethod
    def _draft_patterns(words: list[str]) -> list[tuple[re.Pattern, str]]:
        """Paraformer spells English as spaced lowercase letters ("v s c o d e",
        "power shell"); match ASCII hotwords ignoring case and spaces."""
        out = []
        for w in sorted(words, key=len, reverse=True):
            letters = re.sub(r"[^A-Za-z0-9]", "", w)
            if len(letters) < 2 or not w.isascii():
                continue
            body = r"[\s\-.]?".join(re.escape(ch) for ch in letters)
            out.append((re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", re.I), w))
        return out

    def fix_draft(self, text: str) -> str:
        if not text or not re.search(r"[A-Za-z]", text):
            return text
        for pat, w in self.draft_terms:
            text = pat.sub(w, text)
        # Leftover spelled-out letters: "x m l" -> "xml".
        return re.sub(r"(?<![A-Za-z])(?:[A-Za-z] )+[A-Za-z](?![A-Za-z])",
                      lambda m: m.group(0).replace(" ", ""), text)

    def refresh(self):
        try:
            m = os.path.getmtime(self.path)
        except OSError:
            return
        if m == self.mtime:
            return
        with self.lock:
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
                reps = []
                for r in data.get("replacements", []):
                    try:
                        reps.append((re.compile(r["from"]), r["to"]))
                    except (re.error, KeyError) as e:
                        log.warning("bad replacement %r: %s", r, e)
                self.hotwords = [w for w in data.get("hotwords", []) if isinstance(w, str) and w.strip()]
                self.replacements = reps
                self.draft_terms = self._draft_patterns(self.hotwords)
                st = dict(STREAM_DEFAULTS)
                for k, v in (data.get("stream") or {}).items():
                    if k in st and isinstance(v, (int, float)):
                        st[k] = float(v)
                self.stream = st
                self.mtime = m
                log.info("lexicon loaded: %d hotwords, %d replacements", len(self.hotwords), len(reps))
            except Exception as e:  # keep the previous lexicon on a bad edit
                log.error("lexicon reload failed: %s", e)

    def context(self, client_terms: list[str]) -> str:
        self.refresh()
        seen, terms = set(), []
        for t in list(client_terms) + self.hotwords:
            k = t.strip().lower()
            if k and k not in seen:
                seen.add(k)
                terms.append(t.strip())
        return ", ".join(terms)

    def fix(self, text: str) -> str:
        for pat, rep in self.replacements:
            text = pat.sub(rep, text)
        return text


def strip_overlap(head: str, tail: str, max_k: int = 12) -> str:
    """Drop the start of `tail` that repeats the end of `head` (punctuation/space/case-insensitive).
    Only used for the overlay draft, so a rare lost doubled character (看看) is acceptable."""
    h = re.sub(r"[\W_]+", "", head.lower())
    pos = [i for i, c in enumerate(tail) if re.match(r"\w", c) and c != "_"]
    tn = "".join(tail[i] for i in pos).lower()
    for k in range(min(max_k, len(tn), len(h)), 0, -1):
        if tn[:k] == h[-k:]:
            return tail[pos[k - 1] + 1:].lstrip(" ，,。.、")
    return tail


def echoes_context(text: str, prior: str, dur: float) -> bool:
    """Qwen read the prior text back instead of transcribing: far more words than the audio can hold, or
    mostly a verbatim copy of the prior text."""
    t = re.sub(r"[\W_]+", "", text)
    p = re.sub(r"[\W_]+", "", prior)
    if len(t) < 8 or not p:
        return False
    words = len(re.findall(r"[\u3400-\u9fff]", text)) + len(re.findall(r"[A-Za-z0-9]+", text))
    if words > 10 * max(dur, 0.5) + 4:
        return True
    m = difflib.SequenceMatcher(a=t, b=p, autojunk=False).find_longest_match(0, len(t), 0, len(p))
    return m.size >= 8 and m.size >= 0.5 * len(t)


def word_chars(text: str) -> str:
    return re.sub(r"[\W_]+", "", text or "").lower()


def join_text(parts: list[str]) -> str:
    out = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if out and re.match(r"[A-Za-z0-9]", p[0]) and re.search(r"[A-Za-z0-9.,;:!?]$", out):
            out += " "
        out += p
    return out


class Engine:
    """Process-wide models. Qwen runs on one thread (the GPU); Paraformer
    decodes are serialized by a lock (cheap, ~30 ms per 100 ms of audio)."""

    def __init__(self, warm_up: bool = True, should_warm_up=None):
        t = time.time()
        # Stateful KV on the iGPU breaks once the cache crosses ~342 tokens (OpenVINO
        # 2026.4 GPU plugin); passing the KV explicitly is just as fast for short units.
        self.qwen = Qwen3AsrOV(QWEN_DIR, device=CFG.final_device, cache_dir=CFG.cache_dir,
                               stateful=CFG.stateful_kv)
        self.qwen_pool = cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="qwen")
        self.wait_lock = threading.Lock()
        self.finals_waiting = 0
        self.para = sherpa_onnx.OnlineRecognizer.from_paraformer(
            tokens=os.path.join(PARA_DIR, "tokens.txt"),
            encoder=os.path.join(PARA_DIR, "encoder.int8.onnx"),
            decoder=os.path.join(PARA_DIR, "decoder.int8.onnx"),
            num_threads=2, sample_rate=SR, feature_dim=80, decoding_method="greedy_search",
            enable_endpoint_detection=False)
        self.para_lock = threading.Lock()
        self.lexicon = Lexicon(LEXICON)
        self.lexicon.refresh()
        self.punct = None
        if os.path.exists(PUNCT_MODEL):
            try:
                self.punct = Punctuator(PUNCT_MODEL)
            except Exception as e:
                log.warning("punctuation model unavailable: %s", e)
        warm = np.zeros(SR, dtype=np.float32)
        inference_call(self.qwen.device + " warm-up", self.qwen.transcribe, warm, max_new_tokens=4)
        self.finals_running = 0
        self.npu = None
        self.npu_pool = cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="npu")
        if CFG.draft_enabled and os.path.isdir(NPU_DIR):
            try:
                from qwen_npu import Qwen3AsrNPU
                self.npu = Qwen3AsrNPU(NPU_DIR, **NPU_OPTS)
                inference_call("NPU warm-up", self.npu.transcribe, warm, max_new_tokens=4)
            except Exception as e:
                log.warning("NPU preview model unavailable: %s", str(e).splitlines()[0][:200])
                self.npu = None
        t_load = time.time() - t
        if warm_up and CFG.warmup:
            if should_warm_up is None or should_warm_up():
                self.warmup(should_continue=should_warm_up)
            else:
                log.info("warm-up skipped: client waiting")
        log.info("engine ready in %.1fs (load %.1fs; qwen device=%s, npu previews=%s, punct=%s)", time.time() - t,
                 t_load, self.qwen.device, bool(self.npu), bool(self.punct))

    def warmup(self, should_continue=None):
        """Run the real request path (hotword prefix cache, NPU preview + rollback, speculative final,
        punctuation) on a recorded clip, so the user's first dictation is as fast as later ones."""
        if not os.path.exists(WARMUP_WAV):
            log.info("no warm-up clip yet (%s); zero-audio warm-up only", WARMUP_WAV)
            return
        try:
            with wave.open(WARMUP_WAV, "rb") as w:
                pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
        except Exception as e:
            log.warning("warm-up clip unreadable (%s); zero-audio warm-up only", e)
            return
        t = time.time()
        try:
            r = None
            for pass_idx in range(2):
                if should_continue and not should_continue():
                    log.info("warm-up %s: client waiting",
                             "skipped" if pass_idx == 0 else f"yielded after pass {pass_idx}")
                    return
                ids = None
                if self.npu:
                    p = self.submit_npu_preview(pcm, [], None, [], False).result()
                    if should_continue and not should_continue():
                        log.info("warm-up yielded after NPU preview: client waiting")
                        return
                    p = self.submit_npu_preview(pcm, [], None, self.npu.rollback(p["ids"], 5), False).result()
                    ids = p.get("ids")
                if should_continue and not should_continue():
                    log.info("warm-up yielded before GPU pass %d: client waiting", pass_idx + 1)
                    return
                r = self.submit(pcm, [], None, draft_ids=ids, prefix_cache=True).result()
            if self.punct and r is not None:
                if should_continue and not should_continue():
                    log.info("warm-up yielded before punct: client waiting")
                    return
                self.punct.fix(r["text"][:-1], r["text"][-8:-1] or "ok", r["text"][-1:], 0.5)
            if r is not None:
                log.info("warm-up done in %.1fs: %s", time.time() - t, r["text"][:40])
        except Exception as e:
            log.warning("warm-up failed: %s", str(e).splitlines()[0][:200])

    def close(self):
        self.qwen_pool.shutdown(wait=True)
        self.npu_pool.shutdown(wait=True)
        self.qwen = self.npu = self.para = self.punct = None

    def stream_cfg(self) -> dict:
        self.lexicon.refresh()
        return dict(self.lexicon.stream)

    def new_vad(self, min_silence: float = 0.5, threshold: float = 0.5):
        c = sherpa_onnx.VadModelConfig()
        c.silero_vad.model = VAD_MODEL
        c.silero_vad.threshold = threshold
        c.silero_vad.min_silence_duration = min_silence
        c.silero_vad.min_speech_duration = 0.2
        c.silero_vad.max_speech_duration = MAX_SEGMENT_S
        c.silero_vad.window_size = 512
        c.sample_rate = SR
        c.num_threads = 1
        return sherpa_onnx.VoiceActivityDetector(c, buffer_size_in_seconds=600)

    def transcribe(self, audio: np.ndarray, vocab: list[str], language: str | None, abort=None,
                   prior: str = "", draft_ids: list[int] | None = None, prefix_cache: bool | None = None) -> dict:
        ctx = self.lexicon.context(vocab)
        if prior:
            ctx = f"{ctx}\n{prior}" if ctx else prior
        lang_name = LANG_NAMES.get(language) if language else None
        try:
            r = inference_call(self.qwen.device, self.qwen.transcribe, audio, context=ctx, language=lang_name, abort=abort, draft_ids=draft_ids,
                                     prefix_cache=prefix_cache)
        except Aborted:
            raise
        except Exception as e:
            log.warning("qwen failed (%s); rebuilding infer requests and retrying", str(e).splitlines()[0][:200])
            self.qwen.reset_requests()
            r = inference_call(self.qwen.device + " retry", self.qwen.transcribe, audio, context=ctx, language=lang_name, abort=abort)
        if prior and echoes_context(r["text"], prior, len(audio) / SR):
            # a short unclear unit can make Qwen read the earlier text back; decode it again without it
            log.info("context echo (%.1fs audio): %r; re-decoding without prior text", len(audio) / SR, r["text"][:60])
            t0 = r.get("timings", {})
            r = inference_call(self.qwen.device + " context retry", self.qwen.transcribe, audio, context=self.lexicon.context(vocab), language=lang_name,
                                     abort=abort,
                                     prefix_cache=prefix_cache)
            r.setdefault("timings", {})["echo_retry"] = 1
            r["timings"]["first_try_total"] = t0.get("total")
        r["raw"] = r["text"]
        r["text"] = self.lexicon.fix(r["text"])
        r["lang_code"] = LANG_CODES.get(r.get("language") or "", language)
        return r

    def submit(self, audio: np.ndarray, vocab: list[str], language: str | None, prior_fn=None,
               draft_ids: list[int] | None = None, prefix_cache: bool | None = None) -> cf.Future:
        """`prior_fn` is called when the decode starts, so it sees every earlier unit that has finished by then."""
        with self.wait_lock:
            self.finals_waiting += 1

        def job():
            with self.wait_lock:
                self.finals_waiting -= 1
                self.finals_running += 1
            try:
                return self.transcribe(audio, vocab, language, prior=prior_fn() if prior_fn else "",
                                       draft_ids=draft_ids, prefix_cache=prefix_cache)
            finally:
                with self.wait_lock:
                    self.finals_running -= 1
        return self.qwen_pool.submit(job)

    def finals_busy(self) -> bool:
        return self.finals_waiting + self.finals_running > 0

    def submit_preview(self, audio: np.ndarray, vocab: list[str], language: str | None, ctl: dict) -> cf.Future:
        """Low priority: gives way (Aborted) as soon as a final decode is queued, unless ctl['keep']."""
        return self.qwen_pool.submit(self.transcribe, audio, vocab, language,
                                     lambda: not ctl.get("keep") and self.finals_waiting > 0)

    def submit_npu_preview(self, audio: np.ndarray, vocab: list[str], language: str | None,
                           prefix_ids: list[int], yield_to_finals: bool) -> cf.Future:
        def job():
            lang_name = LANG_NAMES.get(language) if language else None
            r = inference_call("NPU", self.npu.transcribe, audio, context=self.lexicon.context(vocab), language=lang_name,
                                    abort=self.finals_busy if yield_to_finals else None, prefix_ids=prefix_ids)
            r["text"] = self.lexicon.fix(r["text"])
            return r
        return self.npu_pool.submit(job)


ENGINE: Engine | None = None
ENGINE_READY = threading.Event()
LIFE_LOCK = threading.Lock()
LIFE = {"loading": False, "busy": 0, "last_use": time.time(), "reason": None}


def touch():
    LIFE["last_use"] = time.time()


def ensure_engine():
    """Start loading the models unless they are loaded or loading."""
    with LIFE_LOCK:
        if ENGINE is not None or LIFE["loading"]:
            return
        LIFE["loading"] = True
    threading.Thread(target=_boot, name="boot", daemon=True).start()


def _boot():
    global ENGINE
    finished = threading.Event()

    def expired():
        if finished.is_set():
            return
        log.critical("engine load exceeded %.1fs; restarting the server process", LOAD_TIMEOUT_S)
        faulthandler.dump_traceback()
        os._exit(3)

    timer = threading.Timer(LOAD_TIMEOUT_S, expired)
    timer.daemon = True
    timer.start()
    try:
        # Check LIFE["busy"] after model load (not before): a client that began dictating while
        # models were loading skips the warm-up so its own request gets the GPU immediately.
        eng = Engine(warm_up=True, should_warm_up=lambda: LIFE["busy"] == 0)
    except Exception:
        log.critical("engine failed:\n%s", traceback.format_exc())
        os._exit(3)  # the supervisor restarts the server
    finally:
        finished.set()
        timer.cancel()
    with LIFE_LOCK:
        ENGINE = eng
        LIFE["loading"] = False
        LIFE["reason"] = None
        ENGINE_READY.set()
    touch()


def unload_engine(reason: str) -> bool:
    global ENGINE
    with LIFE_LOCK:
        if ENGINE is None or LIFE["loading"] or LIFE["busy"]:
            return False
        eng, ENGINE = ENGINE, None
        ENGINE_READY.clear()
        LIFE["reason"] = reason
    eng.close()
    del eng
    gc.collect()
    log.info("engine unloaded (%s)", reason)
    return True


def in_night(now: dt.datetime | None = None) -> bool:
    if NIGHT_START == NIGHT_END:
        return False
    h = (now or dt.datetime.now()).hour
    return NIGHT_START <= h < NIGHT_END if NIGHT_START <= NIGHT_END else (h >= NIGHT_START or h < NIGHT_END)


def lifecycle():
    while True:
        time.sleep(30)
        try:
            idle = time.time() - LIFE["last_use"]
            if ENGINE is not None:
                if in_night() and idle >= NIGHT_IDLE_S:
                    unload_engine("night")
                elif IDLE_UNLOAD_S > 0 and idle >= IDLE_UNLOAD_S:
                    unload_engine(f"idle {idle / 3600:.1f} h")
            elif LIFE["reason"] == "night" and not LIFE["loading"] and not in_night():
                log.info("offline window over: reloading")
                ensure_engine()
        except Exception:
            log.error("lifecycle: %s", traceback.format_exc())


def acquire_engine(timeout: float) -> Engine | None:
    touch()
    ensure_engine()
    ENGINE_READY.wait(timeout)
    return ENGINE


class LazyStream:
    """A recording that may begin while the models are unloaded: audio is buffered until they are
    loaded again (~15 s), then replayed into a real Stream. Holds a busy count so nothing unloads mid-use."""

    WAKING = CFG.legacy_status_text or STATUS_WAKING

    def __init__(self, run: dict):
        self.run = run
        self.buf: list[np.ndarray] = []
        self.inner: Stream | None = None
        self.revision = 0
        self.closed = False
        with LIFE_LOCK:
            LIFE["busy"] += 1
        touch()
        ensure_engine()

    def close(self):
        if not self.closed:
            self.closed = True
            with LIFE_LOCK:
                LIFE["busy"] -= 1
            touch()

    def _ready(self, wait: float = 0, replay_preview: bool = True) -> Stream | None:
        if self.inner is None:
            if wait:
                ENGINE_READY.wait(wait)
            eng = ENGINE
            if eng is None:
                return None
            self.inner = Stream(eng, self.run)
            self.inner.revision = self.revision
            had_waking = self.revision > 0
            if self.buf:
                pcm, self.buf = np.concatenate(self.buf), []
                prev_allow = getattr(self.inner, "allow_preview", True)
                if hasattr(self.inner, "allow_preview"):
                    self.inner.allow_preview = False
                try:
                    for i in range(0, len(pcm), SR // 10):  # Silero VAD misreads one huge buffer
                        self.inner.feed(pcm[i:i + SR // 10])
                finally:
                    if hasattr(self.inner, "allow_preview"):
                        self.inner.allow_preview = prev_allow and replay_preview
            elif hasattr(self.inner, "allow_preview"):
                self.inner.allow_preview = replay_preview
            if had_waking and hasattr(self.inner, "last_text"):
                # Intermediate replay outputs were discarded; mark last_text as WAKING so the
                # next live feed replaces the waking banner on the overlay even on silence.
                self.inner.last_text = ("", self.WAKING)
        return self.inner

    def feed(self, pcm: np.ndarray) -> dict:
        touch()
        s = self._ready()
        if s:
            return s.feed(pcm)
        self.buf.append(pcm)
        ms = int(sum(len(b) for b in self.buf) * 1000 / SR)
        changed = self.revision == 0
        if changed:
            self.revision = 1
        update = {"result_changed": changed, "is_final": False, "revision": self.revision,
                  "input_received_ms": ms, "audio_committed_ms": 0, "buffered_ms": ms,
                  "committed_changed": False, "tentative_changed": changed}
        text = {"full": self.WAKING, "committed": "", "tentative": self.WAKING}
        return {"Fed": {"update": update, "text": text if changed else None}}

    def finalize(self, want_language: bool) -> dict:
        try:
            s = self._ready(wait=LOAD_WAIT_S, replay_preview=False)
            if s is None:
                return {"BackendError": "ASR server: the models did not load in time"}
            return s.finalize(want_language)
        finally:
            self.close()


def audit(kind: str, **kw):
    try:
        path = os.path.join(LOG_DIR, f"asr-{dt.date.today():%Y%m%d}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": dt.datetime.now().isoformat(timespec="milliseconds"), "kind": kind, **kw},
                               ensure_ascii=False) + "\n")
    except OSError:
        pass


def prune_logs(days: int = CFG.log_days):
    cutoff = time.time() - days * 86400
    for n in os.listdir(LOG_DIR):
        if n.startswith("asr-") and n.endswith(".jsonl"):
            p = os.path.join(LOG_DIR, n)
            if os.path.getmtime(p) < cutoff:
                os.remove(p)


# Feedback-tool requests: they let a client whose SSH key may only forward the server port read the audit
# records of a past dictation and update the lexicon, without shell access to this machine.

def audit_around(ts: float, window_s: float = 600, limit: int = 2000) -> list[dict]:
    t0 = dt.datetime.fromtimestamp(ts - window_s)
    t1 = dt.datetime.fromtimestamp(ts + window_s)
    out = []
    for day in sorted({t0.date(), t1.date()}):
        try:
            with open(os.path.join(LOG_DIR, f"asr-{day:%Y%m%d}.jsonl"), encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in lines:
            try:
                r = json.loads(line)
                if t0 <= dt.datetime.fromisoformat(r["ts"]) <= t1:
                    out.append(r)
            except (ValueError, KeyError, TypeError):
                pass
    return out[-limit:]


def lexicon_state() -> dict:
    try:
        with open(LEXICON, "rb") as f:
            raw = f.read()
    except OSError:
        return {"data": {}, "sha256": ""}
    return {"data": json.loads(raw.decode("utf-8-sig")), "sha256": hashlib.sha256(raw).hexdigest()}


def lexicon_put(data, base_sha256: str) -> dict:
    """Replace the lexicon if it is still the version the client read; the old one goes to lexicon-history."""
    if not isinstance(data, dict) or not isinstance(data.get("hotwords", []), list) \
            or not isinstance(data.get("replacements", []), list) or not isinstance(data.get("stream", {}), dict):
        raise ValueError("lexicon must be an object with hotwords/replacements lists and a stream object")
    for r in data.get("replacements", []):
        re.compile(r["from"])
    with LEXICON_LOCK:
        cur = lexicon_state()
        if cur["sha256"] != (base_sha256 or ""):
            raise ValueError("the lexicon changed on the server meanwhile; read it again")
        os.makedirs(os.path.dirname(LEXICON), exist_ok=True)
        if cur["sha256"]:
            hist = os.path.join(os.path.dirname(LEXICON), "lexicon-history")
            os.makedirs(hist, exist_ok=True)
            shutil.copyfile(LEXICON, os.path.join(hist, f"lexicon-{dt.datetime.now():%Y%m%d-%H%M%S-%f}.json"))
            for old in sorted(os.listdir(hist))[:-50]:
                os.remove(os.path.join(hist, old))
        tmp = LEXICON + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, LEXICON)
        log.info("lexicon replaced by a client (%d hotwords, %d replacements)",
                 len(data.get("hotwords", [])), len(data.get("replacements", [])))
        return lexicon_state()


LEXICON_LOCK = threading.Lock()


class Pcm:
    """Append-only float32 buffer with amortized growth."""

    def __init__(self):
        self.buf = np.zeros(SR * 30, dtype=np.float32)
        self.n = 0

    def append(self, x: np.ndarray):
        need = self.n + len(x)
        if need > len(self.buf):
            nb = np.zeros(max(need, len(self.buf) * 2), dtype=np.float32)
            nb[:self.n] = self.buf[:self.n]
            self.buf = nb
        self.buf[self.n:need] = x
        self.n = need

    def __len__(self):
        return self.n

    def __getitem__(self, s: slice):
        return self.buf[:self.n][s]


class Stream:
    """One recording. VAD speech segments are merged into units of a few
    seconds (Qwen keeps more context that way); each unit is decoded by Qwen
    once it closes and then committed. Paraformer drafts fill the tentative
    text meanwhile. The trailing punctuation of the newest unit is held back
    until the next unit shows how long the pause was."""

    def __init__(self, eng: Engine, run: dict):
        self.eng = eng
        self.cfg = eng.stream_cfg()
        self.vocab = list(run.get("vocabulary") or [])
        self.language = run.get("language")
        self.vad = eng.new_vad(self.cfg["vad_min_silence_s"])
        self.vad_buf = np.zeros(0, dtype=np.float32)
        self.audio = Pcm()
        self.para = eng.para.create_stream()
        self.unit: list[int] | None = None  # [start, end] samples of the open unit
        self.cover = 0      # audio before this sample belongs to a closed unit's clip
        self.covered = 0    # samples decoded by some unit (for the audit)
        self.units: list[dict] = []
        self.committed_n = 0
        self.committed = ""
        self.held = ""
        self.prev_end: int | None = None
        self.revision = 0
        self.last_text = ("", "")
        self._draft_in = self._draft_out = ""
        self.t0 = time.time()
        # Qwen previews of the open region (audio since the last closed unit; Paraformer covers the same span)
        self.region_start = 0
        self.preview: dict | None = None      # in flight: future, start, end, para_len
        self.preview_res: dict | None = None  # newest finished preview of the current region
        self.n_preview = 0
        self.preview_ms: list[int] = []
        self.allow_preview = True

    def _para_text(self) -> str:
        return self.eng.para.get_result(self.para).strip()

    def _merge_preview(self, para: str) -> str:
        """Qwen text for the audio the newest preview covered + the Paraformer words heard since."""
        p = self.preview_res
        if not p or p["start"] != self.region_start:
            return para if self.cfg.get("para_draft", 1) else ""
        tail = strip_overlap(p["text"], para[p["para_len"]:].strip())
        if not self.cfg.get("draft_para_tail", 1):
            tail = ""
        text = p["text"].strip()
        if tail:
            text = text.rstrip(TRAIL_PUNCT)
        return join_text([text, tail])

    def _close_unit(self, b: int | None = None):
        start, end = self.unit
        self.unit = None
        a = max(0, start - PAD_BEFORE)
        # The client already cut out silence, so unclaimed audio before the unit that a VAD as sensitive as
        # the client's calls speech is soft speech the server VAD missed: the unit takes it.
        lb = int(self.cfg.get("unit_lookback_s", 0) * SR)
        if lb > 0 and a > self.cover:
            sp = self._soft_speech(max(self.cover, a - lb), a)
            if sp:
                a = max(self.cover, sp[0] - PAD_BEFORE)
        b = min(len(self.audio), end + PAD_AFTER) if b is None else b
        # the preview (and so the draft) heard everything since the previous unit: kept for the head check
        wa = max(self.cover, self.region_start - PAD_BEFORE, b - int(MAX_CHUNK_S * SR))
        wide = self.audio[wa:b].copy() if wa < a else None
        self.covered += max(0, b - max(a, self.cover))
        self.cover = max(self.cover, b)
        clip = self.audio[a:b].copy()
        with self.eng.para_lock:
            draft = self._para_text()
            self.para = self.eng.para.create_stream()
        draft = self._merge_preview(draft)
        gap = None if self.prev_end is None else (start - self.prev_end) / SR
        self.prev_end = end
        n = int(self.cfg.get("unit_context_chars", 0))
        idx = len(self.units)
        p = self.preview_res
        draft_ids = p.get("ids") if p and p["start"] == self.region_start and self.cfg.get("spec_draft", 1) else None
        fut = self.eng.submit(clip, self.vocab, self.language,
                              (lambda: self._prior_text(idx, n)) if n > 0 else None, draft_ids=draft_ids,
                              prefix_cache=bool(self.cfg.get("prefix_cache", 1)))
        self.region_start = end
        self.preview_res = None
        self.units.append({"future": fut, "draft": draft, "text": None, "lang": None, "gap": gap,
                           "dur": len(clip) / SR, "closed_at": time.time(), "wide": wide})

    def _soft_speech(self, a: int, b: int) -> tuple[int, int] | None:
        """First start and last end of the speech (spans of 0.3 s or more) that a VAD as sensitive as the
        client's hears in audio[a:b]; None if it hears none."""
        thr = float(self.cfg.get("soft_vad_threshold", 0) or 0)
        if thr <= 0 or b - a < int(0.3 * SR):
            return None
        vad = self.eng.new_vad(self.cfg["vad_min_silence_s"], threshold=thr)
        x = self.audio[a:b]
        for i in range(0, len(x) // 512 * 512, 512):
            vad.accept_waveform(x[i:i + 512])
        vad.flush()
        spans = []
        while not vad.empty():
            s = vad.front
            if len(s.samples) >= int(0.3 * SR):
                spans.append((a + s.start, a + s.start + len(s.samples)))
            vad.pop()
        return (spans[0][0], spans[-1][1]) if spans else None

    def _prior_text(self, idx: int, n: int) -> str:
        """The last `n` characters said before unit `idx`: Qwen finals where done, Paraformer drafts otherwise."""
        parts = []
        for u in self.units[:idx]:
            f = u["future"]
            if f.done() and not f.cancelled() and f.exception() is None:
                parts.append(f.result()["text"])
            else:
                parts.append(u["draft"])
        return join_text(parts)[-n:]

    def _maybe_preview(self):
        if not self.allow_preview:
            return
        p = self.preview
        if p and p["future"].done():
            self.preview = None
            try:
                r = p["future"].result()
            except Aborted:
                r = None
            except Exception as e:
                log.warning("preview failed: %s", str(e).splitlines()[0][:200])
                r = None
            if r and p["start"] == self.region_start:
                self.preview_res = {**p, "text": r["text"], "ids": r.get("ids")}
                self.preview_ms.append(round((time.time() - p["t"]) * 1000))
                if config.env_flag("DEBUG_PREVIEW", False) or os.path.exists(os.path.join(CFG.home, "debug_preview")):
                    with self.eng.para_lock:
                        audit("preview", end_s=round(p["end"] / SR, 2), qwen=r["text"], para_at=p["para_snap"],
                              para_now=self._para_text())
        iv = self.cfg.get("preview_interval_s", 0)
        if iv <= 0 or self.preview:
            return
        npu = bool(self.cfg.get("preview_device", 0)) and self.eng.npu is not None
        yield_ = not npu or bool(self.cfg.get("npu_yield", 0))
        if yield_ and any(not u["future"].done() for u in self.units[self.committed_n:]):
            return  # finals first
        now, start = len(self.audio), self.region_start
        if now - start < self.cfg.get("preview_min_s", 1.0) * SR \
                or now - start > self.cfg.get("preview_max_s", 15.0) * SR:
            return
        last = self.preview_res["end"] if self.preview_res else start
        if now - last < iv * SR:
            return
        with self.eng.para_lock:
            para = self._para_text()
        if not para and self.unit is None:
            return  # nothing heard yet
        clip = self.audio[max(0, start - PAD_BEFORE):now].copy()
        ctl = {"keep": False}
        if npu:
            k = int(self.cfg.get("npu_rollback", 5))
            prev = self.preview_res.get("ids") if self.preview_res and k > 0 else None
            fut = self.eng.submit_npu_preview(clip, self.vocab, self.language,
                                              self.eng.npu.rollback(prev, k) if prev else [], yield_)
        else:
            fut = self.eng.submit_preview(clip, self.vocab, self.language, ctl)
        self.preview = {"future": fut, "ctl": ctl, "t": time.time(),
                        "start": start, "end": now, "para_len": len(para), "para_snap": para}
        self.n_preview += 1

    def _add_speech(self, start: int, end: int):
        if self.unit and end - self.unit[0] > MAX_SEGMENT_S * SR:
            self._close_unit()
        if self.unit is None:
            self.unit = [start, end]
        else:
            self.unit[1] = end
        if self.unit[1] - self.unit[0] >= self.cfg["unit_min_s"] * SR:
            self._close_unit()

    def _drain_vad(self):
        while not self.vad.empty():
            seg = self.vad.front
            self._add_speech(seg.start, seg.start + len(seg.samples))
            self.vad.pop()

    def _commit(self, text: str, gap: float | None):
        self.committed, self.held = commit_unit(
            self.eng.punct if self.cfg.get("grammar_punct", 1) else None, self.committed, self.held, text, gap,
            self.cfg["comma_gap_s"], fillers=bool(self.cfg.get("drop_fillers", 1)),
            grammar=bool(self.cfg.get("grammar_punct", 1)), stop_gap=self.cfg.get("stop_gap_s", 1.4))

    def _harvest(self, wait: bool):
        while self.committed_n < len(self.units):
            u = self.units[self.committed_n]
            if not wait and not u["future"].done():
                break
            try:
                r = u["future"].result()
            except Exception as e:
                # Qwen failed twice on this unit (see transcribe); keep its draft rather than lose the recording.
                log.error("unit %d final failed: %s; keeping its draft", self.committed_n, str(e).splitlines()[0][:200])
                r = {"text": u["draft"], "raw": None, "timings": {"failed": 1}}
            r = self._check_head(u, r)
            u["text"], u["lang"] = r["text"], r.get("lang_code")
            audit("unit", dur=round(u["dur"], 2), gap=u["gap"], latency=round(time.time() - u["closed_at"], 2),
                  timings={k: round(v, 3) if isinstance(v, float) else v for k, v in r["timings"].items()},
                  draft=u["draft"], raw=r.get("raw"), text=u["text"], lang=u["lang"])
            self._commit(u["text"], u["gap"])
            self.committed_n += 1

    def _check_head(self, u: dict, r: dict) -> dict:
        """The final decodes only the unit's VAD span; when it lacks words the draft heard (soft speech at
        the start the VAD missed), decode the wider audio the preview heard and keep whichever agrees
        better with the draft."""
        wide, raw = u.pop("wide", None), r.get("raw")
        if wide is None or raw is None or not self.cfg.get("head_check", 1):
            return r
        d, w = word_chars(u["draft"]), word_chars(raw)
        if len(d) < 3:
            return r
        sm = difflib.SequenceMatcher(None, d, w, autojunk=False)
        first = next((m for m in sm.get_matching_blocks() if m.size >= 2), None)
        head = first.a - first.b if first else len(d)
        lost = len(d) - len(w)
        if head < 2 and not (lost >= 3 and lost >= 0.25 * len(d)):
            return r
        try:
            r2 = self.eng.submit(wide, self.vocab, self.language, prefix_cache=False).result()
        except Exception as e:
            log.warning("head re-decode failed: %s", str(e).splitlines()[0][:200])
            return r
        better = difflib.SequenceMatcher(None, d, word_chars(r2["raw"]), autojunk=False).ratio() > sm.ratio()
        log.info("final lacks draft words (head %d, lost %d): %r vs draft %r; wide %.1fs -> %r (%s)", head, lost,
                 raw[:40], u["draft"][:40], len(wide) / SR, r2["raw"][:40], "kept" if better else "dropped")
        if not better:
            r.setdefault("timings", {})["head_retry"] = 0
            return r
        r2.setdefault("timings", {})["head_retry"] = 1
        u["dur"] = len(wide) / SR
        return r2

    def _snapshot(self):
        pending = [u["draft"] for u in self.units[self.committed_n:]]
        with self.eng.para_lock:
            cur = self._para_text()
        cur = self._merge_preview(cur)
        rest = join_text(pending + [cur])
        if rest != self._draft_in:
            self._draft_in = rest
            self._draft_out = DRAFT_REPEAT.sub(r"\1", self.eng.lexicon.fix_draft(rest))
        rest = self._draft_out
        tentative = self.held + rest if rest else ""
        if self.committed and not self.held and rest and re.match(r"[A-Za-z0-9]", rest[0]) \
                and re.search(r"[A-Za-z0-9.,;:!?]$", self.committed):
            tentative = " " + rest
        return self.committed, tentative

    def _update(self, final: bool):
        committed, tentative = self._snapshot() if not final else (self.committed, "")
        cc = committed != self.last_text[0]
        tc = tentative != self.last_text[1]
        if cc or tc:
            self.revision += 1
        self.last_text = (committed, tentative)
        ms = int(len(self.audio) * 1000 / SR)
        update = {"result_changed": cc or tc, "is_final": final, "revision": self.revision,
                  "input_received_ms": ms, "audio_committed_ms": ms, "buffered_ms": 0,
                  "committed_changed": cc, "tentative_changed": tc}
        text = {"full": committed + tentative, "committed": committed, "tentative": tentative}
        return update, text, (cc or tc)

    def feed(self, pcm: np.ndarray) -> dict:
        if len(pcm):
            self.audio.append(pcm)
            with self.eng.para_lock:
                self.para.accept_waveform(SR, pcm)
                while self.eng.para.is_ready(self.para):
                    self.eng.para.decode_stream(self.para)
            self.vad_buf = np.concatenate([self.vad_buf, pcm])
            w = 512
            n = len(self.vad_buf) // w * w
            if n:
                # Silero VAD (sherpa-onnx) loses the start of speech that arrives in one large
                # buffer (e.g. audio queued while the worker connected), so feed ~0.1 s at a time.
                for i in range(0, n, 3 * w):
                    self.vad.accept_waveform(self.vad_buf[i:min(i + 3 * w, n)])
                self.vad_buf = self.vad_buf[n:]
            self._drain_vad()
            if self.unit and not self.vad.is_speech_detected() \
                    and len(self.audio) - self.unit[1] >= self.cfg["unit_pause_s"] * SR:
                self._close_unit()
        self._harvest(wait=False)
        self._maybe_preview()
        update, text, changed = self._update(final=False)
        return {"Fed": {"update": update, "text": text if changed else None}}

    def finalize(self, want_language: bool) -> dict:
        if len(self.vad_buf):
            self.vad.accept_waveform(self.vad_buf)
            self.vad_buf = np.zeros(0, dtype=np.float32)
        self.vad.flush()
        self._drain_vad()
        if self.unit:
            # Trailing words too soft for the server VAD still belong to the last unit.
            b = min(len(self.audio), self.unit[1] + PAD_AFTER)
            sp = self._soft_speech(b, len(self.audio))
            self._close_unit(min(len(self.audio), sp[1] + PAD_AFTER) if sp else None)
        elif self.units:
            sp = self._soft_speech(self.cover, len(self.audio))
            if sp:
                self.unit = list(sp)
                self._close_unit()
        if not self.units and len(self.audio) > SR // 2:
            # VAD heard nothing: let Qwen judge the whole clip once.
            self.unit = [0, len(self.audio)]
            self._close_unit()
        self._harvest(wait=True)
        self.committed += self.held
        self.held = ""
        update, text, _ = self._update(final=True)
        lang = None
        if want_language:
            langs = [u["lang"] for u in self.units if u.get("lang")]
            lang = max(set(langs), key=langs.count) if langs else None
        audit("stream", audio_s=round(len(self.audio) / SR, 2), units=len(self.units),
              uncovered_s=round((len(self.audio) - min(self.covered, len(self.audio))) / SR, 2),
              previews=self.n_preview,
              preview_ms=(sorted(self.preview_ms)[len(self.preview_ms) // 2] if self.preview_ms else None),
              preview_dev="npu" if self.cfg.get("preview_device", 0) and self.eng.npu else "gpu",
              wall_s=round(time.time() - self.t0, 2), text=text["full"])
        if CFG.warmup_capture and text["full"].strip():
            save_warmup_clip(self.audio[0:len(self.audio)])
        return {"Finalized": {"update": update, "text": text, "language": lang}}


def save_warmup_clip(pcm: np.ndarray):
    """Keep the first 3-8 s dictation as the local warm-up clip, so later model loads warm the real path."""
    if os.path.exists(WARMUP_WAV) or not 3 * SR <= len(pcm) <= 8 * SR:
        return
    try:
        os.makedirs(os.path.dirname(WARMUP_WAV), exist_ok=True)
        tmp = WARMUP_WAV + ".tmp"
        with wave.open(tmp, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes((np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes())
        os.replace(tmp, WARMUP_WAV)
        log.info("saved %.1fs warm-up clip to %s", len(pcm) / SR, WARMUP_WAV)
    except OSError as e:
        log.warning("could not save the warm-up clip: %s", e)


def batch_run(eng: Engine, pcm: np.ndarray, options: dict) -> dict:
    t = time.time()
    vocab = list(options.get("vocabulary") or [])
    language = options.get("language")
    chunks = []
    if len(pcm) <= MAX_CHUNK_S * SR:
        chunks = [pcm]
    else:
        vad = eng.new_vad()
        # Silero VAD must be fed window by window; one whole-buffer call yields a single bogus span.
        for i in range(0, len(pcm), 512):
            vad.accept_waveform(pcm[i:i + 512])
        vad.flush()
        spans = []
        while not vad.empty():
            s = vad.front
            spans.append([max(0, s.start - PAD_BEFORE), min(len(pcm), s.start + len(s.samples) + PAD_AFTER)])
            vad.pop()
        merged = []
        for a, b in spans:
            if merged and b - merged[-1][0] <= MAX_CHUNK_S * SR:
                merged[-1][1] = b
            else:
                merged.append([a, b])
        # Chunks meet halfway between speech spans (up to MAX_CHUNK_S), so soft speech between them is kept.
        lim = int(MAX_CHUNK_S * SR)
        for j in range(len(merged) - 1):
            mid = (merged[j][1] + merged[j + 1][0]) // 2
            merged[j][1] = max(merged[j][1], min(mid, merged[j][0] + lim))
            merged[j + 1][0] = min(merged[j + 1][0], max(mid, merged[j + 1][1] - lim))
        if merged:
            merged[0][0] = min(merged[0][0], max(0, merged[0][1] - lim))
            merged[-1][1] = max(merged[-1][1], min(len(pcm), merged[-1][0] + lim))
        chunks = [pcm[a:b] for a, b in merged] or [pcm]
    futs = [eng.submit(c, vocab, language) for c in chunks]
    res = [f.result() for f in futs]
    text = join_text([r["text"] for r in res])
    langs = [r.get("lang_code") for r in res if r.get("lang_code")]
    lang = None if language else (max(set(langs), key=langs.count) if langs else None)
    audit("run", audio_s=round(len(pcm) / SR, 2), chunks=len(chunks), wall_s=round(time.time() - t, 2), text=text)
    return {"Transcript": {"text": text, "raw_text": text, "language": lang, "timestamp_kind": "None",
                           "segments": [{"t0_ms": 0, "t1_ms": int(len(pcm) * 1000 / SR), "first_word": 0,
                                         "n_words": 0, "first_token": 0, "n_tokens": 0, "text": text,
                                         "speaker_id": 0}]}}


DEVICE = {"name": CFG.device_id, "description": f"{CFG.label} · Qwen3-ASR-1.7B (OpenVINO {CFG.final_device})",
          "kind": "remote", "device_type": "Cpu", "device_id": None, "memory_total": 0, "index": 0,
          "key": json.dumps(["remote", "name", CFG.device_id], separators=(",", ":"))}


def status_info() -> dict:
    """Cheap health report for the client's connection test; never wakes the models."""
    import openvino
    with LIFE_LOCK:
        life = dict(LIFE)
    return {"label": CFG.label, "device_id": CFG.device_id, "description": DEVICE["description"],
            "loaded": ENGINE_READY.is_set() and ENGINE is not None, "loading": bool(life["loading"]),
            "idle_s": round(time.time() - life["last_use"], 1), "openvino": openvino.get_version(),
            "final_device": CFG.final_device, "draft": CFG.draft_enabled}


def loaded_info() -> dict:
    return {"arch": "qwen3_asr", "variant": f"qwen3-asr-1.7b@{CFG.device_id}",
            "backend": f"{CFG.device_id}-openvino",
            "device": DEVICE["description"], "on_gpu": False,
            "capabilities": {"native_sample_rate": SR, "languages": list(LANG_NAMES.keys()),
                             "translate_target_languages": [], "max_timestamp_kind": "None",
                             "supports_language_detect": True, "supports_translate": False,
                             "supports_streaming": True, "supports_spec_decode": False, "max_audio_ms": 0},
            "supports_initial_prompt": False, "supports_long_form": True, "supports_vocabulary": True}


def read_exact(sock_file, n: int) -> bytes:
    b = sock_file.read(n)
    if b is None or len(b) < n:
        raise EOFError
    return b


def read_frame(f):
    head = f.read(4)
    if not head:
        return None
    if len(head) < 4:
        raise EOFError
    (jl,) = struct.unpack("<I", head)
    msg = json.loads(read_exact(f, jl).decode("utf-8"))
    (pl,) = struct.unpack("<I", read_exact(f, 4))
    pcm = np.frombuffer(read_exact(f, pl), dtype="<f4").astype(np.float32) if pl else np.zeros(0, np.float32)
    return msg, pcm


def write_frame(f, msg):
    j = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    f.write(struct.pack("<I", len(j)) + j + struct.pack("<I", 0))
    f.flush()


def kind_of(msg):
    if isinstance(msg, str):
        return msg, {}
    k = next(iter(msg))
    return k, msg[k] or {}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        peer = self.client_address
        log.info("client connected %s", peer)
        stream: LazyStream | None = None
        loaded = False

        def end_stream():
            nonlocal stream
            if stream:
                stream.close()
            stream = None
        try:
            while True:
                fr = read_frame(self.rfile)
                if fr is None:
                    break
                msg, pcm = fr
                k, body = kind_of(msg)
                try:
                    if k == "Status":
                        resp = {"Status": status_info()}
                    elif k == "Hello":
                        resp = {"Hello": {"devices": [DEVICE] if body.get("list_devices") else None}}
                    elif k == "Load":
                        # Never block the client handshake on a cold model load; StreamBegin/LazyStream
                        # buffers audio and shows a waking banner while the models load in the background.
                        touch()
                        ensure_engine()
                        loaded = True
                        resp = {"Loaded": loaded_info()}
                    elif k == "AuditQuery":
                        resp = {"Audit": {"records": audit_around(float(body["ts"]), float(body.get("window_s", 600)))}}
                    elif k == "LexiconGet":
                        resp = {"Lexicon": lexicon_state()}
                    elif k == "LexiconPut":
                        resp = {"Lexicon": lexicon_put(body.get("data"), body.get("base_sha256", ""))}
                    elif not loaded:
                        resp = {"Error": "no model loaded"}
                    elif k == "Run":
                        with LIFE_LOCK:
                            LIFE["busy"] += 1
                        try:
                            eng = acquire_engine(LOAD_WAIT_S)
                            resp = batch_run(eng, pcm, body.get("options") or {}) if eng else \
                                {"BackendError": "ASR server: the models did not load in time"}
                        finally:
                            with LIFE_LOCK:
                                LIFE["busy"] -= 1
                            touch()
                    elif k == "StreamBegin":
                        end_stream()
                        stream = LazyStream(body.get("run") or {})
                        resp = "Ok"
                    elif k == "Feed":
                        resp = stream.feed(pcm) if stream else {"Error": "no active stream"}
                    elif k == "Finalize":
                        if stream:
                            resp = stream.finalize(bool(body.get("want_language")))
                            stream = None
                        else:
                            resp = {"Error": "no active stream"}
                    elif k == "StreamReset":
                        end_stream()
                        resp = "Ok"
                    else:
                        resp = {"Error": f"unknown request {k}"}
                except Exception as e:
                    log.error("request %s failed: %s\n%s", k, e, traceback.format_exc())
                    resp = {"BackendError": f"ASR server: {e}"}
                    if k == "Finalize":
                        end_stream()
                write_frame(self.wfile, resp)
        except (EOFError, ConnectionError, OSError) as e:
            log.info("client %s gone: %s", peer, e)
        finally:
            end_stream()
            log.info("client closed %s", peer)


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False


def main():
    prune_logs()
    srv = Server((CFG.bind, PORT), Handler)
    log.info("listening on %s:%d (pid %d; home %s; config %s)", CFG.bind, PORT, os.getpid(), CFG.home,
             ", ".join(CFG.sources) or "defaults")
    if CFG.bind not in ("127.0.0.1", "::1", "localhost"):
        log.warning("bound to %s: the protocol has no authentication; prefer 127.0.0.1 + ssh -W", CFG.bind)
    if in_night() and not CFG.load_in_offline_window:
        LIFE["reason"] = "night"
        log.info("offline window: models load at %02d:00 or on the first request", NIGHT_END)
    else:
        ensure_engine()
    threading.Thread(target=lifecycle, name="lifecycle", daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
