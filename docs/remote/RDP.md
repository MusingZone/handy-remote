# Dictating inside Remote Desktop

When you dictate into a Remote Desktop (`mstsc`) window, the laptop only sees the RDP window, not the
text caret of the app inside it. The **caret helper** runs in your session on the remote PC and tells
Handy Remote where that caret is, so the overlay can sit under it just like on the local desktop.

The helper is reached through the same SSH connection settings as the recognition server, so it must
run on the **server PC**, in the session you open with Remote Desktop.

## How it works

1. When an RDP window comes to the front, Handy Remote opens a persistent tunnel
   `ssh -W 127.0.0.1:<Caret helper port> user@server` (pre-warmed, so the first lookup is fast).
2. For each lookup it sends one request line; the helper answers with one JSON line: the foreground
   app's caret in physical pixels plus the remote virtual-screen bounds.
3. Handy Remote maps the rectangle into the RDP window's client area (also when the window is scaled
   with smart sizing) and places the overlay under it.

The helper uses the same lookup order as the client: Win32 system caret (`GetGUIThreadInfo`), MSAA
`OBJID_CARET`, then UI Automation. It listens on `127.0.0.1` only.

## Install on the server PC

Get the repository's `tools\caret-helper` folder onto the server PC and keep it in a permanent place,
for example `C:\handy-remote\tools\caret-helper`.

1. Build it with the C# compiler that ships with .NET Framework 4 (no SDK needed):

   ```powershell
   C:\handy-remote\tools\caret-helper\build.cmd
   ```

   It prints `BUILD-OK` and creates `caret_helper.exe` next to the script.

2. Register it, from an **elevated** PowerShell, signed in as the user who dictates:

   ```powershell
   powershell -ExecutionPolicy RemoteSigned -File C:\handy-remote\tools\caret-helper\install.ps1
   ```

   It creates the scheduled task `HandyRemote-CaretHelper`, which starts the helper at logon and
   whenever a session connects (console or remote) or is unlocked. A second copy exits at once, so
   repeated triggers are harmless. The task runs elevated so the helper can also read the caret of
   elevated apps such as an Administrator terminal; Windows blocks such queries from a lower level.
   The script ends with `caret_helper running on 127.0.0.1:47823 (pid …)`.

| Option             | Meaning                                       |
| ------------------ | --------------------------------------------- |
| `-Port <n>`        | Listen port (default `47823`)                 |
| `-TaskName <name>` | Task name (default `HandyRemote-CaretHelper`) |
| `-Uninstall`       | Stop the helper and remove the task           |

If you change the port, set the same **Caret helper port** in the client.

## Client side

- **Settings → Remote → Follow the caret in Remote Desktop** (on by default).
- **Caret helper port** must match the helper (default `47823`; the pairing code carries it).
- The client key authorized with `doctor.py authorize` may already open `127.0.0.1:47823`. If you use
  another port, add a matching `permitopen="127.0.0.1:<port>"` to that key's line in
  `authorized_keys` on the server.
- `HANDY_REMOTE_CARET=0` turns the lookups off; `HANDY_REMOTE_CARET=<host>:<port>` changes the address
  the SSH server forwards to.

## Dictating with the remote PC's Handy

The default above records on the laptop and pastes into the RDP window. Alternatively the Handy on
the remote PC can do the whole dictation: it records the laptop microphone that Remote Desktop
redirects, recognizes, refines and pastes inside the session, with its overlay under the caret there.

1. In the RDP connection, record audio from this computer: **Show Options → Local Resources → Remote
   audio → Settings → Record from this computer** (`audiocapturemode:i:1`; `audioqualitymode:i:2`
   keeps the quality high). Reconnect; the remote PC then lists a **Remote Audio** microphone.
2. Install and start Handy Remote on the remote PC and give it a transcribe shortcut that does not
   clash with the laptop's (keys sent to a full-screen RDP window reach the remote session).
3. On the laptop, turn on **Settings → Remote → Dictate inside Remote Desktop** (or
   `"rdp_dictation": true` in a legacy `remote_worker.json`).

With an RDP window in front and nothing recording on the laptop, the laptop's dictation shortcut (in
toggle mode) and `handy --toggle-transcription` then send the request line `t` over the caret tunnel;
the helper toggles the Handy in its session through the app's single-instance window (the same
message a second `handy --toggle-transcription` sends), which takes about 10 ms once the tunnel is
open. This also works for shortcuts injected by software such as mouse-button utilities or
AutoHotkey, which `mstsc` does not forward to the session. If the helper cannot be reached, or is an
older build without `t`, the laptop dictates itself as before.

Voice feedback follows the same rule: `handy --voice-feedback report` (or `review`, `guide`) sends
`f report` over the tunnel while an RDP window is in front, so the report opens in the session whose
Handy did the last dictation; otherwise it opens on the laptop. Run `voice_feedback.py pending` once on
the remote PC as well (see [Dictation feedback](FEEDBACK.md)).

Shortcuts injected into a Remote Desktop window should avoid Alt: when software swallows the key that
was pressed with Alt, the remaining bare Alt press shows the menu key tips of apps such as Windows 11
Notepad, and the dictated text is lost. Ctrl+F9 works; Ctrl+Alt+F9 does not.

### Administrator windows and recording readiness

There are two different input paths. When the laptop records, the elevated **caret helper** reads the
remote caret. When the remote PC's Handy records, **Handy itself** must run at the same or higher
integrity level as the target application. An elevated helper does not grant those rights to Handy.
Restarting Handy unelevated can therefore break both pasting and caret following in administrator
VS Code, even though the helper is healthy. For an explicitly elevated updater policy, see
[Unattended updater](../../tools/updater/README.md).

VAD preloading removes model initialization from the first dictation, not RDP microphone startup.
In on-demand mode, Remote Audio is opened after the shortcut; opening the stream and receiving its
first audio are separate timings. Wait for the recording-ready cue, not just the overlay appearing.
Diagnose `vad_ensure`, `open`, and `first audio arrived` separately in the app log. Do not turn on
**Always-on microphone** to hide this delay without the user's consent.

## Troubleshooting

| Symptom                                                                  | Check                                                                                                  |
| ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------ |
| Overlay stays at the screen edge in RDP                                  | Is `caret_helper.exe` running in the RDP session? `Get-Process caret_helper` on the server             |
| `caret_helper NOT running (no interactive session?)`                     | Run `install.ps1` while signed in interactively; the helper needs a desktop session                    |
| Works for normal apps, not for an admin terminal                         | The task must run elevated: re-run `install.ps1` from an elevated PowerShell                           |
| Admin-app paste warning and bottom overlay when the remote Handy records | Check Handy's own run level, not just the helper's; restore its approved administrator launch policy   |
| Overlay is offset                                                        | Make sure the RDP window shows the whole remote desktop; report the case with the RDP display settings |
