"""Server configuration: defaults < <home>/config/config.toml < environment variables.

Every key can be set from the environment as HANDY_REMOTE_<KEY> (upper case, e.g.
HANDY_REMOTE_FINAL_DEVICE=CPU). The pre-release VOICE_ASR_* names are still read as
deprecated aliases. See docs/remote/CONFIGURATION.md for the meaning of each key.
"""
from __future__ import annotations

import os
import socket
import sys
import tomllib
from dataclasses import dataclass, field, fields

ENV_PREFIX = "HANDY_REMOTE_"
# Deprecated environment names from the pre-release server.
LEGACY_ENV = {
    "port": "VOICE_ASR_PORT",
    "lexicon": "VOICE_ASR_LEXICON",
    "log_dir": "VOICE_ASR_LOG_DIR",
    "final_device": "VOICE_ASR_DEVICE",
    "idle_unload_hours": "VOICE_ASR_IDLE_UNLOAD_H",
    "offline_window": "VOICE_ASR_NIGHT",
    "load_in_offline_window": "VOICE_ASR_LOAD_AT_NIGHT",
    "draft_enabled": "VOICE_ASR_NPU",
    "stateful_kv": "VOICE_ASR_STATEFUL",
}


def env_flag(name: str, default: bool = True) -> bool:
    """Debug/experiment switch read from HANDY_REMOTE_<name> (or the legacy VOICE_ASR_<name>)."""
    raw = os.environ.get(ENV_PREFIX + name, os.environ.get("VOICE_ASR_" + name))
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def default_home() -> str:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "handy-remote-server")


@dataclass
class Config:
    home: str = ""
    # network: loopback only by default; clients come in through `ssh -W`
    bind: str = "127.0.0.1"
    port: int = 47821
    # how the server presents itself in Handy: a stable id and a label (empty = this machine's name)
    device_id: str = "remote"
    device_label: str = ""
    # final (committed) text: Qwen3-ASR 1.7B on an OpenVINO device (GPU, CPU or AUTO)
    final_model: str = "qwen3-asr-1.7b-ov-int8"
    final_device: str = "CPU"
    stateful_kv: bool = False
    # NPU model for live previews and speculative drafts (draft_enabled = false: no NPU use)
    draft_model: str = "qwen3-asr-0.6b-ov-npu-int8"
    draft_enabled: bool = False
    preview_model: str = "sherpa-onnx-streaming-paraformer-bilingual-zh-en"
    vad_model: str = "silero_vad.onnx"
    punct_model: str = "sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8/model.int8.onnx"
    lexicon: str = ""
    # keep-alive: unload after this many idle hours (0 = never); optional daily offline window "H-H"
    idle_unload_hours: float = 2.0
    offline_window: str = ""
    offline_idle_minutes: float = 10.0
    load_in_offline_window: bool = False
    warmup: bool = True
    # keep the first 3-8 s dictation as the local warm-up clip when there is none (never leaves this machine)
    warmup_capture: bool = False
    # literal text instead of status tokens such as {{handy-remote:waking}}, for older clients
    legacy_status_text: str = ""
    log_dir: str = ""
    log_days: int = 14
    models_dir: str = ""
    cache_dir: str = ""
    config_dir: str = ""
    sources: list = field(default_factory=list)  # files the values came from (for `doctor`)

    @property
    def offline_hours(self) -> tuple[int, int] | None:
        w = (self.offline_window or "").strip()
        if not w:
            return None
        a, b = (int(x) for x in w.split("-"))
        if not (0 <= a <= 23 and 0 <= b <= 24):
            raise ValueError(f"offline_window {w!r}: hours must be 0-24")
        return a, b

    @property
    def label(self) -> str:
        return self.device_label or socket.gethostname()

    def model_path(self, name: str) -> str:
        return name if os.path.isabs(name) else os.path.join(self.models_dir, name)

    @property
    def warmup_wav(self) -> str:
        return os.path.join(self.config_dir, "warmup.wav")


def _coerce(kind, raw):
    if kind is bool:
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() not in ("0", "false", "no", "off", "")
    if kind is int:
        return int(raw)
    if kind is float:
        return float(raw)
    return str(raw)


def config_path(home: str, env=None) -> str:
    env = os.environ if env is None else env
    return env.get(ENV_PREFIX + "CONFIG") or os.path.join(home, "config", "config.toml")


def load(home: str | None = None, env: dict | None = None) -> Config:
    env = os.environ if env is None else env
    home = home or env.get(ENV_PREFIX + "HOME") or default_home()
    cfg = Config(home=os.path.abspath(home))
    cfg.config_dir = os.path.join(cfg.home, "config")
    path = config_path(cfg.home, env)
    data: dict = {}
    if os.path.exists(path):
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        # [server], [models], [lifecycle] ... sections are only for readability and are flattened
        for k, v in raw.items():
            if isinstance(v, dict):
                data.update(v)
            else:
                data[k] = v
        cfg.sources.append(path)
    types = {f.name: type(getattr(cfg, f.name)) for f in fields(cfg) if f.name not in ("home", "sources")}
    unknown = sorted(set(data) - set(types))
    if unknown:
        raise ValueError(f"{path}: unknown keys {unknown}")
    for k, kind in types.items():
        val = data.get(k)
        legacy = LEGACY_ENV.get(k)
        if ENV_PREFIX + k.upper() in env:
            val = env[ENV_PREFIX + k.upper()]
            cfg.sources.append(ENV_PREFIX + k.upper())
        elif legacy and legacy in env:
            val = env[legacy]
            cfg.sources.append(legacy + " (deprecated)")
        if val is not None:
            setattr(cfg, k, _coerce(kind, val))
    cfg.models_dir = cfg.models_dir or os.path.join(cfg.home, "models")
    cfg.cache_dir = cfg.cache_dir or os.path.join(cfg.home, "ov_cache")
    cfg.log_dir = cfg.log_dir or os.path.join(cfg.home, "logs")
    cfg.lexicon = cfg.lexicon or os.path.join(cfg.config_dir, "lexicon.json")
    cfg.offline_hours  # noqa: B018  (validates the window early)
    return cfg
