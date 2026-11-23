"""Qwen3-ASR-1.7B (OpenVINO INT8, explicit KV cache) inference.

Model: dseditor/Qwen3-ASR-1.7B-INT8_OpenVINO. The encoder has a fixed 10 s
window (mel 128x1000 -> 130 audio tokens), so callers pass <= 10 s clips.
"""
from __future__ import annotations

import json
import os
import time

import numpy as np
import openvino as ov
from tokenizers import Tokenizer, decoders, models, pre_tokenizers

from config import env_flag

SR = 16000
N_FFT = 400
HOP = 160
N_MELS = 128


def _hz_to_mel(f):
    f = np.asarray(f, dtype=np.float64)
    f_sp = 200.0 / 3
    mels = f / f_sp
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0
    return np.where(f >= min_log_hz, min_log_mel + np.log(np.maximum(f, 1e-10) / min_log_hz) / logstep, mels)


def _mel_to_hz(m):
    m = np.asarray(m, dtype=np.float64)
    f_sp = 200.0 / 3
    freqs = f_sp * m
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0
    return np.where(m >= min_log_mel, min_log_hz * np.exp(logstep * (m - min_log_mel)), freqs)


def _mel_filters():
    # Slaney-style filterbank, identical to Whisper/librosa defaults.
    fftfreqs = np.linspace(0, SR / 2, 1 + N_FFT // 2)
    mel_f = _mel_to_hz(np.linspace(_hz_to_mel(0.0), _hz_to_mel(SR / 2), N_MELS + 2))
    fdiff = np.diff(mel_f)
    ramps = mel_f[:, None] - fftfreqs[None, :]
    weights = np.zeros((N_MELS, len(fftfreqs)))
    for i in range(N_MELS):
        lower = -ramps[i] / fdiff[i]
        upper = ramps[i + 2] / fdiff[i + 1]
        weights[i] = np.maximum(0, np.minimum(lower, upper))
    enorm = 2.0 / (mel_f[2: N_MELS + 2] - mel_f[:N_MELS])
    weights *= enorm[:, None]
    return weights.astype(np.float32)


_FILTERS = _mel_filters()
_WINDOW = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(N_FFT) / N_FFT)).astype(np.float32)


