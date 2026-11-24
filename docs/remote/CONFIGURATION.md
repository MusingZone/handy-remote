# Configuration

## Server: `config.toml`

Location: `<home>\config\config.toml`, where `<home>` is `%LOCALAPPDATA%\handy-remote-server` unless
`HANDY_REMOTE_HOME` (or `--home DIR` on the command line) says otherwise. `HANDY_REMOTE_CONFIG` can
point at another file. A commented template is [`server/config.example.toml`](../../server/config.example.toml).

Precedence: built-in defaults < `config.toml` < environment variables. Every key is optional and can
be set from the environment as `HANDY_REMOTE_<KEY>` in upper case, e.g.
`HANDY_REMOTE_FINAL_DEVICE=CPU`. Section names (`[server]`, `[models]`, …) are only for readability;
unknown keys are an error. Restart the server after a change
(`scripts\restart_server.ps1`).

### Network and identity

| Key            | Default       | Meaning                                                                      |
| -------------- | ------------- | ---------------------------------------------------------------------------- |
| `bind`         | `"127.0.0.1"` | Listen address. Keep loopback: the protocol has no authentication of its own |
| `port`         | `47821`       | Listen port (the client's **Server port**)                                   |
| `device_id`    | `"remote"`    | Stable id of this server in Handy                                            |
| `device_label` | `""`          | Name shown in Handy; empty = this computer's name                            |

### Models

| Key             | Default                                                                                | Meaning                                                                                 |
| --------------- | -------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `final_model`   | `"qwen3-asr-1.7b-ov-int8"`                                                             | Folder of the final-text model (relative to `models_dir`)                               |
| `final_device`  | `"CPU"`                                                                                | OpenVINO device for final text: `GPU`, `CPU` or `AUTO`                                  |
| `draft_enabled` | `false`                                                                                | Use the NPU model for previews and drafts; `false` = no NPU                             |
| `draft_model`   | `"qwen3-asr-0.6b-ov-npu-int8"`                                                         | Folder of the NPU model                                                                 |
| `preview_model` | `"sherpa-onnx-streaming-paraformer-bilingual-zh-en"`                                   | Streaming Paraformer folder (CPU drafts)                                                |
| `vad_model`     | `"silero_vad.onnx"`                                                                    | Silero VAD model file                                                                   |
| `punct_model`   | `"sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8/model.int8.onnx"` | CT-Transformer punctuation model                                                        |
| `stateful_kv`   | `false`                                                                                | Advanced: OpenVINO stateful KV cache for the final model. Disables speculative decoding |
| `lexicon`       | `""`                                                                                   | Lexicon file; empty = `<config_dir>\lexicon.json`                                       |

Model names may also be absolute paths. The built-in defaults do not require a GPU or NPU.
`doctor.py --write` generates a machine-local acceleration profile from detected devices; keep
that generated file outside the source checkout.

### Lifecycle

| Key                      | Default | Meaning                                                                                                  |
| ------------------------ | ------- | -------------------------------------------------------------------------------------------------------- |
| `idle_unload_hours`      | `2.0`   | Unload the models after this many idle hours; `0` = keep them loaded                                     |
| `offline_window`         | `""`    | Optional daily window `"start-end"` in local hours, e.g. `"1-9"` or `"22-6"`. Empty = none               |
| `offline_idle_minutes`   | `10.0`  | Inside the window, unload once idle this long; models reload when the window ends                        |
| `load_in_offline_window` | `false` | Load the models at server start even inside the window                                                   |
| `warmup`                 | `true`  | Run the real request path once after every load                                                          |
| `warmup_capture`         | `false` | Opt in to keeping your first 3–8 s dictation as `<config_dir>\warmup.wav` for later warm-ups             |
| `legacy_status_text`     | `""`    | Literal text sent instead of the `{{handy-remote:waking}}` token, for clients that predate status tokens |

Whatever the lifecycle state, a dictation always works: unloaded models are reloaded on demand while
the audio is buffered.

### Paths and logs

| Key          | Default           | Meaning                                                     |
| ------------ | ----------------- | ----------------------------------------------------------- |
| `models_dir` | `<home>\models`   | Model folder                                                |
| `cache_dir`  | `<home>\ov_cache` | OpenVINO compiled model cache                               |
| `config_dir` | `<home>\config`   | Default location of `lexicon.json` and `warmup.wav`         |
| `log_dir`    | `<home>\logs`     | `server.log` and the daily audit files `asr-YYYYMMDD.jsonl` |
| `log_days`   | `14`              | Delete audit files older than this many days                |

### Server environment variables

| Variable                       | Effect                                                                                  |
| ------------------------------ | --------------------------------------------------------------------------------------- |
| `HANDY_REMOTE_<KEY>`           | Overrides any key above                                                                 |
| `HANDY_REMOTE_HOME`            | Server home (also used by all scripts in `server\scripts`)                              |
| `HANDY_REMOTE_CONFIG`          | Path of `config.toml`                                                                   |
| `HANDY_REMOTE_DEBUG_PREVIEW=1` | Write every preview to the audit log (same as creating the file `<home>\debug_preview`) |
| `HANDY_REMOTE_SPEC=0`          | Experiment switch: build the final model without speculative verification               |
| `HANDY_REMOTE_PREFIX_CACHE=0`  | Experiment switch: no prefix KV cache                                                   |
| `HANDY_REMOTE_SHARE=0`         | Experiment switch: no shared ("ping-pong") KV buffers                                   |

Deprecated aliases from the pre-release server are still read: `VOICE_ASR_PORT`, `VOICE_ASR_LEXICON`,
`VOICE_ASR_LOG_DIR`, `VOICE_ASR_DEVICE` (`final_device`), `VOICE_ASR_IDLE_UNLOAD_H`,
`VOICE_ASR_NIGHT` (`offline_window`), `VOICE_ASR_LOAD_AT_NIGHT`, `VOICE_ASR_NPU` (`draft_enabled`),
`VOICE_ASR_STATEFUL` (`stateful_kv`).

`python doctor.py` prints which files and variables the effective configuration came from.

## Server: `lexicon.json`

Location: `<home>\config\lexicon.json` (template: [`server/lexicon.example.json`](../../server/lexicon.example.json)).
The server reloads it automatically when it changes; a broken edit is logged and the previous version
stays active. The feedback tool edits the same file ([FEEDBACK.md](FEEDBACK.md)).

```json
{
  "hotwords": ["Handy", "OpenVINO", "PowerShell", "GitHub"],
  "replacements": [{ "from": "(?i)open vino", "to": "OpenVINO" }],
  "stream": {}
}
```

- `hotwords`: terms that bias Qwen3-ASR through its context prompt. This list is also Handy's
  **custom words** list (Settings → Advanced): every client that uses the server syncs it both ways, at
  startup, every 5 minutes, after each edit and when the settings page opens. A word added or removed
  on one PC (or here, or by the feedback tool) reaches the others; edits made at the same time are
  merged, not lost. Each write keeps the previous file in `<home>\config\lexicon-history`. With cloud
  recognition or without a server the list stays on that PC.
- `replacements`: regex fixes applied in order to recognized text (Python `re.sub`; case-sensitive
  unless the pattern starts with `(?i)`).
- `stream`: overrides of the streaming parameters below.

### Streaming parameters (`stream`)

The example lexicon has no streaming overrides. Tune these values for your own hardware and
dictations. NPU previews require both `draft_enabled = true` and `preview_device = 1`.

| Key                  | Built-in | Meaning                                                                                               |
| -------------------- | -------- | ----------------------------------------------------------------------------------------------------- |
| `vad_min_silence_s`  | `0.5`    | Silence that ends a VAD speech segment                                                                |
| `unit_min_s`         | `4.0`    | A unit closes at the first segment end once it spans this much audio                                  |
| `unit_pause_s`       | `1.1`    | …or after this much silence, whatever its length                                                      |
| `comma_gap_s`        | `1.0`    | A shorter pause between units joins them with a comma                                                 |
| `stop_gap_s`         | `1.4`    | A pause this long after Qwen's full stop keeps it even if the punctuation model says comma            |
| `grammar_punct`      | `1`      | `1` = the punctuation model decides from the words whether units join with a mark                     |
| `drop_fillers`       | `1`      | `1` = drop hesitation fillers (嗯/呃/um) from committed text                                          |
| `unit_context_chars` | `0`      | Pass this many characters of earlier text to Qwen with each unit (`0` = off)                          |
| `preview_device`     | `0`      | `0` = previews by the 1.7B model on its configured device (yielding to finals); `1` = 0.6B on the NPU |
| `preview_interval_s` | `1.0`    | Re-decode the open sentence this often (`0` = no Qwen previews)                                       |
| `preview_min_s`      | `1.0`    | First preview of a sentence once it holds this much audio                                             |
| `preview_max_s`      | `15.0`   | No previews for an open sentence longer than this                                                     |
| `npu_yield`          | `0`      | `1` = NPU previews pause while a GPU final is queued or running                                       |
| `npu_rollback`       | `5`      | NPU previews reuse the previous hypothesis minus this many tokens (`0` = never reuse)                 |
| `para_draft`         | `1`      | `0` = never show Paraformer text; the overlay waits for the first Qwen preview                        |
| `draft_para_tail`    | `1`      | `1` = append Paraformer words heard after the newest Qwen preview; `0` = Qwen only                    |
| `spec_draft`         | `1`      | `1` = the unit's last preview seeds lossless speculative decoding of the final                        |
| `prefix_cache`       | `1`      | `1` = reuse the hotword prompt's KV cache                                                             |
| `unit_lookback_s`    | `10.0`   | A unit also takes soft speech the server VAD missed up to this far before it (`0` = off)              |
| `soft_vad_threshold` | `0.3`    | VAD threshold that defines "soft speech" for the lookback and the end of a dictation                  |
| `head_check`         | `1`      | `1` = re-decode a unit with the wider preview audio when its final lacks words the draft had          |

## Client: settings

The **Settings → Remote** page ([CLIENT-SETUP.md](CLIENT-SETUP.md)) stores its values in the `remote`
block of `%APPDATA%\com.musingzone.handy-remote\settings_store.json`:

| Key                 | Default | UI label                           |
| ------------------- | ------- | ---------------------------------- |
| `enabled`           | `false` | Use remote server                  |
| `incremental_paste` | `true`  | Paste while speaking               |
| `rdp_caret`         | `true`  | Follow the caret in Remote Desktop |
| `rdp_dictation`     | `false` | Dictate inside Remote Desktop      |
| `host`              | `""`    | Server                             |
| `user`              | `""`    | SSH user                           |
| `ssh_port`          | `22`    | SSH port                           |
| `server_port`       | `47821` | Server port                        |
| `caret_port`        | `47823` | Caret helper port                  |
| `identity_file`     | `""`    | SSH key file                       |
| `ssh_path`          | `""`    | ssh program (empty = automatic)    |
| `host_key`          | `""`    | Server key (pinned when set)       |
| `label`             | `""`    | Server name from the pairing code  |

The ssh command always uses `BatchMode=yes`, `ConnectTimeout=8`, `ServerAliveInterval=15` and
`ServerAliveCountMax=2`. With a pinned key it uses `StrictHostKeyChecking=yes` and the app's own
`remote_known_hosts`; without one, `StrictHostKeyChecking=accept-new`.

### Pairing code

`handy-remote-pair:` followed by unpadded base64url JSON:
`{"v":1,"host":…,"user":…,"ssh_port":22,"server_port":47821,"caret_port":47823,"device_id":…,"label":…,"host_key":"ssh-ed25519 AAAA…"}`.
It contains no secrets.

### Legacy `remote_worker.json`

Before the settings page existed, `%APPDATA%\com.musingzone.handy-remote\remote_worker.json` named the
worker command: `{"command": "C:\\Windows\\System32\\OpenSSH\\ssh.exe", "args": ["-W", "127.0.0.1:47821", "alice@asr-server"], "incremental_paste": true}`
(optional `"enabled": false`, and `"rdp_dictation": true` for [dictating with the remote PC's
Handy](RDP.md#dictating-with-the-remote-pcs-handy)). It is still honoured while remote recognition is off on the settings
page, and renamed to `remote_worker.json.migrated` when you save the page with remote recognition on.

### Client environment variables

| Variable                           | Effect                                                                   |
| ---------------------------------- | ------------------------------------------------------------------------ |
| `HANDY_OVERLAY_CARET=0`            | Overlay stays at the screen edge instead of following the caret          |
| `HANDY_REMOTE_CARET=0`             | No caret lookups inside Remote Desktop windows                           |
| `HANDY_REMOTE_CARET=<host>:<port>` | Address that SSH forwards caret requests to, as seen from the SSH server |
| `HANDY_DISABLE_UPDATER`            | Upstream switch that turns update checks off                             |

## Caret helper

`caret_helper.exe --port N` (default `47823`, or the `HANDY_REMOTE_CARET_PORT` environment variable).
Request line `t` toggles dictation of the Handy running in the helper's session (answer
`{"ok":1,"toggled":1}`); any other line asks for the caret.
`install.ps1` accepts `-Port`, `-TaskName` (default `HandyRemote-CaretHelper`) and `-Uninstall`. See
[RDP.md](RDP.md).

## Feedback tool

`%APPDATA%\handy-remote\feedback.toml`, or the file named by `HANDY_REMOTE_FEEDBACK_CONFIG`. Template
and every key: [`tools/feedback/feedback.example.toml`](../../tools/feedback/feedback.example.toml).
See [FEEDBACK.md](FEEDBACK.md).
