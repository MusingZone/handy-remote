"""Feed a wav through Handy's remote-worker protocol in real time-ish chunks; print what the overlay shows each second.

usage: python overlay_test.py <wav> [--rt] [--pause S] (--tcp HOST:PORT | --worker CMD [ARG...])
  --tcp 127.0.0.1:47821   talk to the server directly (on the server machine)
  --worker ssh -W 127.0.0.1:47821 user@server   spawn the same worker command Handy uses (must be the last option)
  --pause S               keep the connection idle for S seconds first (e.g. until the server unloads its models)
"""
import json, socket, struct, subprocess, sys, time, wave
import numpy as np

wav, realtime = sys.argv[1], "--rt" in sys.argv
if "--tcp" in sys.argv:
    host, port = sys.argv[sys.argv.index("--tcp") + 1].rsplit(":", 1)
    sock = socket.create_connection((host, int(port)))
    rf, wf, proc = sock.makefile("rb"), sock.makefile("wb"), None
elif "--worker" in sys.argv:
    proc = subprocess.Popen(sys.argv[sys.argv.index("--worker") + 1:], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    rf, wf = proc.stdout, proc.stdin
else:
    sys.exit(__doc__)


def read_n(n):
    b = b""
    while len(b) < n:
        c = rf.read(n - len(b))
        if not c:
            raise EOFError("worker closed")
        b += c
    return b


def call(msg, pcm=None):
    j = json.dumps(msg).encode()
    b = b"" if pcm is None else pcm.astype("<f4").tobytes()
    wf.write(struct.pack("<I", len(j)) + j + struct.pack("<I", len(b)) + b)
    wf.flush()
    r = json.loads(read_n(struct.unpack("<I", read_n(4))[0]))
    m = struct.unpack("<I", read_n(4))[0]
    if m:
        read_n(m)
    return r


call({"Hello": {}})
call({"Load": {"path": "remote-qwen3-asr-1.7b", "backend": "remote", "device": "remote"}})
if "--pause" in sys.argv:  # keep the connection idle first (e.g. until the server unloads its models)
    time.sleep(float(sys.argv[sys.argv.index("--pause") + 1]))
with wave.open(wav) as w:
    a = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
call({"StreamBegin": {"options": {}}})
committed, tent, t0, commits = "", "", time.time(), []
for i in range(0, len(a), 1600):
    if realtime:
        time.sleep(max(0, t0 + i / 16000 - time.time()))
    r = call({"Feed": {}}, a[i:i + 1600])["Fed"]
    if r.get("text"):
        if r["text"]["committed"] != committed:
            commits.append((round(i / 16000 + 0.1, 1), round(time.time() - t0, 1)))
        committed, tent = r["text"]["committed"], r["text"]["tentative"]
    if (i // 1600) % 10 == 9:
        print(f"{(i + 1600) / 16000:5.1f}s | {committed} [{tent}]")
t = time.time()
f = call({"Finalize": {}})["Finalized"]
print(f"final {time.time() - t:.2f}s after stop | {f['text']['full']}")
print("commit times (audio_s, wall_s):", commits)
wf.close()
if proc:
    proc.wait(10)
