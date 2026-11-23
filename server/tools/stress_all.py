"""Stress the multi-token step graph: random past/new lengths, each call repeated; identical inputs must
give identical outputs. Prints the mask layout, mismatch count and survives-or-crashes.

Usage: python stress_all.py <16 kHz wav, 10 s or longer> [N calls]"""
import os
import sys
import time
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the server modules
from asr_server import CFG, QWEN_DIR  # noqa: E402
from config import env_flag  # noqa: E402
from qwen_ov import Qwen3AsrOV, _causal_mask, log_mel  # noqa: E402

N = int(sys.argv[2]) if len(sys.argv) > 2 else 600
SHARE = env_flag("SHARE")
q = Qwen3AsrOV(QWEN_DIR, device=CFG.final_device, cache_dir=CFG.cache_dir, stateful=False)
print("share", SHARE, [i.get_partial_shape() for i in q.step_all.inputs], flush=True)
with wave.open(sys.argv[1]) as w:
    a = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
emb = next(iter(q.enc_req.infer({0: log_mel(a[:10 * 16000], None)}).values())).astype(np.float32)
pre = list(q.tpl["prefix_ids"])
full = np.concatenate([q._embed(pre[:3] + q._encode_text("计算机 OpenVINO NPU GPU 模型") + pre[3:]), emb], axis=1)
L = full.shape[1]
o = q.pre_req.infer({"input_embeds": full, "position_ids": np.arange(L, dtype=np.int64)[None]})
_, K0, V0 = (np.array(x) for x in q._unpack(o))
rng = np.random.default_rng(0)
reqs = [q.step_all.create_infer_request() for _ in range(2)]
GRAPH = os.environ.get("GRAPH", "all")
if GRAPH == "step":
    reqs = [q.step.create_infer_request() for _ in range(2)]
NAMES = {n for i in reqs[0].model_inputs for n in i.get_names()}
bad = 0
t = time.time()
for it in range(N):
    T = int(rng.integers(2, 10)) if it % 3 else int(rng.integers(30, 100))
    if os.environ.get("FIXT"):
        T = int(os.environ["FIXT"])
    p = int(rng.integers(20, L - T)) if os.environ.get("FIXT") else int(rng.integers(20, L - 1))
    T = min(T, L - p)
    feed = {"new_embed": np.ascontiguousarray(full[:, p:p + T]), "new_pos": np.arange(p, p + T, dtype=np.int64)[None],
            "past_keys": np.ascontiguousarray(K0[:, :, :, :p]), "past_values": np.ascontiguousarray(V0[:, :, :, :p]),
            "attn_mask": _causal_mask(p, T)}
    if "attn_mask" not in NAMES:
        del feed["attn_mask"]
    if it % int(os.environ.get("EVERY", "50")) == 0:
        print("it", it, "p", p, "T", T, flush=True)
    outs = []
    for r in reqs:
        o = r.infer(feed, share_inputs=SHARE, share_outputs=SHARE)
        lg, k, v = q._unpack(o)
        outs.append((np.array(lg), np.array(k[:, :, :, p:])))
    # the new KV must also match what the full prefill computed for those positions (fp16 noise allowed)
    dk = np.abs(outs[0][1] - K0[:, :, :, p:p + T]).max()
    same = np.array_equal(outs[0][0], outs[1][0])
    if not same or dk > 8 or not np.isfinite(outs[0][0]).all():
        bad += 1
        if bad <= 5:
            print(f"it {it} p {p} T {T} same {same} dk {dk:.2f}", flush=True)
print(f"done {N} calls x2 in {time.time() - t:.0f}s, bad {bad}", flush=True)
sys.stdout.flush()
os._exit(0)
