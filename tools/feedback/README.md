# Dictation feedback

A small companion tool for Handy Remote: when a dictation comes out wrong, report it in a few seconds; an
analyzer proposes safe fixes (hotwords, replacement rules, streaming parameters), you accept or reject them,
and accepted changes go to the speech server's lexicon and are verified by re-transcribing the recording.

- `voice_feedback.py report`: pick a recent dictation from Handy's history and type the correct text.
- `voice_feedback.py analyze [report-dir]`: analyse new reports (started automatically, in the background).
- `voice_feedback.py review`: review pending suggestions and apply the accepted ones.
- `voice_feedback.py status`: show settings, connection and reports.
- `voice_feedback.py guide`: open the user guide (`guide.en.html` / `guide.zh-CN.html`).

Handy Remote's tray icon shows the state (blue dot = analysing, orange dot = suggestions or notices waiting)
from `%LOCALAPPDATA%\handy-remote\pending.json` and runs `pyw -3 voice_feedback.py <review|report|guide>`
from its menu. Bind a hotkey of your choice to `pyw -3 <path>\voice_feedback.py report` for quick reports.

## Requirements

- Windows, Python 3.10+ with tkinter (the python.org installer includes it). Stdlib only; numpy is used
  for audio conversion when installed. Reading `feedback.toml` needs Python 3.11+ (or `tomli`).
- Handy Remote (or upstream Handy) with history and recordings enabled.
- A speech server from this repository (`server/`) recent enough to answer `AuditQuery`, `LexiconGet`
  and `LexiconPut`. The tool never opens a shell on the server: everything goes through the worker
  protocol, directly over TCP or through `ssh -W` using the client's own connection settings
  (`%APPDATA%\com.musingzone.handy-remote\remote_connection.json`).

## Configuration

Optional: copy `feedback.example.toml` to `%APPDATA%\handy-remote\feedback.toml` (or point the
`HANDY_REMOTE_FEEDBACK_CONFIG` environment variable at another file). It sets the reports folder,
the UI language (`auto`, `en`, `zh-CN`), connection overrides and the analyzer.

## Analyzer backends

- `copilot` (default): the GitHub Copilot CLI (`copilot` on PATH or `analyzer.copilot.path`), run
  headless with shell and write tools denied, following `ANALYZE.md`.
- `openai`: any OpenAI-compatible `/chat/completions` endpoint. The API key comes from the environment
  variable named in `api_key_env`, or from a generic Windows Credential Manager entry named in
  `api_key_credential`; it is never stored in the config file.
- `off`: reports are only saved; nothing is analysed or proposed.

## Data

Reports, `changelog.jsonl`, `backlog.md` and local lexicon backups live in `reports_dir`
(default `%LOCALAPPDATA%\handy-remote\feedback`); logs, notices and `pending.json` in
`%LOCALAPPDATA%\handy-remote`. Each PC handles only the reports it filed, so `reports_dir` may be shared.

## Tests

```
cd tools\feedback\tests
py -3 -m unittest
```
