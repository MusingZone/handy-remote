"""Checks this machine for the Handy Remote server and helps connect a client.

  python doctor.py                 report: Python, packages, OpenVINO devices, memory, disk, models, sshd, port
  python doctor.py --write         also write <home>/config/config.toml for the detected hardware (keeps an existing one)
  python doctor.py pair            print the pairing code to paste into Handy (Settings > Remote recognition)
  python doctor.py authorize KEY   allow a client's SSH public key to reach only the server ports

Options: --home DIR (default: HANDY_REMOTE_HOME or %LOCALAPPDATA%/handy-remote-server), --force (overwrite config.toml).
"""
from __future__ import annotations

import base64
import ctypes
import getpass
import importlib.metadata as md
import json
import os
import shutil
import socket
import subprocess
import sys

import config

CARET_PORT = 47823
SSHD_DIR = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "ssh")


def ok(flag: bool) -> str:
    return "ok  " if flag else "WARN"


def total_ram_gb() -> float | None:
    if sys.platform != "win32":
        try:
            return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
        except (ValueError, OSError, AttributeError):
            return None

    class MemStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    m = MemStatus()
    m.dwLength = ctypes.sizeof(MemStatus)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m.ullTotalPhys / 2**30


def openvino_devices() -> dict[str, str]:
    try:
        import openvino as ov
    except ImportError:
        return {}
    core = ov.Core()
    out = {}
    for d in core.available_devices:
        try:
            out[d] = core.get_property(d, "FULL_DEVICE_NAME")
        except Exception:  # noqa: BLE001 - a broken driver should not stop the report
            out[d] = "?"
    return out


def profile(devices: dict[str, str]) -> dict:
    gpu = next((d for d in devices if d.startswith("GPU")), None)
    return {"final_device": gpu or "CPU", "draft_enabled": any(d.startswith("NPU") for d in devices)}


def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def sshd_state() -> str:
    if sys.platform != "win32":
        return "n/a"
    r = subprocess.run(["sc.exe", "query", "sshd"], capture_output=True, text=True)
    if "RUNNING" in r.stdout:
        return "running"
    return "stopped" if "STATE" in r.stdout else "not installed"


def report(cfg: config.Config, write: bool, force: bool) -> int:
    problems = 0
    print(f"home      {cfg.home}")
    print(f"config    {', '.join(cfg.sources) or '(defaults only)'}")
    good = sys.version_info >= (3, 12)
    problems += not good
    print(f"{ok(good)}  Python {sys.version.split()[0]}")
    for pkg in ("openvino", "sherpa-onnx", "tokenizers", "numpy"):
        try:
            print(f"ok    {pkg} {md.version(pkg)}")
        except md.PackageNotFoundError:
            problems += 1
            print(f"WARN  {pkg} missing: pip install -r requirements.txt")
    devices = openvino_devices()
    for d, name in devices.items():
        print(f"ok    OpenVINO device {d}: {name}")
    prof = profile(devices)
    if prof["final_device"] == "CPU":
        print("WARN  no OpenVINO GPU: final text runs on the CPU (slower, about 2-4x real time)")
    if not prof["draft_enabled"]:
        print("info  no NPU: live previews also run on the final model (no speculative drafts)")
    ram = total_ram_gb()
    if ram is not None:
        good = ram >= 16
        problems += not good
        print(f"{ok(good)}  memory {ram:.0f} GB (about 10 GB used while the models are loaded)")
    os.makedirs(cfg.home, exist_ok=True)
    free = shutil.disk_usage(cfg.home).free / 2**30
    print(f"{ok(free >= 8)}  disk {free:.0f} GB free in {cfg.home} (models about 4 GB, compiled cache about 2 GB)")
    for key in ("final_model", "preview_model", "vad_model", "punct_model") + (("draft_model",) if prof["draft_enabled"] else ()):
        path = cfg.model_path(getattr(cfg, key))
        there = os.path.exists(path)
        problems += not there
        print(f"{ok(there)}  {key}: {path}{'' if there else '  (run scripts/download_models.ps1)'}")
    st = sshd_state()
    print(f"{ok(st == 'running')}  OpenSSH server: {st}")
    print(f"info  port {cfg.port}: {'a server is listening' if port_in_use(cfg.port) else 'free (server not running)'}")
    target = config.config_path(cfg.home)
    if write:
        if os.path.exists(target) and not force:
            print(f"kept  {target} (use --force to overwrite)")
        else:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write("# written by doctor.py for this machine; see config.example.toml for all keys\n")
                f.write(f'final_device = "{prof["final_device"]}"\n')
                f.write(f'draft_enabled = {str(prof["draft_enabled"]).lower()}\n')
            print(f"wrote {target}")
        lex = cfg.lexicon
        if not os.path.exists(lex):
            src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lexicon.example.json")
            shutil.copyfile(src, lex)
            print(f"wrote {lex}")
    print("result:", "ready" if problems == 0 else f"{problems} problem(s) above")
    return 1 if problems else 0


