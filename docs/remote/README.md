# Handy Remote documentation

Handy Remote is a fork of [Handy](https://github.com/cjpais/Handy) that runs speech recognition on
another Windows PC (the _server_) reached over SSH, with live previews, sentence-by-sentence paste and
an overlay that follows the text caret.

中文版：[zh-CN/README.md](zh-CN/README.md)

## Start here

| Document                                                         | What it covers                                                 |
| ---------------------------------------------------------------- | -------------------------------------------------------------- |
| [ARCHITECTURE.md](ARCHITECTURE.md)                               | How the client, SSH relay, server and helpers fit together     |
| [HARDWARE.md](HARDWARE.md)                                       | What the server needs (GPU, NPU, memory, disk) and performance |
| [SERVER-SETUP.md](SERVER-SETUP.md)                               | Installing the recognition server on a Windows PC              |
| [CLIENT-SETUP.md](CLIENT-SETUP.md)                               | Installing Handy Remote and connecting it to the server        |
| [CONFIGURATION.md](CONFIGURATION.md)                             | Every server key, lexicon option, client setting and env var   |
| [RDP.md](RDP.md)                                                 | Caret helper for dictating inside Remote Desktop               |
| [CLOUD-AND-LLM.md](CLOUD-AND-LLM.md)                             | Cloud speech APIs and LLM polish/structure before paste        |
| [FEEDBACK.md](FEEDBACK.md)                                       | Reporting wrong dictations and tuning the lexicon              |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md)                         | Logs, typical log lines, `doctor.py`, connection test results  |
| [UPDATING.md](UPDATING.md)                                       | In-app updater, unattended updater, server updates             |
| [../../SECURITY.md](../../SECURITY.md)                           | Security model and how to report vulnerabilities               |
| [../../CHANGES-FROM-UPSTREAM.md](../../CHANGES-FROM-UPSTREAM.md) | What differs from upstream Handy                               |

## Typical setup in five steps

1. On the server PC: install OpenSSH Server and run `server\scripts\setup.ps1 -InstallTask`
   ([SERVER-SETUP.md](SERVER-SETUP.md)).
2. On the server: `python doctor.py pair` prints a pairing code.
3. On your laptop: install Handy Remote, open **Settings → Remote**, paste the code and press **Apply**
   ([CLIENT-SETUP.md](CLIENT-SETUP.md)).
4. Press **Create key**, copy the `doctor.py authorize` command and run it on the server.
5. Press **Test connection**, then **Save**. Dictate as usual.