def log_mel(audio: np.ndarray, n_samples: int | None) -> np.ndarray:
    if n_samples:
        a = np.zeros(n_samples, dtype=np.float32)
        a[: min(len(audio), n_samples)] = audio[:n_samples]
    else:
        a = audio.astype(np.float32)
        # The encoder windows mel in 100-frame (1 s) blocks.
        a = np.pad(a, (0, (-len(a)) % SR))
    padded = np.pad(a, (N_FFT // 2, N_FFT // 2), mode="reflect")
    n_frames = 1 + (len(padded) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n_frames)[:, None]
    frames = padded[idx] * _WINDOW
    mag = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    mag = mag[:-1].T  # (201, frames-1)
    mel = _FILTERS @ mag
    log_spec = np.log10(np.maximum(mel, 1e-10))
    log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
    return ((log_spec + 4.0) / 4.0).astype(np.float32)


class Aborted(Exception):
    pass


def _with_mask(model: ov.Model) -> ov.Model:
    """Give every attention an explicit [1, 1, new, past+new] mask input: the export assumed one new
    token, so it had no causal mask. Zeros reproduce the original single-token graph."""
    sdpa = [o for o in model.get_ordered_ops() if o.get_type_name() == "ScaledDotProductAttention"]
    mask = ov.opset13.parameter([1, 1, -1, -1], sdpa[0].input_value(3).get_element_type(), name="attn_mask")
    mask.output(0).get_tensor().set_names({"attn_mask"})
    for o in sdpa:
        o.input(3).replace_source_output(mask.output(0))
    model.add_parameters([mask])
    model.validate_nodes_and_infer_types()
    return model


def _all_position_logits(model: ov.Model) -> ov.Model:
    """Remove the Slice that keeps only the last position in front of lm_head. The GPU plugin (2026.4)
    corrupts memory when this graph's new-token count varies between calls, so it is always fed
    exactly SPEC_ROWS rows."""
    mm = model.outputs[0].get_node().input_value(0).get_node()
    sl = mm.input_value(0).get_node()
    if mm.get_type_name() != "MatMul" or sl.get_type_name() != "Slice":
        raise RuntimeError("unexpected decoder head layout")
    mm.input(0).replace_source_output(sl.input_value(0))
    model.validate_nodes_and_infer_types()
    return model


def _causal_mask(past: int, n: int) -> np.ndarray:
    return np.triu(np.full((n, past + n), -np.inf, dtype=np.float32), past + 1)[None, None]


SPEC_ROWS = 9  # current token + up to 8 drafted ones per verify call


def _next_draft(draft: list[int], p: int, gen: list[int]) -> int:
    """Index in `draft` that should follow `gen`: advance on a match, else resync on the last two
    generated tokens (prompt-lookup style); len(draft) when the draft has nothing more to offer."""
    if p < len(draft) and draft[p] == gen[-1]:
        return p + 1
    tail = gen[-2:]
    for j in range(len(draft) - len(tail) + 1):
        if draft[j:j + len(tail)] == tail:
            return j + len(tail)
    return len(draft)


class Qwen3AsrOV:
    def __init__(self, model_dir: str, device: str = "CPU", encoder_device: str | None = None,
                 cache_dir: str | None = None, stateful: bool = True):
        self.dir = model_dir
        core = ov.Core()
        if cache_dir:
            core.set_property({"CACHE_DIR": cache_dir})
        cfg = {"PERFORMANCE_HINT": "LATENCY"}
        enc_dev = encoder_device or device
        t = time.time()
        self.enc = core.compile_model(os.path.join(model_dir, "audio_encoder_model.xml"), enc_dev, cfg)
        self.emb = core.compile_model(os.path.join(model_dir, "thinker_embeddings_model.xml"), "CPU", cfg)
        self.prefill = core.compile_model(os.path.join(model_dir, "decoder_prefill_kv_model.xml"), device, cfg)
        step_model = core.read_model(os.path.join(model_dir, "decoder_kv_model.xml"))
        self.stateful = stateful
        # Explicit KV only: the step graph gets a mask input, so it can also prefill a multi-token prompt
        # tail on top of cached KV, and a copy returns logits for every new position, so one call can
        # verify several drafted tokens (speculative decoding with the NPU preview).
        self.masked = not stateful and env_flag("SPEC")
        if stateful:
            from openvino._offline_transformations import apply_make_stateful_transformation
            step_model.outputs[0].get_tensor().set_names({"logits"})
            step_model.outputs[1].get_tensor().set_names({"present_keys"})
            step_model.outputs[2].get_tensor().set_names({"present_values"})
            apply_make_stateful_transformation(step_model, {"past_keys": "present_keys",
                                                            "past_values": "present_values"})
        elif self.masked:
            step_model = _with_mask(step_model)
        self.step = core.compile_model(step_model, device, cfg)
        self.step_all = None
        if self.masked:
            self.step_all = core.compile_model(_all_position_logits(_with_mask(
                core.read_model(os.path.join(model_dir, "decoder_kv_model.xml")))), device, cfg)
        self.load_seconds = time.time() - t
        self.enc_req = self.enc.create_infer_request()
        self.emb_req = self.emb.create_infer_request()
        self.pre_req = self.prefill.create_infer_request()
        self.step_req = self.step.create_infer_request()
        self._make_pingpong()
        with open(os.path.join(model_dir, "prompt_template.json"), encoding="utf-8") as f:
            self.tpl = json.load(f)
        self.n_samples = int(self.tpl["n_samples"])
        self.special = set(self.tpl["special_ids"])
        self.eos = {int(self.tpl["eos_id"]), int(self.tpl["eot_id"]), 151643}
        self.asr_text_id = int(self.tpl["asr_text_id"])
        tok = Tokenizer(models.BPE.from_file(os.path.join(model_dir, "vocab.json"),
                                             os.path.join(model_dir, "merges.txt")))
        tok.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False, use_regex=True)
        tok.decoder = decoders.ByteLevel()
        self.tok = tok
        self.device = device
        self.pad_to_window = False
        self.prefix_cache = env_flag("PREFIX_CACHE")
        self.share = env_flag("SHARE")

    def _make_pingpong(self):
        # Two requests per graph, alternating: each step reads the KV the other one just wrote
        # (shared buffers), so Python never copies the cache.
        self.pp = [self.step.create_infer_request() for _ in range(2)]
        self.pp_all = [self.step_all.create_infer_request() for _ in range(2)] if self.step_all else None
        self.tail_req = self.step.create_infer_request() if self.masked else None
        self._pcache = None  # (prompt ids before the audio, their keys, their values)

    def _prefill_cached(self, prefix: list[int], audio_emb: np.ndarray, suf_e: np.ndarray):
        """Prefill that reuses the KV of the prompt text (system prompt + hotwords, identical for every
        unit) and only runs the new tail - the changed context end, the audio and the suffix - through
        the masked step graph (which keeps only the last position's logits)."""
        c, n = self._pcache, 0
        if c:
            lim = min(len(c[0]), len(prefix))
            while n < lim and c[0][n] == prefix[n]:
                n += 1
        if n < 8:
            o = self.pre_req.infer({"input_embeds": self._embed(prefix),
                                    "position_ids": np.arange(len(prefix), dtype=np.int64)[None]})
            _, k, v = self._unpack(o)
            c = self._pcache = (list(prefix), k, v)
            n = len(prefix)
        rest = prefix[n:]
        e = np.concatenate(([self._embed(rest)] if rest else []) + [audio_emb.astype(np.float32), suf_e], axis=1)
        o = self.tail_req.infer({"new_embed": e, "new_pos": np.arange(n, n + e.shape[1], dtype=np.int64)[None],
                                    "past_keys": np.ascontiguousarray(c[1][:, :, :, :n]),
                                    "past_values": np.ascontiguousarray(c[2][:, :, :, :n]),
                                    "attn_mask": _causal_mask(n, e.shape[1])})
        lg, keys, values = self._unpack(o)
        if rest:
            self._pcache = (list(prefix), np.ascontiguousarray(keys[:, :, :, :len(prefix)]),
                            np.ascontiguousarray(values[:, :, :, :len(prefix)]))
        return lg[:, -1:], keys, values, n

    def reset_requests(self):
        self.enc_req, self.emb_req = self.enc.create_infer_request(), self.emb.create_infer_request()
        self.pre_req, self.step_req = self.prefill.create_infer_request(), self.step.create_infer_request()
        self._make_pingpong()

    def _embed(self, ids: list[int]) -> np.ndarray:
        out = self.emb_req.infer({0: np.asarray([ids], dtype=np.int64)})
        return next(iter(out.values()))

    def _encode_text(self, text: str) -> list[int]:
        return self.tok.encode(text).ids if text else []

    def transcribe(self, audio: np.ndarray, context: str = "", language: str | None = None,
                   max_new_tokens: int = 256, abort=None, draft_ids: list[int] | None = None,
                   spec_k: int = 8, spec_margin: float = 1.0, prefix_cache: bool | None = None) -> dict:
        """abort: optional callable polled before the encoder and between decode steps;
        returning True raises Aborted (used to preempt low-priority previews).
        draft_ids: a guess at the generated ids (the NPU preview of the same audio). Each step then
        feeds the next token plus up to spec_k draft tokens and keeps the longest prefix this model
        would have produced itself, so the output is the same as plain greedy decoding."""
        if abort and abort():
            raise Aborted()
        timings = {}
        t0 = time.perf_counter()
        mel = log_mel(audio.astype(np.float32), self.n_samples if self.pad_to_window else None)
        timings["mel"] = time.perf_counter() - t0

        t = time.perf_counter()
        audio_emb = next(iter(self.enc_req.infer({0: mel}).values()))
        timings["encoder"] = time.perf_counter() - t
        if abort and abort():
            raise Aborted()

        prefix = list(self.tpl["prefix_ids"])
        # prefix = <|im_start|> system \n [context] <|im_end|> \n <|im_start|> user \n <|audio_start|>
        prefix = prefix[:3] + self._encode_text(context) + prefix[3:]
        suffix = list(self.tpl["suffix_ids"])
        if language and language in self.tpl["language_suffix_ids"]:
            suffix += list(self.tpl["language_suffix_ids"][language])

        t = time.perf_counter()
        suf_e = self._embed(suffix)
        if self.tail_req is not None and (self.prefix_cache if prefix_cache is None else prefix_cache):
            logits, keys, values, reused = self._prefill_cached(prefix, audio_emb, suf_e)
            timings["prefix_reused"] = reused
            L = keys.shape[3]
        else:
            pre_e = self._embed(prefix)
            embeds = np.concatenate([pre_e, audio_emb.astype(pre_e.dtype), suf_e], axis=1)
            L = embeds.shape[1]
            pos = np.arange(L, dtype=np.int64)[None]
            out = self.pre_req.infer({"input_embeds": embeds, "position_ids": pos})
            logits, keys, values = self._unpack(out)
        timings["prefill"] = time.perf_counter() - t

        t = time.perf_counter()
        generated: list[int] = []
        first_token_at = None
        cur = L
        if self.stateful:
            # A fresh request per utterance: reusing one leaves a stale KV variable
            # layout on the GPU plugin once the prefill length changes.
            self.step_req = self.step.create_infer_request()
            for st in self.step_req.query_state():
                st.state = ov.Tensor(np.array(keys if "key" in st.name else values, copy=True))
        draft = list(draft_ids or []) if self.pp_all and not self.stateful else []
        p = steps = accepted = resolves = 0
        last = logits[0, -1]
        pending = None  # a generated token whose KV is not written yet (re-fed alone to decide precisely)
        while True:
            if abort and abort():
                raise Aborted()
            prop = []
            if pending is None:
                if len(generated) >= max_new_tokens:
                    break
                nxt = int(np.argmax(last))
                if first_token_at is None:
                    first_token_at = time.perf_counter() - t0
                if nxt in self.eos:
                    break
                generated.append(nxt)
                if draft:
                    p = _next_draft(draft, p, generated)
                    prop = draft[p:p + min(spec_k, SPEC_ROWS - 1, max_new_tokens - len(generated))]
            else:
                nxt, pending = pending, None
                resolves += 1
            ids = [nxt] + prop
            # verify calls always carry SPEC_ROWS rows (see _all_position_logits); the causal mask keeps
            # the padding from affecting the real rows, and its KV is dropped below
            fed = ids + [ids[-1]] * (SPEC_ROWS - len(ids)) if prop else ids
            e = self._embed(fed)
            pos = np.arange(cur, cur + len(fed), dtype=np.int64)[None]
            if self.stateful:
                out = self.step_req.infer({"new_embed": e, "new_pos": pos})
                lg = next(iter(out.values()))
            else:
                req = (self.pp_all if prop else self.pp)[steps % 2]
                feed = {"new_embed": e, "new_pos": pos, "past_keys": keys, "past_values": values}
                if prop:
                    feed["attn_mask"] = _causal_mask(cur, len(fed))
                elif self.masked:
                    feed["attn_mask"] = np.zeros((1, 1, 1, cur + 1), np.float32)
                out = req.infer(feed, share_inputs=self.share, share_outputs=self.share)
                lg, keys, values = self._unpack(out)
            steps += 1
            # Row j predicts the token after ids[j]. The multi-token graph rounds differently from the
            # single-token one (fp16, ~0.2-0.4 logits), so a near-tie row is re-decided by feeding ids[j]
            # alone; confident rows are trusted. This keeps the output equal to plain greedy decoding.
            acc, cut = 0, None
            if prop:
                for j in range(len(ids)):
                    row = lg[0, j]
                    top2 = np.partition(row, -2)[-2:]
                    if top2[1] - top2[0] < spec_margin:
                        cut = j
                        break
                    tok = int(np.argmax(row))
                    if j < len(prop) and tok == prop[j] and tok not in self.eos:
                        generated.append(tok)
                        acc += 1
                    else:
                        break
            accepted += acc
            p += acc
            if cut is not None:
                pending = ids[cut]
                cur += cut
            else:
                cur += 1 + acc
                # copy: the shared output buffer is overwritten two steps later
                last = np.array(lg[0, acc])
            if not self.stateful and keys.shape[3] > cur:  # drop the KV of rejected draft tokens
                keys = np.ascontiguousarray(keys[:, :, :, :cur])
                values = np.ascontiguousarray(values[:, :, :, :cur])
        timings["decode"] = time.perf_counter() - t
        timings["tokens"] = len(generated)
        timings["steps"] = steps
        if draft:
            timings["draft_accepted"] = accepted
            timings["draft_resolves"] = resolves
        timings["first_token"] = first_token_at
        timings["total"] = time.perf_counter() - t0

        if language and language in self.tpl["language_suffix_ids"]:
            lang, text_ids = language, generated
        elif self.asr_text_id in generated:
            k = generated.index(self.asr_text_id)
            lang = self.tok.decode([i for i in generated[:k] if i not in self.special]).replace("language", "").strip()
            text_ids = generated[k + 1:]
        else:
            lang, text_ids = "", generated
        text = self.tok.decode([i for i in text_ids if i not in self.special]).strip()
        return {"text": text, "language": lang, "timings": timings, "ids": generated}

    @staticmethod
    def _unpack(out):
        vals = list(out.values())
        return vals[0], vals[1], vals[2]

