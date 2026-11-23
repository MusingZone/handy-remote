# Copilot bridge

Serves your GitHub Copilot models as a small OpenAI-compatible API on this computer
(`http://127.0.0.1:47870/v1`), so Handy Remote's **refine before paste** can use them, or any other
OpenAI-compatible client can. It is built on the official
[GitHub Copilot SDK](https://www.npmjs.com/package/@github/copilot-sdk).

| Endpoint                    |                                                      |
| --------------------------- | ---------------------------------------------------- |
| `GET /v1/models`            | the models your Copilot plan allows                  |
| `POST /v1/chat/completions` | one answer per request; no tools, nothing remembered |
| `GET /health`               | liveness                                             |

## Requirements

- Windows, Node.js 22.12 or later.
- A GitHub account with a Copilot plan, signed in to GitHub Copilot on this computer (for example
  with `copilot` → `/login`, or `gh auth login` on the same account).
- Each request counts against your Copilot plan like any other Copilot request. Choose a model
  available to your own account and compare its latency and cost using your plan's current terms.

## Install

```powershell
powershell -ExecutionPolicy RemoteSigned -File Install-CopilotBridge.ps1
```

This copies the bridge to `%LOCALAPPDATA%\copilot-bridge\app`, installs its one dependency, and
registers a scheduled task (`CopilotBridge`) that starts it hidden at logon. `-Port <n>` picks another
port; `-Uninstall` removes it again.

In Handy Remote: **Post-processing → Find local models** lists **GitHub Copilot (Copilot bridge)**
with the models available to your account; choose one, press **Use**, then run the **Speed test**.

## Security and privacy

- Only loopback connections are served; requests with an `Origin` header (from a browser page) are
  refused. Set `COPILOT_BRIDGE_TOKEN` to also require `Authorization: Bearer <token>`.
- Dictated text goes to GitHub Copilot, under your account's Copilot data policy. It is never written
  to the bridge log (`%LOCALAPPDATA%\copilot-bridge\bridge.log`).
- Sessions run with no tools, every permission request rejected, no session store and no telemetry.

Environment: `COPILOT_BRIDGE_PORT`, `COPILOT_BRIDGE_HOME` (data folder), `COPILOT_BRIDGE_TOKEN`.

Tests: `node --test` in this folder.
