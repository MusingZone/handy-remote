# Dictation feedback

When a dictation comes out wrong, report it in a few seconds. An analyzer proposes safe fixes
(hotwords, replacement rules, streaming parameters); you accept or reject them; accepted changes go to
the server's lexicon and are verified by transcribing the recording again. The tool lives in
[`tools/feedback`](../../tools/feedback/README.md).

## Requirements

- Windows with Python 3.10+ including tkinter (the python.org installer has it). Reading
  `feedback.toml` needs Python 3.11+ (or the `tomli` package). numpy is used when installed.
- Handy Remote with history and recordings kept.
- A server from this repository (it answers `AuditQuery`, `LexiconGet` and `LexiconPut`). The tool
  never opens a shell on the server: it uses the worker protocol through the client's own connection
  settings (`%APPDATA%\com.musingzone.handy-remote\remote_connection.json`), so the restricted client
  key is enough.
- An analyzer backend (below), or `off`.

## Install

Copy `tools\feedback` to a permanent folder, e.g. `C:\handy-remote\tools\feedback`, then:

```powershell
cd C:\handy-remote\tools\feedback
py -3 voice_feedback.py status    # settings, connection, reports
py -3 voice_feedback.py pending   # tells Handy Remote's tray where the tool is
```

After that, the Handy Remote tray menu has **Report the last dictation…**, **Review suggestions** and
**Dictation feedback guide**. For a quick report, bind a hotkey of your choice to
`handy --voice-feedback report` (Handy Remote 0.10.1-remote.5 or later; inside Remote Desktop it opens
the report on the remote PC, see [RDP](RDP.md)) or to
`pyw -3 C:\handy-remote\tools\feedback\voice_feedback.py report`.

Optional settings: copy `feedback.example.toml` to `%APPDATA%\handy-remote\feedback.toml` and edit it
(reports folder, UI language `auto`/`en`/`zh-CN`, connection overrides, analyzer).

## The five steps

| Step                  | Who / when               | What happens                                                                                                                                                                                                                                                                                                                                                  |
| --------------------- | ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. **Report**         | you, right away          | Hotkey or tray **Report the last dictation…**. The newest dictation is preselected; fix the wrong parts and press **Ctrl+Enter**. Optionally the just-typed text is replaced by your correction (Ctrl+Z undoes it).                                                                                                                                           |
| 2. **Analyse**        | background, ~1 minute    | The tray icon gets a **blue dot**. Your correction, the server's record of that dictation and the current lexicon go to the analyzer, which classifies the problem and proposes only safe changes. Replacement rules are first tried against your history so you see how many past dictations they would touch. Ideas that need code changes go to a backlog. |
| 3. **Tray dot**       | when analysis is done    | The dot turns **orange** and stays until you look. Nothing pops up by itself.                                                                                                                                                                                                                                                                                 |
| 4. **Review**         | you, whenever convenient | Click the tray icon. Each suggestion explains why; confident ones are preselected. Choose **Apply selected**, **Reject all** or **Later**.                                                                                                                                                                                                                    |
| 5. **Apply + verify** | background, seconds      | Accepted changes are written to the server's `lexicon.json` (previous version backed up on the server and locally) and are live for the next dictation, no restart. The reported recording is transcribed again and compared with your correction. Better or unchanged: silence. Worse, or the check failed: the orange dot returns with a notice.            |

Tray icon states: plain = nothing waiting; blue dot = a report is being analysed; orange dot =
suggestions or a notice to review.

## Analyzer backends

Set `[analyzer] backend` in `feedback.toml`.

| Backend             | How it runs                                                                                                                                                                                                                                       |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `copilot` (default) | The GitHub Copilot CLI (`copilot` on PATH or `analyzer.copilot.path`), headless, with shell and write tools denied, following [`ANALYZE.md`](../../tools/feedback/ANALYZE.md)                                                                     |
| `openai`            | Any OpenAI-compatible `/chat/completions` endpoint (`base_url`, `model`). The API key comes from the environment variable named in `api_key_env` or a generic Windows Credential Manager entry named in `api_key_credential`; never from the file |
| `off`               | Reports are only saved; nothing is analysed or proposed                                                                                                                                                                                           |

Example for an OpenAI-compatible endpoint with the key in Credential Manager:

```powershell
cmdkey /generic:handy-remote-feedback /user:api /pass
```

```toml
[analyzer]
backend = "openai"

[analyzer.openai]
base_url = "https://api.openai.com/v1"
model = "gpt-4o-mini"
api_key_credential = "handy-remote-feedback"
```

## What is sent to the analyzer

Text only: the recognized and corrected text, your tags and note, the server's per-unit record
(drafts, raw and final text, durations, pauses, latency), the current lexicon, Handy's custom words
and the recent change log. The audio stays on your PC and the server; it is used only for the
re-transcription check. With `backend = "off"` nothing leaves your machines.

## What is worth reporting

- Names, product names and technical terms that keep coming out wrong (hotwords, replacement rules).
- A word consistently replaced by a homophone.
- Clearly wrong punctuation (extra comma, missing sentence break).
- Missing or swallowed words, high latency (the server record helps find the cause).

One sentence per report, fixing only the wrong parts, gives the best analysis.

## Data

| What                                                | Where                                                                  |
| --------------------------------------------------- | ---------------------------------------------------------------------- |
| Reports (recording, correction, analysis, decision) | `%LOCALAPPDATA%\handy-remote\feedback\reports\` (or `reports_dir`)     |
| Change log, backlog, local lexicon backups          | `changelog.jsonl`, `backlog.md`, `lexicon-history\` in the same folder |
| Logs, notices, tray state                           | `%LOCALAPPDATA%\handy-remote` (`pending.json`)                         |
| Server-side lexicon history                         | `<server home>\config\lexicon-history\` (last 50)                      |

Each PC handles only the reports it filed, so `reports_dir` may be a shared folder.
