# Architecture

Handy Remote keeps upstream Handy's desktop app and replaces its local transcription worker with a
connection to a recognition server on another PC. Everything else (shortcuts, history, paste,
post-processing) works as in Handy.

## Components

```
Client PC                                             Server PC (Windows, CPU / optional GPU / NPU)
┌──────────────────────────────┐                       ┌───────────────────────────────────────┐
│ Handy Remote (Tauri app)     │  ssh.exe -W           │ sshd (OpenSSH Server, key auth)       │
│  worker protocol on stdio ───┼── 127.0.0.1:47821 ═══▶│  └▶ asr_server.py  127.0.0.1:47821    │
│  caret lookup (RDP only) ────┼── 127.0.0.1:47823 ═══▶│  └▶ caret_helper.exe 127.0.0.1:47823  │
│                              │                       │      (in the RDP session)             │
│ voice_feedback.py (optional)─┼── 127.0.0.1:47821 ═══▶│     AuditQuery / LexiconGet / Put     │
└──────────────────────────────┘                       └───────────────────────────────────────┘
```

- **No network port is exposed.** The server binds `127.0.0.1` only. The client starts
  `ssh -W 127.0.0.1:<port> user@server`, which connects stdin/stdout to that loopback port through an
  SSH session. SSH provides authentication (key only) and encryption.
- **Same machine:** when the server host is empty, `localhost`, `127.0.0.1` or `::1`, the app runs
  itself as a relay (`--tcp-relay 127.0.0.1:<port>`) instead of ssh.
- **Model list:** while remote recognition is on, the server appears as a placeholder model
  (`handy-remote-server`) and the local models are not used.

## Worker protocol

The server speaks Handy's own worker protocol over TCP. Each frame is
`[u32 LE json_len][json][u32 LE pcm_bytes][f32 LE pcm]`.

| Request                                          | Purpose                                                         |
| ------------------------------------------------ | --------------------------------------------------------------- |
| `Status`                                         | Cheap health report for **Test connection**; never wakes models |
| `Hello`, `Load`                                  | Handshake and model load (waits for the models)                 |
| `StreamBegin`, `Feed`, `Finalize`, `StreamReset` | Streaming dictation                                             |
| `Run`                                            | One-shot transcription of a whole clip                          |
| `AuditQuery`, `LexiconGet`, `LexiconPut`         | Used by the feedback tool; no shell access needed               |

## Recognition pipeline (server)

1. **Audio** arrives as 16 kHz float PCM while you speak.
2. **Silero VAD** cuts it into speech segments. Segments are merged into _units_ of a few seconds: a
   unit closes when a segment ends (a pause of `vad_min_silence_s`, 0.5 s) and the unit spans at least
   `unit_min_s` (4 s), when `unit_pause_s` (1.1 s) of silence follows it whatever its length, or before
   it would grow past 25 s (a single segment is also capped at 25 s).
   **No audio is dropped silently:** the client has already cut out silence, so audio before a unit (up
   to `unit_lookback_s`) or after the last one that a VAD as sensitive as the client's
   (`soft_vad_threshold`) calls speech joins the neighbouring unit. The VAD is fed in slices of a
   few frames, because one large chunk makes it place a speech start late.
3. **Final text:** each unit is decoded by **Qwen3-ASR 1.7B** (int8, OpenVINO) on the configured device, prompted
   with your hotwords and Handy's custom words.
4. **Live previews:** the open sentence is re-decoded every `preview_interval_s` by **Qwen3-ASR 0.6B
   on the Intel NPU** (`draft_enabled = true`, `preview_device = 1`) or by the 1.7B model on the final-text device (`preview_device = 0`, also
   the fallback without an NPU). A streaming **Paraformer** model on the CPU can show words before the
   first Qwen preview (`para_draft`).
5. **Speculative decoding:** the unit's last NPU preview is used as a draft that the 1.7B model
   verifies in a few steps (`spec_draft`). It is lossless: the output equals plain decoding.
   **Prefix KV cache** reuses the hotword prompt's attention cache (`prefix_cache`). When a final lacks
   words its draft had (typically a soft start), the unit is decoded again with the wider audio the
   preview heard, and the result that agrees better with the draft is kept (`head_check`). If a final
   fails twice, its draft is committed instead.
6. **Punctuation:** a **CT-Transformer** punctuation model decides from the words, not from pause
   length, whether two units join with a comma, a full stop or nothing (`grammar_punct`). Standalone
   fillers (嗯/呃/um) are dropped (`drop_fillers`).
7. **Lexicon:** regex replacement rules are applied to the final text. The lexicon file is
   hot-reloaded.
8. **Commit:** each finished sentence is sent to the client as committed text. With **Paste while
   speaking** on, the client pastes it immediately; at key release only the rest is pasted.
   Only successfully pasted fragments are counted. A blocked or failed fragment stops the queue
   and is retried with the remaining text at key release; cancellation discards queued fragments.
   If the final transcript rewrites an already pasted sentence, the corrected suffix is appended
   without dropping words and a paste warning asks you to review the full transcript in History
   (the client cannot safely erase text already entered in another app).

## Model lifecycle (keep-alive)

- Models load at server start, followed by a **warm-up** that runs the real request path (a recorded
  clip `warmup.wav` if present, otherwise silence).
- After `idle_unload_hours` without use, and optionally inside a daily `offline_window`, the models are
  unloaded to free memory.
- The next dictation reloads them on demand. Meanwhile the server
  buffers your audio and sends the status token `{{handy-remote:waking}}`; the client shows a localized
  "waking the model" notice in the overlay. The buffered audio is replayed after the load, so nothing
  is lost.

## Client additions

- **Caret-following overlay** (Windows): the recording card is placed under the text caret, found via
  Win32 `GetGUIThreadInfo`, then MSAA `OBJID_CARET` (Chromium/Electron apps), then UI Automation
  `TextPattern2`/`TextPattern`. Each lookup runs on a short-lived thread with a timeout.
- **Remote Desktop:** when an `mstsc` window is in front, the caret is asked from the caret helper in
  the remote session over a persistent `ssh -W` tunnel and mapped into the RDP client area
  ([RDP.md](RDP.md)).
- **Ready cue:** the card stays dimmed until the microphone really delivers samples, then shows a
  microphone glyph with an expanding ring (screen readers get a localized label), so the first word is
  not lost.
- **Feedback tray dot:** blue while a report is analysed, orange when suggestions wait
  ([FEEDBACK.md](FEEDBACK.md)).
- **Own identity:** product name "Handy Remote", identifier `com.musingzone.handy-remote`, own signed
  updater ([UPDATING.md](UPDATING.md)).

## Files on the client

App data folder: `%APPDATA%\com.musingzone.handy-remote`.

| File                     | Purpose                                                             |
| ------------------------ | ------------------------------------------------------------------- |
| `settings_store.json`    | All settings, including the `remote` block                          |
| `remote_ed25519`, `.pub` | This app's SSH key (created by **Create key**)                      |
| `remote_known_hosts`     | The pinned server host key                                          |
| `remote_connection.json` | Connection details for the feedback tool (no secrets)               |
| `remote_worker.json`     | Legacy hand-written worker command; replaced once the page is saved |
| `migrated-from.txt`      | Written after the one-time copy from upstream Handy                 |
