"""Final-decode variants on the production Qwen3AsrOV: prefix KV cache and NPU-drafted speculation.

Usage (on the server machine): python final_ab.py <dir of 16 kHz wavs> <out.jsonl>
Units: every recording cut into 3 s and 5 s pieces, decoded with the production hotword context plus a
changing 'prior text' tail (as unit_context_chars does). Draft = NPU 0.6B on the piece minus its last 0.7 s.
Reference = plain (full prefill graph, one token per step).
"""
from __future__ import annotations

import json
import os
import sys
import time
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the server modules
from asr_server import CFG, LEXICON, NPU_DIR, NPU_OPTS, QWEN_DIR, Lexicon  # noqa: E402
from qwen_npu import Qwen3AsrNPU  # noqa: E402
from qwen_ov import Qwen3AsrOV  # noqa: E402

rec, out_path = sys.argv[1:3]
SR = 16000
q = Qwen3AsrOV(QWEN_DIR, device=CFG.final_device, cache_dir=CFG.cache_dir, stateful=False)
if not CFG.draft_enabled:
    raise SystemExit("This speculation benchmark needs draft_enabled=true and an NPU model.")
npu = Qwen3AsrNPU(NPU_DIR, **NPU_OPTS)
lex = Lexicon(LEXICON)
lex.refresh()
hot = lex.context([])


def cer(a, b):
    a, b = [c for c in a if c.isalnum()], [c for c in b if c.isalnum()]
    d = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        prev, d[0] = d[0], i
        for j, y in enumerate(b, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (x != y))
    return d[-1] / max(1, len(b))


units = []
for n in sorted(x for x in os.listdir(rec) if x.endswith(".wav")):
    with wave.open(os.path.join(rec, n)) as w:
        a = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
    for piece in (3, 5):
        for i in range(0, len(a), piece * SR):
            u = a[i:i + piece * SR]
            if len(u) >= 1.5 * SR:
                units.append((f"{n}@{i // SR}/{piece}", u))
print("units", len(units), flush=True)
MODES = [("plain", False, False), ("cache", True, False), ("spec", False, True), ("cache+spec", True, True)]
w = units[0][1]
for _, pc, sp in MODES:
    q.prefix_cache = pc
    q.transcribe(w, context=hot, draft_ids=npu.transcribe(w, context=hot)["ids"] if sp else None)

agg = {m[0]: [] for m in MODES}
prior = ""
npu_ms = []
with open(out_path, "w", encoding="utf-8") as f:
    for name, a in units:
        ctx = f"{hot}\n{prior}" if prior else hot
        tn = time.perf_counter()
        d = npu.transcribe(a[:max(SR // 2, len(a) - int(0.7 * SR))], context=hot)["ids"]
        npu_ms.append((time.perf_counter() - tn) * 1000)
        row = {"unit": name, "dur": round(len(a) / SR, 2)}
        ref = None
        for m, pc, sp in MODES:
            q.prefix_cache = pc
            print("RUN", name, m, flush=True)
            t = time.perf_counter()
            r = q.transcribe(a, context=ctx, draft_ids=d if sp else None)
            wall = time.perf_counter() - t
            tm = r["timings"]
            if ref is None:
                ref = r
                row["text"] = r["text"]
            x = {"wall": round(wall, 4), "prefill": round(tm["prefill"], 4), "decode": round(tm["decode"], 4),
                 "reused": tm.get("prefix_reused", 0), "same": r["text"] == ref["text"],
                 "cer": round(cer(r["text"], ref["text"]), 4)}
            if not x["same"]:
                x["text"] = r["text"]
            row[m] = x
            agg[m].append(x)
        prior = (prior + ref["text"])[-80:]
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()

print(f"\nNPU draft mean {np.mean(npu_ms):.0f} ms, median {np.median(npu_ms):.0f} ms")
print("\nmode        wall   prefill  decode  reused_tok  text_same  cer_vs_plain")
for m, rs in agg.items():
    print(f"{m:10s} {np.mean([r['wall'] for r in rs]):.3f}  {np.mean([r['prefill'] for r in rs]):.3f}   "
          f"{np.mean([r['decode'] for r in rs]):.3f}   {np.mean([r['reused'] for r in rs]):8.0f}   "
          f"{sum(r['same'] for r in rs):3d}/{len(rs)}   {np.mean([r['cer'] for r in rs]):.4f}")
for line in open(out_path, encoding="utf-8"):
    r = json.loads(line)
    for k, v in r.items():
        if isinstance(v, dict) and "text" in v:
            print("DIFF", r["unit"], k, "\n   ref:", r["text"], "\n   got:", v["text"])
