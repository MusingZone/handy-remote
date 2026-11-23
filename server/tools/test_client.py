"""Drive asr_server.py like Handy does: hello, load, a realtime-paced stream, then a batch run.
Usage: python test_client.py <wav> [--host 127.0.0.1] [--port 47821] [--fast] [--ssh]"""
import argparse, json, struct, socket, subprocess, time, wave
import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument("wav")
ap.add_argument("--port", type=int, default=47821)
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--fast", action="store_true", help="feed without realtime pacing")
ap.add_argument("--ssh", nargs="*", help="spawn this command (e.g. ssh -W) instead of TCP")
ap.add_argument("--no-run", action="store_true")
a = ap.parse_args()

if a.ssh:
    p = subprocess.Popen(a.ssh, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    rd, wr = p.stdout, p.stdin
else:
    s = socket.create_connection((a.host, a.port))
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    rd, wr = s.makefile("rb"), s.makefile("wb")


def send(msg, pcm=None):
    j = json.dumps(msg).encode()
    b = np.asarray(pcm if pcm is not None else [], dtype="<f4").tobytes()
    wr.write(struct.pack("<I", len(j)) + j + struct.pack("<I", len(b)) + b)
    wr.flush()


def recv():
    n = struct.unpack("<I", rd.read(4))[0]
    r = json.loads(rd.read(n))
    m = struct.unpack("<I", rd.read(4))[0]
    rd.read(m)
    return r


def call(msg, pcm=None):
    send(msg, pcm)
    return recv()


t = time.time()
print("HELLO", json.dumps(call({"Hello": {"list_devices": True}}), ensure_ascii=False), f"{time.time()-t:.2f}s")
t = time.time()
info = call({"Load": {"path": "x.gguf", "backend": "Cpu", "device": "Auto"}})
print("LOAD", json.dumps(info, ensure_ascii=False)[:300], f"{time.time()-t:.2f}s")
with wave.open(a.wav) as w:
    assert w.getframerate() == 16000 and w.getsampwidth() == 2, "need a 16 kHz 16-bit wav"
    audio = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32).reshape(-1, w.getnchannels())
audio = audio.mean(axis=1) / 32768
sr = 16000
vocab = ["VS Code", "PowerShell", "Python"]
print("SB", call({"StreamBegin": {"run": {"vocabulary": vocab, "language": None}, "stream": {}}}))
chunk = 480 * 4  # Handy feeds ~30 ms frames; group 4 of them
t0 = time.time()
last = None
for i in range(0, len(audio), chunk):
    if not a.fast:
        target = t0 + (i + chunk) / sr
        d = target - time.time()
        if d > 0:
            time.sleep(d)
    r = call("Feed", audio[i:i + chunk])
    txt = r.get("Fed", {}).get("text")
    if txt and (txt["committed"], txt["tentative"]) != last:
        last = (txt["committed"], txt["tentative"])
        print(f"  t={time.time()-t0:5.1f}s audio={(i+chunk)/sr:5.1f}s | C: {txt['committed'][-40:]!r} | T: {txt['tentative'][-30:]!r}")
tf = time.time()
fin = call({"Finalize": {"want_language": True}})
print(f"FINALIZE after stop: {time.time()-tf:.2f}s ->", json.dumps(fin, ensure_ascii=False)[:600])
if not a.no_run:
    t = time.time()
    r = call({"Run": {"options": {"vocabulary": vocab}}}, audio)
    print(f"RUN {time.time()-t:.2f}s ->", r.get("Transcript", r).get("text") if isinstance(r, dict) else r)
