# 客户端安装

## 1. 安装 Handy Remote

从[最新版本](https://github.com/MusingZone/handy-remote/releases/latest)下载 Windows 安装程序并运行。

- **SmartScreen：** 发行版没有代码签名，Windows 可能提示“Windows 已保护你的电脑”；点 **更多信息 → 仍要运行**。
  应用内更新仍会用本项目自己的更新签名校验（[../UPDATING.md](../UPDATING.md)）。
- **与 Handy 并存：** Handy Remote 有独立的应用标识（`com.musingzone.handy-remote`），安装时不会替换上游 Handy。
  两者同时运行时，请给它们设置不同的快捷键。
- **首次运行：** 如果存在上游 Handy 的数据文件夹 `%APPDATA%\com.pais.handy`，Handy Remote 会把其中的设置、历史、录音和模型
  复制到 `%APPDATA%\com.musingzone.handy-remote`（模型文件尽量用硬链接，日志跳过）。上游 Handy 的文件夹不会被移动或修改。
  这只发生一次，且仅在新文件夹还没有设置文件时。

## 2. 检查 SSH 客户端

Windows 10 和 11 自带 OpenSSH 客户端（`C:\Windows\System32\OpenSSH\ssh.exe`），Handy Remote 会自动找到。
如果 **ssh program** 显示 “ssh not found”，在 **设置 → 系统 → 可选功能 → OpenSSH 客户端** 中安装，或在管理员 PowerShell 中：

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0
```

## 3. 连接服务器

先准备好服务器（[SERVER-SETUP.md](SERVER-SETUP.md)），然后打开 **Settings → Remote**：

1. **Pairing code：** 粘贴 `doctor.py pair` 输出的 `handy-remote-pair:…` 一行，点 **Apply**。下面的字段（包括服务器主机密钥）会自动填好。
2. **Server：** 如果从本机解析不了服务器的计算机名，换成能访问的地址或名字（例如 `asr-server.lan`）。
3. **This computer's key → Create key**，再点 **Copy command**，把复制的 `doctor.py authorize "…"` 命令拿到服务器上运行
   （[SERVER-SETUP.md](SERVER-SETUP.md) 第 6 步）。
4. **Test connection。** 预期结果：“Connected in … ms; the server answered in … ms.”，再加上
   “Models are loaded.” 或 “Models are unloaded; they load on the first dictation”。
5. **Save。** 此后识别交给服务器完成，服务器会显示为当前选中的模型。

没有配对码时，手动填写 **Server**、**SSH user**、**SSH port** 和 **Server port**。此时主机密钥在第一次连接时被接受，并保存在你常规的
SSH `known_hosts` 中。

## 页面上的设置

| 设置                                   | 默认值     | 含义                                                                                                                 |
| -------------------------------------- | ---------- | -------------------------------------------------------------------------------------------------------------------- |
| **Use remote server**                  | 关         | 听写发送到服务器，而不是用本机模型                                                                                   |
| **Paste while speaking**               | 开         | 还在录音时就把已完成的句子上屏                                                                                       |
| **Follow the caret in Remote Desktop** | 开         | 浮窗显示在远程桌面窗口内应用的光标下方；需要光标助手（[../RDP.md](../RDP.md)）                                       |
| **Dictate inside Remote Desktop**      | 关         | 远程桌面窗口在前台时，快捷键改为启动远程会话里的 Handy（[../RDP.md](../RDP.md#dictating-with-the-remote-pcs-handy)） |
| **Server**                             | –          | 主机名或地址；`localhost` 表示服务器就在本机（不走 SSH）                                                             |
| **SSH user**                           | –          | 服务器上的账户                                                                                                       |
| **SSH port**                           | 22         | 服务器的 SSH 端口                                                                                                    |
| **Server port**                        | 47821      | 识别服务器在服务器本机上的端口                                                                                       |
| **Caret helper port**                  | 47823      | 服务器上光标助手的端口                                                                                               |
| **SSH key file**                       | 空         | 私钥；为空时使用本应用自己的密钥（如果存在），否则使用你的 SSH 默认设置                                              |
| **ssh program**                        | Automatic  | `ssh.exe` 的路径                                                                                                     |
| **Server key**                         | 来自配对码 | 固定的 SSH 主机密钥；密钥不同的服务器会被拒绝。**Clear** 取消固定                                                    |

如果这次听写会被语言模型改写（按了后处理快捷键，或打开了「每次听写都润色」），或者转写钩子可能修改最终文本，**Paste while speaking** 会自动不生效。

## 使用

- 和 Handy 一样听写。浮窗出现在文字光标下方（Windows），并显示实时预览。
- 麦克风真正送来声音之前，浮窗保持暗色；出现带扩散圆环的麦克风图标后再开始说话。
- 如果服务器之前卸载了模型，浮窗会提示正在唤醒模型。继续说即可：音频会先缓存，模型加载完成（约 12 秒）后再转写，不会丢失。

## 服务器就在本机

把 **Server** 设为 `localhost`。Handy Remote 会通过内置中转直接连接 `127.0.0.1:<Server port>`，不使用 SSH。

## 切回本机识别

关闭 **Use remote server** 并 **Save**，然后在 **Models** 页面选择一个本机模型。

## 客户端环境变量

| 变量                               | 作用                                                                     |
| ---------------------------------- | ------------------------------------------------------------------------ |
| `HANDY_OVERLAY_CARET=0`            | 浮窗固定在屏幕边缘，不跟随光标                                           |
| `HANDY_REMOTE_CARET=0`             | 从不查询光标助手（关闭远程桌面光标跟随）                                 |
| `HANDY_REMOTE_CARET=<host>:<port>` | SSH 服务器转发光标请求的目标地址（默认 `127.0.0.1:<Caret helper port>`） |
| `HANDY_DISABLE_UPDATER`            | 上游开关：关闭更新检查                                                   |

更多内容见 [../CONFIGURATION.md](../CONFIGURATION.md)。遇到问题见 [../TROUBLESHOOTING.md](../TROUBLESHOOTING.md)。
