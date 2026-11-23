# Server setup

The recognition server runs on a Windows PC using OpenVINO on the CPU or a supported Intel GPU;
an Intel NPU is optional ([HARDWARE.md](HARDWARE.md)).
It listens on `127.0.0.1` only; clients reach it through SSH.

Commands below assume the server code lives in `C:\handy-remote\server` and the default server home
`%LOCALAPPDATA%\handy-remote-server`. Adjust the paths if you choose others.

## 1. Prerequisites

- Python 3.12 or 3.13 from [python.org](https://www.python.org/downloads/) with the `py` launcher.
- Current Intel graphics/NPU drivers if you use those accelerators.
- OpenSSH Server. In an **elevated** PowerShell:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Set-Service sshd -StartupType Automatic
Start-Service sshd
```

Installing the capability normally adds an inbound firewall rule for TCP 22. No other port needs to be
opened: the recognition server is never reachable from the network directly.

## 2. Get the server code

Clone the repository, or download **Source code (zip)** from the
[latest release](https://github.com/MusingZone/handy-remote/releases/latest), and copy its `server`
folder to a permanent place, for example `C:\handy-remote\server`. The background task runs the
scripts from that folder.

## 3. Run the setup

```powershell
powershell -ExecutionPolicy RemoteSigned -File C:\handy-remote\server\scripts\setup.ps1 -InstallTask
```

The script:

1. creates a Python virtual environment in `<home>\.venv` and installs `requirements.txt`
   (OpenVINO, sherpa-onnx, tokenizers, numpy);
2. downloads the models into `<home>\models` and verifies them (SHA-256 for large files; interrupted
   downloads resume);
3. runs `doctor.py --write`, which checks the machine and writes `<home>\config\config.toml` for the
   detected GPU/NPU plus a starter `lexicon.json`;
4. with `-InstallTask`, registers and starts the background task `HandyRemote-Server`.

Re-running is safe: finished steps are skipped or verified.

| Option              | Meaning                                                                                   |
| ------------------- | ----------------------------------------------------------------------------------------- |
| `-HomeDir <dir>`    | Server home (default `$env:HANDY_REMOTE_HOME`, else `%LOCALAPPDATA%\handy-remote-server`) |
| `-Source hf-mirror` | Download from hf-mirror.com when huggingface.co is unreachable                            |
| `-SkipNpu`          | Do not download the NPU model (machines without an Intel NPU)                             |
| `-InstallTask`      | Register the background task                                                              |
| `-AtStartup`        | With `-InstallTask`: start at boot without anyone logged on (needs elevation)             |

Downloads honour the standard `HTTPS_PROXY` / `NO_PROXY` variables.

### Models that are downloaded

| Key             | Model                                     | Source                                                                                                                                |
| --------------- | ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `final_model`   | Qwen3-ASR 1.7B, OpenVINO int8             | [dseditor/Qwen3-ASR-1.7B-INT8_OpenVINO](https://huggingface.co/dseditor/Qwen3-ASR-1.7B-INT8_OpenVINO)                                 |
| `draft_model`   | Qwen3-ASR 0.6B, OpenVINO int8 for the NPU | [iamwaqargulzar/handy-qwen3-asr-0.6b-openvino-npu-int8](https://huggingface.co/iamwaqargulzar/handy-qwen3-asr-0.6b-openvino-npu-int8) |
| `vad_model`     | Silero VAD                                | [sherpa-onnx releases](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models)                                                 |
| `preview_model` | Streaming Paraformer, bilingual zh/en     | [sherpa-onnx releases](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models)                                                 |
| `punct_model`   | CT-Transformer punctuation, zh/en, int8   | [sherpa-onnx releases](https://github.com/k2-fsa/sherpa-onnx/releases/tag/punctuation-models)                                         |

Licences: [THIRD-PARTY-NOTICES.md](../../THIRD-PARTY-NOTICES.md).

## 4. Check the machine

```powershell
$py = "$env:LOCALAPPDATA\handy-remote-server\.venv\Scripts\python.exe"
& $py C:\handy-remote\server\doctor.py
```

`doctor.py` reports Python, packages, OpenVINO devices, memory, disk, model files, the OpenSSH server
state and whether the server port is in use, and ends with `result: ready` or the number of problems.
Add `--write` to (re)create `config.toml` (an existing one is kept unless you add `--force`).

Wait for the server to report ready:

```powershell
Select-String "$env:LOCALAPPDATA\handy-remote-server\logs\server.log" -Pattern 'engine ready' | Select-Object -Last 1
```

## 5. Pair a client

Print the pairing code:

```powershell
& $py C:\handy-remote\server\doctor.py pair
```

The output is a single line `handy-remote-pair:…`. It contains only the host name, user name, SSH
port, server port, caret helper port, device id, label and the server's public SSH host key; the
script prints the decoded content too. Paste it into the client ([CLIENT-SETUP.md](CLIENT-SETUP.md)).
If the client cannot resolve this PC's name, replace the **Server** field there with an address it can
reach.

## 6. Authorize the client's key

In the client, **Settings → Remote → Create key**, then **Copy command**. It copies a line like
`python doctor.py authorize "ssh-ed25519 AAAA… handy-remote@laptop"`. Run it on the server with the
virtual environment's Python:

```powershell
cd C:\handy-remote\server
& $py doctor.py authorize "ssh-ed25519 AAAA... handy-remote@laptop"
```

The key is added with the options
`restrict,port-forwarding,permitopen="127.0.0.1:47821",permitopen="127.0.0.1:47823"`: it can open
only the server port and the caret helper port, with no shell, no file transfer and no other
forwarding.

**Administrator accounts:** Windows OpenSSH reads administrators' keys only from
`%ProgramData%\ssh\administrators_authorized_keys`. Run the command from an elevated PowerShell. If
`doctor.py` cannot write the file, it prints the line to append; afterwards keep the file readable
only by Administrators and SYSTEM:

```powershell
icacls "$env:ProgramData\ssh\administrators_authorized_keys" /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F"
```

## 7. Operate

| Task                                | Script in `C:\handy-remote\server\scripts`                            |
| ----------------------------------- | --------------------------------------------------------------------- |
| Restart after editing `config.toml` | `restart_server.ps1` (stops the server, waits for `engine ready`)     |
| Edit hotwords / rules               | none: edit `<home>\config\lexicon.json`, it is reloaded automatically |
| Run without the task                | `run_server.ps1` (the supervisor; output goes to `<home>\logs`)       |
| Register the task again             | `install_task.ps1` (add `-AtStartup` from an elevated PowerShell)     |
| Remove the background task          | `install_task.ps1 -Uninstall`                                         |

Run the scripts like this (Windows blocks `.ps1` files under the default execution policy):

```powershell
powershell -ExecutionPolicy RemoteSigned -File C:\handy-remote\server\scripts\restart_server.ps1
```

The task `HandyRemote-Server` starts `run_server.ps1`, a supervisor that restarts the server after a
crash with back-off and exits if another supervisor already runs. A 5-minute repetition restarts the
supervisor if it ever stopped. Without `-AtStartup` it runs at logon of the current user (no
administrator rights needed).

To watch the server in a console instead, pause the task and end its server first:

```powershell
Disable-ScheduledTask -TaskName HandyRemote-Server | Stop-ScheduledTask
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object CommandLine -like '*asr_server.py*' | ForEach-Object { Stop-Process -Id $_.ProcessId }
& $py C:\handy-remote\server\asr_server.py
# afterwards (Ctrl+C first):
Enable-ScheduledTask -TaskName HandyRemote-Server | Start-ScheduledTask
```

## Server home layout

| Path                             | Content                                                                       |
| -------------------------------- | ----------------------------------------------------------------------------- |
| `<home>\.venv`                   | Python environment                                                            |
| `<home>\models`                  | Model files                                                                   |
| `<home>\ov_cache`                | OpenVINO compiled model cache                                                 |
| `<home>\config\config.toml`      | Server configuration ([CONFIGURATION.md](CONFIGURATION.md))                   |
| `<home>\config\lexicon.json`     | Hotwords, replacement rules, streaming parameters                             |
| `<home>\config\lexicon-history\` | Last 50 lexicon versions replaced by the feedback tool                        |
| `<home>\config\warmup.wav`       | Optional local warm-up recording (`warmup_capture = true` explicitly opts in) |
| `<home>\logs\`                   | `server.log`, `supervisor.log`, `asr-YYYYMMDD.jsonl` audit                    |

## Run the server tests

The tests need no model files:

```powershell
cd C:\handy-remote\server
& $py -m unittest discover -s tests
```
