# 服务器安装

识别服务器运行在 Windows 电脑上，通过 OpenVINO 使用 CPU 或受支持的 Intel GPU；Intel NPU 是可选加速设备（[../HARDWARE.md](../HARDWARE.md)）。
它只监听 `127.0.0.1`，客户端通过 SSH 访问。

以下命令假设服务器代码放在 `C:\handy-remote\server`，服务器主目录为默认的
`%LOCALAPPDATA%\handy-remote-server`。如果选了别的位置，请相应修改路径。

## 1. 前提条件

- 从 [python.org](https://www.python.org/downloads/) 安装 Python 3.12 或 3.13（含 `py` 启动器）。
- 使用 Intel GPU/NPU 加速时，安装对应的最新驱动。
- OpenSSH Server。在**管理员** PowerShell 中：

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Set-Service sshd -StartupType Automatic
Start-Service sshd
```

安装该功能通常会自动添加 TCP 22 的入站防火墙规则。不需要开放其他端口：识别服务器本身从网络上永远无法直接访问。

## 2. 获取服务器代码

克隆本仓库，或从[最新版本](https://github.com/MusingZone/handy-remote/releases/latest)下载
**Source code (zip)**，把其中的 `server` 文件夹复制到一个固定位置，例如 `C:\handy-remote\server`。
后台任务会从这个文件夹运行脚本。

## 3. 运行安装脚本

```powershell
powershell -ExecutionPolicy RemoteSigned -File C:\handy-remote\server\scripts\setup.ps1 -InstallTask
```

脚本会：

1. 在 `<home>\.venv` 创建 Python 虚拟环境并安装 `requirements.txt`（OpenVINO、sherpa-onnx、tokenizers、numpy）；
2. 把模型下载到 `<home>\models` 并校验（大文件校验 SHA-256，中断的下载可续传）；
3. 运行 `doctor.py --write`：检查本机，按检测到的 GPU/NPU 写出 `<home>\config\config.toml`，并生成初始
   `lexicon.json`；
4. 带 `-InstallTask` 时，注册并启动后台任务 `HandyRemote-Server`。

可以放心重复运行：已完成的步骤会跳过或重新校验。

| 选项                | 含义                                                                                     |
| ------------------- | ---------------------------------------------------------------------------------------- |
| `-HomeDir <dir>`    | 服务器主目录（默认 `$env:HANDY_REMOTE_HOME`，否则 `%LOCALAPPDATA%\handy-remote-server`） |
| `-Source hf-mirror` | huggingface.co 无法访问时改从 hf-mirror.com 下载                                         |
| `-SkipNpu`          | 不下载 NPU 模型（没有 Intel NPU 的机器）                                                 |
| `-InstallTask`      | 注册后台任务                                                                             |
| `-AtStartup`        | 与 `-InstallTask` 一起用：开机即启动，无需用户登录（需管理员权限）                       |

下载遵循标准的 `HTTPS_PROXY` / `NO_PROXY` 环境变量。

### 下载的模型

| 配置项          | 模型                                 | 来源                                                                                                                                  |
| --------------- | ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------- |
| `final_model`   | Qwen3-ASR 1.7B，OpenVINO int8        | [dseditor/Qwen3-ASR-1.7B-INT8_OpenVINO](https://huggingface.co/dseditor/Qwen3-ASR-1.7B-INT8_OpenVINO)                                 |
| `draft_model`   | Qwen3-ASR 0.6B，NPU 用 OpenVINO int8 | [iamwaqargulzar/handy-qwen3-asr-0.6b-openvino-npu-int8](https://huggingface.co/iamwaqargulzar/handy-qwen3-asr-0.6b-openvino-npu-int8) |
| `vad_model`     | Silero VAD                           | [sherpa-onnx releases](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models)                                                 |
| `preview_model` | 流式 Paraformer，中英双语            | [sherpa-onnx releases](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models)                                                 |
| `punct_model`   | CT-Transformer 标点模型，中英，int8  | [sherpa-onnx releases](https://github.com/k2-fsa/sherpa-onnx/releases/tag/punctuation-models)                                         |

许可证见 [THIRD-PARTY-NOTICES.md](../../../THIRD-PARTY-NOTICES.md)。

## 4. 检查本机

```powershell
$py = "$env:LOCALAPPDATA\handy-remote-server\.venv\Scripts\python.exe"
& $py C:\handy-remote\server\doctor.py
```

`doctor.py` 会报告 Python、依赖包、OpenVINO 设备、内存、磁盘、模型文件、OpenSSH 服务器状态以及服务器端口是否被占用，
最后输出 `result: ready` 或问题数量。加 `--write` 可（重新）生成 `config.toml`（已有文件会保留，除非再加 `--force`）。

等待服务器报告就绪：

```powershell
Select-String "$env:LOCALAPPDATA\handy-remote-server\logs\server.log" -Pattern 'engine ready' | Select-Object -Last 1
```

## 5. 与客户端配对

输出配对码：

```powershell
& $py C:\handy-remote\server\doctor.py pair
```

输出是一行 `handy-remote-pair:…`。其中只包含主机名、用户名、SSH 端口、服务器端口、光标助手端口、设备 ID、显示名称和服务器的
SSH 公钥（主机密钥）；脚本也会打印解码后的内容。把它粘贴到客户端（[CLIENT-SETUP.md](CLIENT-SETUP.md)）。
如果客户端解析不了这台电脑的名字，就在客户端的 **Server** 字段里换成它能访问的地址。

## 6. 授权客户端密钥

在客户端点 **Settings → Remote → Create key**，再点 **Copy command**，会复制类似
`python doctor.py authorize "ssh-ed25519 AAAA… handy-remote@laptop"` 的一行。在服务器上用虚拟环境里的 Python 运行：

```powershell
cd C:\handy-remote\server
& $py doctor.py authorize "ssh-ed25519 AAAA... handy-remote@laptop"
```

密钥会带以下选项写入：
`restrict,port-forwarding,permitopen="127.0.0.1:47821",permitopen="127.0.0.1:47823"`。它只能打开服务器端口和光标助手端口，
不能开 shell、不能传文件、不能做其他转发。

**管理员账户：** Windows OpenSSH 只从 `%ProgramData%\ssh\administrators_authorized_keys` 读取管理员的密钥。
请在管理员 PowerShell 中运行上述命令。如果 `doctor.py` 写不进该文件，它会打印需要追加的那一行；之后把文件权限限制为只有
Administrators 和 SYSTEM 可读：

```powershell
icacls "$env:ProgramData\ssh\administrators_authorized_keys" /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F"
```

## 7. 日常运维

| 操作                      | `C:\handy-remote\server\scripts` 中的脚本                     |
| ------------------------- | ------------------------------------------------------------- |
| 修改 `config.toml` 后重启 | `restart_server.ps1`（停止服务器，等待 `engine ready`）       |
| 修改热词 / 替换规则       | 不需要脚本：编辑 `<home>\config\lexicon.json`，会自动重新加载 |
| 不通过任务运行            | `run_server.ps1`（守护进程；输出写入 `<home>\logs`）          |
| 重新注册任务              | `install_task.ps1`（在管理员 PowerShell 中可加 `-AtStartup`） |
| 删除后台任务              | `install_task.ps1 -Uninstall`                                 |

运行脚本的方式（默认执行策略会阻止 `.ps1` 文件）：

```powershell
powershell -ExecutionPolicy RemoteSigned -File C:\handy-remote\server\scripts\restart_server.ps1
```

任务 `HandyRemote-Server` 启动 `run_server.ps1`：这是一个守护进程，服务器崩溃后会退避重启；如果已有另一个守护进程在运行则直接退出。
任务每 5 分钟重复一次，守护进程意外停止时会被拉起。不加 `-AtStartup` 时，任务在当前用户登录时运行（不需要管理员权限）。

想在控制台里直接看服务器输出时，先暂停任务并结束它的服务器进程：

```powershell
Disable-ScheduledTask -TaskName HandyRemote-Server | Stop-ScheduledTask
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object CommandLine -like '*asr_server.py*' | ForEach-Object { Stop-Process -Id $_.ProcessId }
& $py C:\handy-remote\server\asr_server.py
# 结束后（先按 Ctrl+C）：
Enable-ScheduledTask -TaskName HandyRemote-Server | Start-ScheduledTask
```

## 服务器主目录结构

| 路径                             | 内容                                                          |
| -------------------------------- | ------------------------------------------------------------- |
| `<home>\.venv`                   | Python 环境                                                   |
| `<home>\models`                  | 模型文件                                                      |
| `<home>\ov_cache`                | OpenVINO 编译模型缓存                                         |
| `<home>\config\config.toml`      | 服务器配置（[../CONFIGURATION.md](../CONFIGURATION.md)）      |
| `<home>\config\lexicon.json`     | 热词、替换规则、流式参数                                      |
| `<home>\config\lexicon-history\` | 反馈工具替换掉的最近 50 个词表版本                            |
| `<home>\config\warmup.wav`       | 你的第一段 3–8 秒听写，仅保存在本机用于预热                   |
| `<home>\logs\`                   | `server.log`、`supervisor.log`、审计日志 `asr-YYYYMMDD.jsonl` |

## 运行服务器测试

测试不需要模型文件：

```powershell
cd C:\handy-remote\server
& $py -m unittest discover -s tests
```
