# Updating

## 1. In-app updater (client)

Handy Remote checks this repository's releases, not upstream Handy's. The update feed is
`https://github.com/MusingZone/handy-remote/releases/latest/download/latest.json`; every installer in it
carries a minisign signature that the app verifies with the public key built into it.

- **Settings → About → Check for updates**, or the tray menu **Check for Updates...**.
- Set the environment variable `HANDY_DISABLE_UPDATER` to turn update checks off.

Settings, models and history stay in `%APPDATA%\com.musingzone.handy-remote` across updates.

## 2. Unattended updater (optional)

[`tools/updater`](../../tools/updater/README.md) installs updates on its own, only when you are not
dictating. Run once, as yourself (no admin rights needed):

```powershell
powershell -ExecutionPolicy RemoteSigned -File tools\updater\Install-AutoUpdate.ps1 -RunNow
```

It copies the scripts to `%LOCALAPPDATA%\handy-remote\updater` and registers the hidden per-user
scheduled task `HandyRemote-AutoUpdate` (at logon and every 6 hours). Each run:

1. Reads the latest GitHub release of `MusingZone/handy-remote`.
2. Downloads the Windows installer, its `.sig` and `SHA256SUMS.txt`; checks the SHA-256 and the minisign
   signature against the app's updater public key. Any mismatch stops the run.
3. Waits until Handy Remote is idle: no keyboard or mouse input and no log activity for 2 minutes (gives
   up after 45 minutes and tries again next run).
4. Backs up the current installation and runs the NSIS installer silently (`/S`).
5. Restarts Handy Remote with `--start-hidden` and checks 30 seconds later that it is still running.
   Otherwise it restores the backup and restarts the old version.

| Option                     | Meaning                                                                      |
| -------------------------- | ---------------------------------------------------------------------------- |
| `-RunNow`                  | Start a run right after registering                                          |
| `-EveryHours <n>`          | Interval (default `6`)                                                       |
| `-TaskName <name>`         | Task name (default `HandyRemote-AutoUpdate`)                                 |
| `-ServerDir <folder>`      | Also update a recognition server on this PC (folder holding `asr_server.py`) |
| `-NotifyScript <file.ps1>` | Called with `-Message <text>` after an install, rollback or failure          |
| `-Uninstall`               | Remove the task (log, downloads and backups stay)                            |

Check without installing:

```powershell
powershell -ExecutionPolicy RemoteSigned -File "$env:LOCALAPPDATA\handy-remote\updater\Update-HandyRemote.ps1" -CheckOnly
```

Try a pre-release (or go back to an older release) — the scheduled task never moves to a pre-release
on its own, and it does not downgrade one either:

```powershell
powershell -ExecutionPolicy RemoteSigned -File "$env:LOCALAPPDATA\handy-remote\updater\Update-HandyRemote.ps1" -Version 0.10.1-remote.6 -IdleMinutes 0
```

Log: `%LOCALAPPDATA%\handy-remote\updater\update.log`.

## 3. Server

The server is plain Python code; its configuration, lexicon, models and logs live in the server home
(`%LOCALAPPDATA%\handy-remote-server`) and are not touched by an update.

**With the unattended updater** on the server PC (`-ServerDir`): when a release contains a newer
`handy-remote-server-*.zip`, it waits until the server has been idle for 2 minutes, backs up and
replaces the code, restarts the server and checks that it answers `Status`; otherwise it rolls back.

**By hand:**

1. Replace the contents of the code folder (e.g. `C:\handy-remote\server`) with the new `server\`
   folder. Your `config.toml` and `lexicon.json` are in the server home, not there.
2. Update the Python packages if `requirements.txt` changed:

   ```powershell
   & "$env:LOCALAPPDATA\handy-remote-server\.venv\Scripts\python.exe" -m pip install -r C:\handy-remote\server\requirements.txt
   ```

3. Restart and wait for `engine ready`:

   ```powershell
   powershell -ExecutionPolicy RemoteSigned -File C:\handy-remote\server\scripts\restart_server.ps1
   ```

New models are downloaded by running `setup.ps1` again; it skips what is already there.

Client and server talk a stable protocol: an older server keeps working with a newer client
(**Test connection** then says the server reports no details), so they can be updated separately.

## 4. Following upstream Handy

Handy Remote tracks [upstream Handy](https://github.com/cjpais/Handy). New upstream releases are merged
into a review branch by a manually started GitHub workflow. It opens a checked pull request but
never merges or publishes automatically. Trial feedback and explicit approval are required before
stable promotion. Releases are
versioned `vX.Y.Z-remote.N`: `X.Y.Z` is the upstream Handy version they are based on, `N` counts the
fork's releases on top of it.

## 5. For maintainers: workflows and secrets

| Workflow             | Runs                            | Does                                                                                                                                                          |
| -------------------- | ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `remote-ci.yml`      | pull requests, pushes to `main` | upstream lint and Rust tests, server and feedback tool tests (no models), Markdown links (`tools/ci/check_links.py`), scrub check (`tools/ci/scrub_check.py`) |
| `remote-release.yml` | by hand only                    | first runs CI on the selected ref, then builds, signs, audits and publishes a **prerelease only**, never latest                                               |
| `upstream-sync.yml`  | by hand only                    | prepares `sync/<tag>` and a checked pull request; no automatic merge or release; a conflict opens an issue labelled `upstream-conflict`                       |

Repository secrets (**Settings → Secrets and variables → Actions**):

| Secret                               | Needed by                   | Content                                                                                                                                                                                      |
| ------------------------------------ | --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `TAURI_SIGNING_PRIVATE_KEY`          | release                     | the minisign private key (`tauri signer generate`); its public key is `plugins.updater.pubkey` in `src-tauri/tauri.conf.json`                                                                |
| `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` | release                     | that key's password                                                                                                                                                                          |
| `SYNC_TOKEN`                         | upstream sync (recommended) | fine-grained token for this repository only: Contents, Pull requests and Workflows read/write. Without it, upstream releases that change `.github/workflows/` cannot be merged automatically |
| `SCRUB_PATTERNS`                     | CI (optional)               | regular expressions, one per line, for private names and hosts that must never be committed. The matches are reported by file and line only                                                  |

A trial release: **Actions → remote release → Run workflow**, version `X.Y.Z-remote.N`
(`X.Y.Z` from `.upstream-version`) and the reviewed full commit SHA. The workflow can only create
a prerelease; installed copies only follow the latest full release. Verify downloaded checksums,
the installer signature and tamper rejection, then install the trial explicitly.

**Stable promotion is a separate manual action, after the user has tried and approved that exact
build.** Promote the existing verified assets; do not rebuild or publish a succession of trial
fixes as stable releases. Do not enable unattended installation without the user's approval.

Keep an offline copy of the signing key: without it, installed copies can no longer be updated.

### Local validation

Run `bun run build`, `bun run lint`, `bun run format:check`, `bun run check:translations`,
the Python suites in `server/tests`, `tools/feedback/tests` and `tools/ci/tests`, and `cargo test`
plus `cargo check --all-targets` in `src-tauri`.

The debug app exports IPC bindings automatically; do not hand-edit them.
Windows production builds require the Vulkan SDK, as described in [BUILD.md](../../BUILD.md);
machine-local build overrides must never be committed.

Before publication, the workflow runs [`Test-ReleaseAssets.ps1`](../../tools/ci/Test-ReleaseAssets.ps1):
every download must match the checksum manifest, updater metadata must name the same version,
URL and signature, the installer must verify with the app's public key, and changed bytes must be
rejected. Run the same script against a downloaded release for independent verification.
