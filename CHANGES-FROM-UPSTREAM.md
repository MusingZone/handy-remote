# Changes from upstream Handy

Handy Remote is based on [Handy](https://github.com/cjpais/Handy) v0.10.1 by CJ Pais (MIT). This file
lists the user-visible changes of the fork, grouped by area. Everything not listed here behaves as in
upstream Handy.

## Remote recognition

- Speech recognition can run on another Windows PC (the server) reached over SSH. The client starts
  `ssh -W 127.0.0.1:<port> user@server`; the server listens on loopback only.
- **Settings → Remote** page: pairing code import, SSH host / user / port / key file / ssh program,
  server port, host key pinning, caret helper port, **Test connection**, **Create key** and a copyable
  authorization command. Remote recognition is off until you turn it on; with it off, the local
  engines are used exactly as before.
- Built-in relay so a server on the same PC works without SSH.
- "Waking the server's models…" notice in the overlay while an unloaded server reloads; audio is
  buffered meanwhile, nothing is lost.
- New settings strings translated into all 26 UI languages.

## Dictation experience

- **Live previews:** the overlay shows the sentence being recognized while you speak.
- **Incremental paste:** finished sentences are pasted while you keep speaking (**Paste while
  speaking**, on by default for remote recognition). Failed fragments remain available for the
  final paste, cancellation discards queued fragments, and rewritten final text is preserved
  with a review warning instead of silently dropping characters.
- **Overlay at the caret:** the overlay follows the text caret (Win32 caret, MSAA, UI Automation),
  falling back to the configured position. `HANDY_OVERLAY_CARET=0` turns it off.
- **Remote Desktop:** inside `mstsc` windows the caret position comes from a small helper running in
  the remote session ([`tools/caret-helper`](tools/caret-helper), [docs](docs/remote/RDP.md)).
- **Ready cue:** a language-neutral microphone glyph with an expanding ring appears only once the
  microphone really delivers audio, so the first word is not cut off.

## Cloud recognition and LLM refinement

- **Settings → Cloud** page: speech recognition by a public cloud API (OpenAI, Azure OpenAI, Azure AI
  Speech, Google Cloud Speech-to-Text, Deepgram, AssemblyAI, ElevenLabs, Groq, Mistral Voxtral,
  Alibaba Qwen3-ASR, SiliconFlow, or any OpenAI-compatible server), with per-provider endpoint, model
  and key and a **Test** button. The executable runs as the recognition worker in cloud mode
  (`--cloud-worker`); keys reach it in an environment variable. Off by default.
- Post-processing: **Refine every dictation** (not only the post-processing shortcut), a
  "Polish & structure" prompt that also formats spoken enumerations as numbered lists, local
  OpenAI-compatible providers (Ollama, LM Studio, llama.cpp, Foundry Local, OpenVINO Model Server,
  Lemonade) with editable addresses, cloud providers DeepSeek, DashScope, Moonshot, Zhipu,
  SiliconFlow, Gemini and Mistral, **Find local models** with a recommended model, and a **Speed
  test**. New providers are inserted before "Custom"; reasoning is disabled for them.
- GitHub Copilot as a refine provider through the [Copilot bridge](tools/copilot-bridge/README.md)
  (loopback OpenAI-compatible server on the Copilot SDK), listed by **Find local models**.
- **My rules**: a rules file (one rule per line, read on every dictation, can live in a synced
  folder) injected into the refine prompt at `${rules}`, plus a rule that keeps the custom words as
  written. Custom endpoints select their own authentication mode; keys are never sent in two
  authentication headers.
- Named endpoints can be imported from a private, non-secret JSON file; endpoint labels, addresses,
  models and authentication are configuration, not application-specific code. Imports are atomic,
  reject embedded credentials, and cannot move a saved key to a changed destination.
  Local discovery stays on loopback; discovery and LLM requests do not follow redirects.
- Fix: the Chinese text of the shipped "Polish & structure" prompt was stored as `?` characters in
  v0.10.1-remote.1 and .2; stored copies are repaired on start.
- Incremental paste now stays on with post-processing enabled and is only skipped for dictations the
  LLM will rewrite. See [docs/remote/CLOUD-AND-LLM.md](docs/remote/CLOUD-AND-LLM.md).

## Feedback loop

- Tray dot: blue while a dictation report is being analysed, orange when suggestions are waiting.
- Tray items **Report the last dictation…**, **Review suggestions** and **Dictation feedback guide**,
  driven by the companion tool [`tools/feedback`](tools/feedback/README.md)
  ([docs](docs/remote/FEEDBACK.md)).

## Recognition server (new, `server/`)

- Python + OpenVINO server: Qwen3-ASR 1.7B for final text on GPU or CPU, optional Qwen3-ASR 0.6B on
  an Intel NPU for previews and lossless speculative decoding, prefix KV cache, Silero VAD,
  streaming Paraformer drafts, CT-Transformer grammar punctuation and filler removal.
- Hot-reloaded lexicon (hotwords, replacement rules, streaming parameters) with server-side history.
- Model lifecycle: idle unload, optional offline window, on-demand reload, real-path warm-up.
- Hardware-neutral defaults: CPU recognition, NPU drafts disabled and warm-up recording opt-in.
  Hardware detection or a private config enables accelerators; the example lexicon has no
  machine-specific stream tuning.
- `setup.ps1`, `doctor.py` (checks, pairing code, key authorization), scheduled task with supervisor
  and watchdog. Requests `Status`, `AuditQuery`, `LexiconGet`, `LexiconPut` besides recognition.
  See [docs/remote](docs/remote/README.md).

## App identity and updates

- Product name **Handy Remote**, identifier `com.musingzone.handy-remote`: installs next to upstream
  Handy instead of replacing it.
- On first run, settings and models are copied (never moved) from upstream Handy's
  `%APPDATA%\com.pais.handy`.
- The in-app updater uses this repository's releases and signing key
  ([UPDATING.md](docs/remote/UPDATING.md)); optional unattended updater in
  [`tools/updater`](tools/updater/README.md).

## Repository automation

- Upstream Handy's workflows stay in the tree, so upstream releases merge cleanly, but are disabled in
  this fork. The fork runs its own: `remote-ci.yml` (upstream lint and Rust tests, server and
  feedback tool tests, Markdown links, scrub check), `remote-release.yml` (signed Windows installer,
  `latest.json`, server zip, `SHA256SUMS.txt`, **prerelease only**) and `upstream-sync.yml`
  (manually prepares a checked pull request; never auto-merges or publishes).
  A release builds the exact commit that passed CI, audits installed contents and scans source,
  history, notes and packaged assets before publishing.
  See [UPDATING.md](docs/remote/UPDATING.md#5-for-maintainers-workflows-and-secrets).

## Source history

The fork is maintained as a reviewed delta on upstream v0.10.1. Use
`git diff v0.10.1..HEAD` for the complete public changes and `git log v0.10.1..HEAD` for the
current clean history. Machine-local configuration, benchmark records and operational notes are
not part of that history.
