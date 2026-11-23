"""Qwen3-ASR-0.6B: one-second NPU encoder blocks and stateful NPUW decoder."""
from __future__ import annotations

import json
import os
import time

import numpy as np
import openvino as ov
from tokenizers import Tokenizer

from qwen_ov import Aborted, HOP, SR, log_mel


class Qwen3AsrNPU:
    """Infer requests are serialized by the caller, like Qwen3AsrOV.

    NPUW compiles the decoder for a fixed prompt window (MAX_PROMPT_LEN) and KV size
    (MAX_PROMPT_LEN + MIN_RESPONSE_LEN): prefill and every decode step cost the same whatever
    the real prompt length, so the windows should be no larger than the longest preview needs."""

    def __init__(self, model_dir: str, max_prompt_tokens: int = 1024, max_new_tokens: int = 256,
                 dec_config: dict | None = None):
        t = time.perf_counter()
        with open(os.path.join(model_dir, "handy_qwen_npu.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        expected = {"hidden_size": 1024, "feature_size": 128, "encoder_chunk_frames": 100,
                    "encoder_tokens_per_chunk": 13, "max_prompt_tokens": 1024}
        if any(cfg.get(k) != v for k, v in expected.items()):
            raise ValueError("Unsupported Qwen NPU model metadata")
        self.max_prompt_tokens = min(max_prompt_tokens, cfg["max_prompt_tokens"])
        self.max_new_tokens = max_new_tokens
        self.chunk_frames = cfg["encoder_chunk_frames"]
        self.tokens_per_chunk = cfg["encoder_tokens_per_chunk"]
        template_dir = os.path.join(os.path.dirname(model_dir), "qwen3-asr-1.7b-ov-int8")
        with open(os.path.join(template_dir, "prompt_template.json"), encoding="utf-8") as f:
            self.tpl = json.load(f)
        self.tok = Tokenizer.from_file(os.path.join(model_dir, "tokenizer.json"))
        ids = {"<|im_start|>": self.tpl["prefix_ids"][0], "<|im_end|>": self.tpl["eos_id"],
               "<|audio_start|>": self.tpl["prefix_ids"][-1],
               "<|audio_end|>": self.tpl["suffix_ids"][0],
               "<|audio_pad|>": self.tpl["audio_pad_id"], "<asr_text>": self.tpl["asr_text_id"]}
        if any(self.tok.token_to_id(k) != v for k, v in ids.items()):
            raise ValueError("NPU tokenizer does not match the Qwen prompt template")
        with open(os.path.join(model_dir, "vocab.json"), encoding="utf-8") as f:
            vocab = json.load(f)
        with open(os.path.join(template_dir, "vocab.json"), encoding="utf-8") as f:
            if vocab != json.load(f):
                raise ValueError("NPU base vocabulary does not match the Qwen prompt template")
        self.special = set(self.tpl["special_ids"])
        self.eos = {self.tpl["eos_id"], self.tpl["eot_id"], self.tok.token_to_id("<|endoftext|>")}
        self.asr_text_id = self.tpl["asr_text_id"]
        cache = os.path.join(model_dir, ".handy-npu-cache", "openvino-2026.4")
        core = ov.Core()
        enc_model = core.read_model(os.path.join(model_dir, "openvino_encoder_model.xml"))
        enc_model.reshape({"input_features": [1, cfg["feature_size"], self.chunk_frames]})
        self.enc = core.compile_model(enc_model, "NPU",
                                      {"CACHE_DIR": os.path.join(cache, "encoder"),
                                       "CACHE_MODE": "OPTIMIZE_SIZE", "PERFORMANCE_HINT": "LATENCY"})
        self.emb = core.compile_model(os.path.join(model_dir, "openvino_embeddings_model.xml"), "CPU",
                                      {"CACHE_DIR": os.path.join(cache, "embeddings"),
                                       "PERFORMANCE_HINT": "LATENCY"})
        dec_model = core.read_model(os.path.join(model_dir, "openvino_decoder_model.xml"))
        dec_model.reshape({"inputs_embeds": [1, -1, cfg["hidden_size"]],
                           "attention_mask": [1, -1], "position_ids": [1, -1], "beam_idx": [1]})
        dec_cfg = {"NPU_USE_NPUW": "YES", "NPUW_LLM": "YES", "NPUW_LLM_BATCH_DIM": 0,
                   "NPUW_LLM_SEQ_LEN_DIM": 2, "NPUW_LLM_MAX_PROMPT_LEN": self.max_prompt_tokens,
                   "NPUW_LLM_MIN_RESPONSE_LEN": self.max_new_tokens, "NPUW_LLM_PREFILL_HINT": "STATIC",
                   "CACHE_DIR": os.path.join(cache, "decoder"), "CACHE_MODE": "OPTIMIZE_SIZE"}
        dec_cfg.update(dec_config or {})
        self.dec = core.compile_model(dec_model, "NPU", dec_cfg)
        self.enc_req = self.enc.create_infer_request()
        self.emb_req = self.emb.create_infer_request()
        self.dec_req = self.dec.create_infer_request()
        self.device = "NPU"
        self.load_seconds = time.perf_counter() - t

    def _embed(self, ids: list[int], audio_emb: np.ndarray) -> np.ndarray:
        out = self.emb_req.infer({"input_ids": np.asarray([ids], dtype=np.int64),
                                  "encoder_hidden_states": audio_emb})
        return next(iter(out.values())).copy()

    def transcribe(self, audio: np.ndarray, context: str = "", language: str | None = None,
                   max_new_tokens: int | None = None, abort=None, prefix_ids: list[int] | None = None) -> dict:
        """prefix_ids: assistant tokens forced after the prompt (the previous preview's hypothesis minus a
        few rolled-back tokens), so only the new tail is decoded. The result's "ids" includes them."""
        def check_abort():
            if abort and abort():
                raise Aborted()

        check_abort()
        max_new_tokens = max_new_tokens or self.max_new_tokens
        audio = np.asarray(audio, dtype=np.float32)
        if audio.ndim != 1 or not len(audio) or not np.isfinite(audio).all():
            raise ValueError("Expected nonempty, finite mono PCM at 16000 Hz")
        if not 1 <= max_new_tokens <= self.max_new_tokens:
            raise ValueError(f"NPU generation supports 1..{self.max_new_tokens} new tokens")
        timings = {}
        t0 = time.perf_counter()
        n_frames = (len(audio) + HOP - 1) // HOP
        chunks = (n_frames + self.chunk_frames - 1) // self.chunk_frames
        final_frames = n_frames - (chunks - 1) * self.chunk_frames
        final_tokens = (final_frames * self.tokens_per_chunk + self.chunk_frames - 1) // self.chunk_frames
        n_audio_tokens = (chunks - 1) * self.tokens_per_chunk + final_tokens
        prefix = list(self.tpl["prefix_ids"])
        ctx_ids = self.tok.encode(context, add_special_tokens=False).ids if context else []
        suffix = list(self.tpl["suffix_ids"])
        forced_language = language in self.tpl["language_suffix_ids"] if language else False
        if forced_language:
            suffix += list(self.tpl["language_suffix_ids"][language])
        forced = list(prefix_ids or [])
        fixed = len(prefix) + n_audio_tokens + len(suffix)
        if fixed + len(ctx_ids) + len(forced) > self.max_prompt_tokens:
            forced = []  # drop the reused hypothesis first, then trim the hotword context
        if fixed + len(ctx_ids) > self.max_prompt_tokens:
            ctx_ids = ctx_ids[:max(0, self.max_prompt_tokens - fixed)]
        ids = prefix[:3] + ctx_ids + prefix[3:] + [self.tpl["audio_pad_id"]] * n_audio_tokens + suffix + forced
        if len(ids) > self.max_prompt_tokens:
            raise ValueError(f"NPU prompt needs {len(ids)} tokens; limit is {self.max_prompt_tokens}")
        timings["forced"] = len(forced)
        mel = log_mel(audio, None)
        timings["mel"] = time.perf_counter() - t0
        t = time.perf_counter()
        blocks = []
        for i in range(chunks):
            check_abort()
            features = np.zeros((1, 128, self.chunk_frames), dtype=np.float32)
            count = final_frames if i == chunks - 1 else self.chunk_frames
            features[0, :, :count] = mel[:, i * self.chunk_frames:i * self.chunk_frames + count]
            out = self.enc_req.infer({"input_features": features})
            keep = final_tokens if i == chunks - 1 else self.tokens_per_chunk
            blocks.append(next(iter(out.values()))[:, :keep].copy())
        audio_emb = np.concatenate(blocks, axis=1)
        timings["encoder"] = time.perf_counter() - t
        check_abort()
        t = time.perf_counter()
        embeds = self._embed(ids, audio_emb)
        length = len(ids)
        self.dec_req.reset_state()
        out = self.dec_req.infer({"inputs_embeds": embeds,
                                  "attention_mask": np.ones((1, length), dtype=np.int64),
                                  "position_ids": np.arange(length, dtype=np.int64)[None],
                                  "beam_idx": np.zeros(1, dtype=np.int32)})
        logits = out[self.dec.output("logits")]
        timings["prefill"] = time.perf_counter() - t
        t = time.perf_counter()
        generated = []
        first_token_at = time.perf_counter() - t0
        for i in range(max_new_tokens):
            check_abort()
            nxt = int(np.argmax(logits[0, -1]))
            if nxt in self.eos:
                break
            generated.append(nxt)
            if i + 1 == max_new_tokens:
                break
            embeds = self._embed([nxt], audio_emb)
            out = self.dec_req.infer({"inputs_embeds": embeds,
                                      "attention_mask": np.ones((1, length + i + 1), dtype=np.int64),
                                      "position_ids": np.asarray([[length + i]], dtype=np.int64),
                                      "beam_idx": np.zeros(1, dtype=np.int32)})
            logits = out[self.dec.output("logits")]
        timings["decode"] = time.perf_counter() - t
        timings["tokens"] = len(generated)
        timings["first_token"] = first_token_at
        timings["total"] = time.perf_counter() - t0
        generated = forced + generated
        if forced_language:
            lang, text_ids = language, generated
        elif self.asr_text_id in generated:
            k = generated.index(self.asr_text_id)
            lang = self.tok.decode([i for i in generated[:k] if i not in self.special],
                                   skip_special_tokens=False).replace("language", "").strip()
            text_ids = generated[k + 1:]
        else:
            lang, text_ids = "", generated
        text = self.tok.decode([i for i in text_ids if i not in self.special],
                               skip_special_tokens=False).strip()
        return {"text": text, "language": lang, "timings": timings, "ids": generated}

    def rollback(self, ids: list[int], k: int) -> list[int]:
        """The reusable part of a previous hypothesis: all but the last k text tokens (never cut the
        language header before <asr_text>)."""
        if not ids:
            return []
        keep = len(ids) - k
        if self.asr_text_id in ids:
            keep = max(keep, ids.index(self.asr_text_id) + 1)
        return ids[:max(0, keep)]
