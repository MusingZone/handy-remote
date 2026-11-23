# Troubleshooting

Start with two checks:

1. **Client:** Settings → Remote → **Test connection**.
2. **Server:** `& "$env:LOCALAPPDATA\handy-remote-server\.venv\Scripts\python.exe" C:\handy-remote\server\doctor.py`
   (adjust the paths if you used another home or code folder).

## Log locations

| Where    | File                                                                                                        | Contains                                                           |
| -------- | ----------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| Client   | `%LOCALAPPDATA%\com.musingzone.handy-remote\logs\handy.log` (Settings → About → **Log Directory** opens it) | Recording, ssh command line, paste, update checks                  |
| Server   | `<home>\logs\server.log`                                                                                    | Startup, model loading, lifecycle, client connections              |
| Server   | `<home>\logs\supervisor.log`                                                                                | Server process starts, exits and restarts                          |
| Server   | `<home>\logs\server.stderr.log`, `server.stdout.log`                                                        | Raw Python output, tracebacks                                      |
| Server   | `<home>\logs\asr-YYYYMMDD.jsonl`                                                                            | Audit records per unit (contains transcript text; kept `log_days`) |
| Feedback | `%LOCALAPPDATA%\handy-remote\`                                                                              | Feedback tool logs and notices                                     |

`<home>` is `%LOCALAPPDATA%\handy-remote-server` by default.

Follow the server log live:

```powershell
Get-Content "$env:LOCALAPPDATA\handy-remote-server\logs\server.log" -Tail 30 -Wait
```

## Typical server log lines

| Line                                                                             | Meaning                                                                                                          |
| -------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `listening on 127.0.0.1:47821 (pid …; home …; config …)`                         | Server started                                                                                                   |
| `engine ready in 14.2s (load …; qwen device=GPU, npu previews=True, punct=True)` | Models loaded; shows the devices actually used                                                                   |
| `warm-up done in …`                                                              | Warm-up finished; the first dictation will be fast                                                               |
| `no warm-up clip yet (…); zero-audio warm-up only`                               | Normal until you have dictated once (`warmup_capture`)                                                           |
| `NPU preview model unavailable: …`                                               | No NPU or driver problem; previews run on the GPU instead                                                        |
| `punctuation model unavailable: …`                                               | Punctuation falls back to pause length; run `setup.ps1` again to download it                                     |
| `engine unloaded (idle 2.0 h)` / `engine unloaded (night)`                       | Lifecycle unload; the next dictation reloads (~12 s)                                                             |
| `offline window over: reloading`                                                 | End of `offline_window`                                                                                          |
| `lexicon loaded: N hotwords, M replacements`                                     | Lexicon (re)loaded                                                                                               |
| `lexicon reload failed …`                                                        | Broken `lexicon.json` edit; the previous version is still active                                                 |
| `client connected …` / `client closed …` / `client gone …`                       | A dictation session through the SSH relay                                                                        |
| `qwen failed (…); rebuilding infer requests and retrying`                        | GPU inference error; the server recovers on its own                                                              |
| `engine failed`                                                                  | Models could not load; the server exits and the supervisor restarts it. See the traceback in `server.stderr.log` |
| `bound to …: the protocol has no authentication; prefer 127.0.0.1 + ssh -W`      | `bind` is not loopback. Change it back unless you know why                                                       |

`supervisor.log` shows `server started pid=…` and `server exited code=… after …s`. Repeated quick
exits mean the server cannot start; look at `server.stderr.log`.

## Test connection results

| Result                                                                               | Meaning / fix                                                                                                                                          |
| ------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `Connected in … ms; the server answered in … ms.` + `Models are loaded.`             | All good                                                                                                                                               |
| … + `Models are unloaded; they load on the first dictation (a few seconds).`         | All good; the models were unloaded by the idle timer                                                                                                   |
| … + `The server is an older version and reports no details.`                         | Works; update the server for status details                                                                                                            |
| `Connection failed: no ssh executable was found; install the Windows OpenSSH client` | Install it (see [CLIENT-SETUP.md](CLIENT-SETUP.md)) or set **ssh program**                                                                             |
| `Connection failed: no answer within 20 s`                                           | Server PC unreachable or asleep, or the server is not running. Check `ssh user@server` by hand                                                         |
| `Connection failed: the connection closed: …`                                        | SSH worked but nothing listens on the server port: start the task (`scripts\restart_server.ps1`)                                                       |
| `Permission denied (publickey…)`                                                     | The client key is not authorized: run `doctor.py authorize` ([SERVER-SETUP.md](SERVER-SETUP.md#6-authorize-the-clients-key))                           |
| `Host key verification failed`                                                       | The server's host key differs from the pinned **Server key**. If the server was reinstalled, import a new pairing code; otherwise stop and investigate |
| `channel 0: open failed: administratively prohibited`                                | The key's `permitopen` does not allow this port. Re-run `doctor.py authorize` after changing ports                                                     |

## Common problems

| Symptom                                                        | Check                                                                                                                            |
| -------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| First word missing                                             | Wait for the ready cue (mic glyph with expanding ring) before speaking                                                           |
| "Waking the server's models…" in the overlay, text after ~12 s | Expected after an idle unload; nothing you said is lost. Raise `idle_unload_hours` or set it to `0`                              |
| Text arrives slowly (several seconds)                          | `doctor.py` shows the device; `final_device = "CPU"` is 2–4× slower. Check GPU drivers                                           |
| No live preview                                                | `preview_interval_s` is `0` or `preview_max_s` is exceeded; see [CONFIGURATION.md](CONFIGURATION.md#streaming-parameters-stream) |
| A name is always wrong                                         | Add it to `hotwords` or report it with the [feedback tool](FEEDBACK.md)                                                          |
| Overlay not at the caret                                       | Some apps expose no caret; in Remote Desktop see [RDP.md](RDP.md)                                                                |
| Server stops after the PC sleeps                               | The task's watchdog restarts it within 5 minutes; `restart_server.ps1` restarts it now                                           |
| Remote recognition is on but Handy uses local models           | Client log line `remote recognition is on, but no ssh was found`                                                                 |

## Run the server in a console

To see every message directly, stop the task and run the server by hand (see
[SERVER-SETUP.md](SERVER-SETUP.md)):

```powershell
Disable-ScheduledTask -TaskName HandyRemote-Server
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object CommandLine -like '*asr_server.py*' | ForEach-Object { Stop-Process -Id $_.ProcessId }
& "$env:LOCALAPPDATA\handy-remote-server\.venv\Scripts\python.exe" C:\handy-remote\server\asr_server.py
# Ctrl+C when done, then:
Enable-ScheduledTask -TaskName HandyRemote-Server
```

## Reporting a bug

Open an issue on [GitHub](https://github.com/MusingZone/handy-remote/issues) with the `doctor.py`
output and the relevant log lines. Remove host names, user names and transcript text first. Security
problems: see [SECURITY.md](../../SECURITY.md).