def host_key() -> str | None:
    path = os.path.join(SSHD_DIR, "ssh_host_ed25519_key.pub")
    try:
        with open(path, encoding="ascii") as f:
            kind, key = f.read().split()[:2]
        return f"{kind} {key}"
    except (OSError, ValueError):
        return None


def pair(cfg: config.Config) -> int:
    info = {"v": 1, "host": socket.gethostname(), "user": getpass.getuser(), "ssh_port": 22,
            "server_port": cfg.port, "caret_port": CARET_PORT, "device_id": cfg.device_id,
            "label": cfg.label, "host_key": host_key()}
    code = base64.urlsafe_b64encode(json.dumps(info, separators=(",", ":")).encode()).decode().rstrip("=")
    print("Paste this into Handy Remote > Settings > Remote > Pairing code, then Apply:\n")
    print(f"handy-remote-pair:{code}\n")
    print("It contains only:", json.dumps(info, indent=1))
    print("\nThe host name may need to be replaced by an address the client can reach.")
    if not info["host_key"]:
        print("Note: no OpenSSH host key found; Handy will ask you to confirm the host key on first connect.")
    return 0


def is_admin_account() -> bool:
    if sys.platform != "win32":
        return False
    r = subprocess.run(["whoami", "/groups"], capture_output=True, text=True)
    return "S-1-5-32-544" in r.stdout


def authorize(cfg: config.Config, key: str) -> int:
    parts = key.split()
    if len(parts) < 2 or not parts[0].startswith(("ssh-", "ecdsa-", "sk-")):
        print("expected an OpenSSH public key such as 'ssh-ed25519 AAAA... comment'")
        return 2
    opts = f'restrict,port-forwarding,permitopen="127.0.0.1:{cfg.port}",permitopen="127.0.0.1:{CARET_PORT}"'
    line = f"{opts} {' '.join(parts)}"
    # Windows OpenSSH reads keys of administrators only from administrators_authorized_keys.
    path = (os.path.join(SSHD_DIR, "administrators_authorized_keys") if is_admin_account()
            else os.path.join(os.path.expanduser("~"), ".ssh", "authorized_keys"))
    try:
        existing = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        if parts[1] in existing:
            print(f"already authorized in {path}")
            return 0
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(("" if not existing or existing.endswith("\n") else "\n") + line + "\n")
        print(f"added to {path}; the key can open only 127.0.0.1:{cfg.port} and :{CARET_PORT}, no shell")
        return 0
    except PermissionError:
        print(f"no permission to write {path}. In an elevated PowerShell, append this line to it:\n\n{line}\n")
        print("and keep the file readable only by Administrators and SYSTEM (see docs/remote/SERVER-SETUP.md).")
        return 1


def main(argv: list[str]) -> int:
    home = None
    if "--home" in argv:
        i = argv.index("--home")
        home = argv[i + 1]
        del argv[i:i + 2]
    cfg = config.load(home)
    if argv[:1] == ["pair"]:
        return pair(cfg)
    if argv[:1] == ["authorize"]:
        return authorize(cfg, " ".join(argv[1:]))
    if argv and argv[0] not in ("--write", "--force"):
        print(__doc__)
        return 2
    return report(cfg, "--write" in argv, "--force" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
