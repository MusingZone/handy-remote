# Hardware

## Server

| Item    | Requirement                                                                                                                       |
| ------- | --------------------------------------------------------------------------------------------------------------------------------- |
| OS      | Windows 10 or 11, x64. The setup and task scripts are PowerShell; other systems are untested.                                     |
| Python  | 3.12 or 3.13 with the `py` launcher (python.org installer)                                                                        |
| GPU     | Recommended: an Intel GPU supported by the OpenVINO GPU plugin (integrated Arc iGPU or a discrete Arc GPU), with a current driver |
| NPU     | Optional: Intel NPU (Intel Core Ultra "AI Boost") for live previews and speculative drafts                                        |
| Memory  | 16 GB or more recommended (`doctor.py` warns below that)                                                                          |
| Disk    | 8 GB free in the server home: models about 4 GB, compiled model cache about 2 GB                                                  |
| Network | OpenSSH Server reachable from the client (usually TCP 22 on the LAN or over a VPN)                                                |

### Device choices

| Machine                 | `final_device` | `draft_enabled` | Live previews                                   |
| ----------------------- | -------------- | --------------- | ----------------------------------------------- |
| Intel iGPU + NPU (best) | `GPU`          | `true`          | Qwen3-ASR 0.6B on the NPU, every 0.5 s          |
| Intel GPU, no NPU       | `GPU`          | `false`         | Qwen3-ASR 1.7B on the GPU between finals        |
| CPU only                | `CPU`          | `false`         | Same model on the CPU; finals noticeably slower |

`python doctor.py --write` detects the devices and writes these two keys for you
([SERVER-SETUP.md](SERVER-SETUP.md)). Without a GPU, `doctor.py` warns that final text runs on the CPU
(slower).

## Client

Any PC that runs Handy. Recognition happens on the server, so the client needs no GPU. Remote
recognition uses the Windows OpenSSH client (`ssh.exe`, built into Windows 10/11). The caret-following
overlay and the Remote Desktop caret are Windows features.

## Measuring your configuration

The default server uses the CPU without an NPU draft model. `doctor.py --write` generates a local
device profile; it does not change the generic source defaults. Enable NPU previews explicitly in
your [configuration](CONFIGURATION.md).

Latency, memory and model load time depend on devices, drivers, OpenVINO version, background load
and utterance length. Check your own numbers in `server.log` (`engine ready in …`) and the audit
records (`latency` per unit), see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).
