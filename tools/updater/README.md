# Unattended updater

Keeps Handy Remote (and optionally a recognition server on the same PC) up to date without interrupting
dictation. The in-app updater (Settings → About) does the same interactively; use this when you want it to
happen on its own.

```powershell
# Register the task (at logon + every 6 h, hidden, no admin rights) and run it once now
powershell -ExecutionPolicy RemoteSigned -File tools\updater\Install-AutoUpdate.ps1 -RunNow

# Also update the recognition server installed on this PC (folder holding asr_server.py)
powershell -ExecutionPolicy RemoteSigned -File tools\updater\Install-AutoUpdate.ps1 -ServerDir C:\handy-remote\server

# Check only / install a given release (a pre-release to try, or an older one) / remove
powershell -ExecutionPolicy RemoteSigned -File "$env:LOCALAPPDATA\handy-remote\updater\Update-HandyRemote.ps1" -CheckOnly
powershell -ExecutionPolicy RemoteSigned -File "$env:LOCALAPPDATA\handy-remote\updater\Update-HandyRemote.ps1" -Version 0.10.1-remote.6
powershell -ExecutionPolicy RemoteSigned -File tools\updater\Install-AutoUpdate.ps1 -Uninstall
```

Each run:

1. reads the latest release of `MusingZone/handy-remote` (set `GITHUB_TOKEN` for a private fork);
2. downloads the `*_x64-setup.exe`, its `.sig` and `SHA256SUMS.txt`, and checks both the SHA-256 and the
   minisign signature against the app's updater public key (`Minisign.cs`, no external tools);
3. waits until nobody has typed or clicked and Handy Remote has not logged anything for 2 minutes
   (gives up after 45 minutes and tries again next run);
4. backs up the installed files, installs silently (`/S`), restarts Handy Remote with `--start-hidden` and
   checks it is still running 30 seconds later; otherwise restores the backup and restarts the old version;
5. with `-ServerDir`: when the release's `handy-remote-server-*.zip` is newer than `VERSION` and the server
   has been idle for 2 minutes, backs up and replaces the code (never `config.toml`, the lexicon or models,
   which live in the server home), restarts it and asks it for `Status`; rolls back if that fails.

Log: `%LOCALAPPDATA%\handy-remote\updater\update.log`. `-NotifyScript <file.ps1>` is called with
`-Message <text>` after an install, rollback or failure, e.g. to send a phone notification.
