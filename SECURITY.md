# Security

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** for this repository:
<https://github.com/MusingZone/handy-remote/security/advisories/new>. Do not open a public issue for
security problems. Include the version, the affected component (client, server, caret helper,
feedback tool, updater) and steps to reproduce.

Problems that also affect upstream Handy should be reported to
[upstream](https://github.com/cjpais/Handy) as well.

## Security model

**Network exposure**

- The recognition server listens on `127.0.0.1` only (`bind` in `config.toml`). Its protocol has no
  authentication of its own; the server logs a warning if you bind it elsewhere.
- Clients reach it only through SSH: `ssh -W 127.0.0.1:<port>` forwards a single connection over an
  authenticated SSH session. No port of the recognition server is opened on the network.
- The caret helper also listens on `127.0.0.1` only and is reached the same way.

**Authentication**

- The client signs in with an SSH key (`BatchMode=yes`, no passwords).
- `doctor.py authorize` adds the client key with `restrict,port-forwarding` and `permitopen` limited to
  the server and caret helper ports: the key cannot open a shell, run commands or reach other ports.
- **Host key pinning:** a pairing code carries the server's SSH host key. Once set (**Server key**),
  the client refuses a server with a different key (`StrictHostKeyChecking=yes` with the app's own
  known-hosts file).
- The pairing code contains no secrets (host, user, ports, label, public host key).

**Data**

- No telemetry. The fork adds no analytics; network traffic is the SSH connection you configure, update
  checks against this repository's GitHub releases, and model downloads during server setup.
- Audio and text stay on your PCs. The server keeps audit records with transcript text in
  `<home>\logs\asr-*.jsonl` (deleted after `log_days`, default 14) and, with `warmup_capture`, one
  short dictation clip as `config\warmup.wav` for warm-ups.
- The feedback tool sends report **text** (corrections, server records, lexicon, custom words) to the
  analyzer backend you configure (GitHub Copilot CLI or an OpenAI-compatible endpoint), and nothing
  with `backend = "off"`. Audio is never sent. API keys come from an environment variable or Windows
  Credential Manager, never from its config file.

**Updates**

- Installers are signed with this project's updater key (minisign); the in-app updater and the
  unattended updater verify the signature, and the unattended updater also the SHA-256 in
  `SHA256SUMS.txt`, before installing.
- Releases are not Authenticode code-signed, so Windows SmartScreen may warn on first install.

## Supported versions

Only the latest release receives fixes.
