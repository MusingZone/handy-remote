# Client setup

## 1. Install Handy Remote

Download the Windows installer from the
[latest release](https://github.com/MusingZone/handy-remote/releases/latest) and run it.

- **SmartScreen:** releases are not code-signed. Windows may show "Windows protected your PC"; choose
  **More info → Run anyway**. The in-app updates are still verified with the project's own update
  signature ([UPDATING.md](UPDATING.md)).
- **Side by side with Handy:** Handy Remote has its own identity (`com.musingzone.handy-remote`), so it
  installs next to upstream Handy without replacing it. If both run at the same time, give them
  different shortcuts.
- **First run:** if upstream Handy's data folder `%APPDATA%\com.pais.handy` exists, Handy Remote
  copies its settings, history, recordings and models into `%APPDATA%\com.musingzone.handy-remote`
  (model files are hard-linked when possible, logs are skipped). Nothing is moved or changed in
  upstream Handy's folder. This happens once, only while the new folder has no settings yet.

## 2. Check the SSH client

Windows 10 and 11 include the OpenSSH client (`C:\Windows\System32\OpenSSH\ssh.exe`). Handy Remote
finds it automatically. If **ssh program** shows "ssh not found", install it from **Settings → System →
Optional features → OpenSSH Client**, or in an elevated PowerShell:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0
```

## 3. Connect to the server

Prepare the server first ([SERVER-SETUP.md](SERVER-SETUP.md)). Then open **Settings → Remote**:

1. **Pairing code:** paste the `handy-remote-pair:…` line from `doctor.py pair` and press **Apply**.
   The fields below are filled in, including the server's host key.
2. **Server:** if the server's computer name does not resolve from this PC, replace it with an address
   or name that does (for example `asr-server.lan`).
3. **This computer's key → Create key**, then **Copy command**, and run the copied
   `doctor.py authorize "…"` command on the server
   ([SERVER-SETUP.md § 6](SERVER-SETUP.md#6-authorize-the-clients-key)).
4. **Test connection.** Expected: "Connected in … ms; the server answered in … ms." and either
   "Models are loaded." or "Models are unloaded; they load on the first dictation".
5. **Save.** Recognition now goes to the server; the server appears as the selected model.

Without a pairing code, fill in **Server**, **SSH user**, **SSH port** and **Server port** by hand.
The host key is then accepted on first connect and stored in your normal SSH `known_hosts`.

## Settings on the page

| Setting                                | Default   | Meaning                                                                                                                                     |
| -------------------------------------- | --------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| **Use remote server**                  | off       | Send dictation to the server instead of the local models                                                                                    |
| **Paste while speaking**               | on        | Paste each finished sentence while you are still recording                                                                                  |
| **Follow the caret in Remote Desktop** | on        | Overlay under the caret of the app inside an RDP window; needs the caret helper ([RDP.md](RDP.md))                                          |
| **Dictate inside Remote Desktop**      | off       | With an RDP window in front, the shortcut starts Handy in the remote session instead ([RDP.md](RDP.md#dictating-with-the-remote-pcs-handy)) |
| **Server**                             | –         | Host name or address; `localhost` = server on this PC (no SSH)                                                                              |
| **SSH user**                           | –         | Account on the server                                                                                                                       |
| **SSH port**                           | 22        | SSH port of the server                                                                                                                      |
| **Server port**                        | 47821     | Port of the recognition server on the server itself                                                                                         |
| **Caret helper port**                  | 47823     | Port of the caret helper on the server                                                                                                      |
| **SSH key file**                       | empty     | Private key; empty = this app's own key if it exists, else your SSH defaults                                                                |
| **ssh program**                        | Automatic | Path to `ssh.exe`                                                                                                                           |
| **Server key**                         | from code | Pinned SSH host key; a server with a different key is refused. **Clear** removes the pin                                                    |

**Paste while speaking** is skipped automatically for dictations a language model will rewrite (the
post-processing shortcut, or **Refine every dictation**) and when a transcription hook could still
change the final text.

## Using it

- Dictate as with Handy. The overlay appears under the text caret (Windows) and shows a live preview.
- The overlay stays dimmed until the microphone really delivers sound; when the microphone glyph with
  the expanding ring appears, start speaking.
- If the server had unloaded its models, the overlay says the model is waking up. Keep speaking: the
  audio is buffered and transcribed once the models are loaded (about 12 s).

## Server on the same PC

Set **Server** to `localhost`. Handy Remote then connects to `127.0.0.1:<Server port>` through its
built-in relay, without SSH.

## Switching back to local recognition

Turn **Use remote server** off and **Save**, then choose a local model on the **Models** page.

## Client environment variables

| Variable                           | Effect                                                                                      |
| ---------------------------------- | ------------------------------------------------------------------------------------------- |
| `HANDY_OVERLAY_CARET=0`            | Keep the overlay at the screen edge instead of under the caret                              |
| `HANDY_REMOTE_CARET=0`             | Never ask the caret helper (RDP caret off)                                                  |
| `HANDY_REMOTE_CARET=<host>:<port>` | Address the SSH server forwards caret requests to (default `127.0.0.1:<Caret helper port>`) |
| `HANDY_DISABLE_UPDATER`            | Upstream switch: turn off update checks                                                     |

More in [CONFIGURATION.md](CONFIGURATION.md). Problems: [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
