# Handy Remote 文档

Handy Remote 是 [Handy](https://github.com/cjpais/Handy) 的分支版本：语音识别在另一台 Windows 电脑（下称
_服务器_）上运行，通过 SSH 连接；说话时浮窗实时显示预览，整句识别完成后逐句上屏，浮窗跟随文字光标。

English: [../README.md](../README.md)

## 文档目录

| 文档                                                                   | 内容                                                     |
| ---------------------------------------------------------------------- | -------------------------------------------------------- |
| [SERVER-SETUP.md](SERVER-SETUP.md)                                     | 在 Windows 电脑上安装识别服务器                          |
| [CLIENT-SETUP.md](CLIENT-SETUP.md)                                     | 安装 Handy Remote 并连接服务器                           |
| [FEEDBACK.md](FEEDBACK.md)                                             | 报告识别错误、调优词表                                   |
| [CLOUD-AND-LLM.md](CLOUD-AND-LLM.md)                                   | 云端语音识别 API、上屏前的大模型润色与结构化             |
| [../ARCHITECTURE.md](../ARCHITECTURE.md)                               | 客户端、SSH 中转、服务器和辅助工具如何配合（英文）       |
| [../HARDWARE.md](../HARDWARE.md)                                       | 服务器硬件要求（GPU、NPU、内存、磁盘）与实测性能（英文） |
| [../CONFIGURATION.md](../CONFIGURATION.md)                             | 全部服务器配置项、词表参数、客户端设置和环境变量（英文） |
| [../RDP.md](../RDP.md)                                                 | 在远程桌面里听写：光标助手（英文）                       |
| [../TROUBLESHOOTING.md](../TROUBLESHOOTING.md)                         | 日志、常见日志行、`doctor.py`、连接测试结果（英文）      |
| [../UPDATING.md](../UPDATING.md)                                       | 应用内更新、无人值守更新、服务器更新（英文）             |
| [../../../SECURITY.md](../../../SECURITY.md)                           | 安全模型与漏洞报告方式（英文）                           |
| [../../../CHANGES-FROM-UPSTREAM.md](../../../CHANGES-FROM-UPSTREAM.md) | 与上游 Handy 的区别（英文）                              |

## 五步完成典型配置

1. 在服务器上：安装 OpenSSH Server，运行 `server\scripts\setup.ps1 -InstallTask`
   （[SERVER-SETUP.md](SERVER-SETUP.md)）。
2. 在服务器上：`python doctor.py pair` 输出一行配对码。
3. 在笔记本上：安装 Handy Remote，打开 **Settings → Remote**，粘贴配对码并点 **Apply**
   （[CLIENT-SETUP.md](CLIENT-SETUP.md)）。
4. 点 **Create key**，复制 `doctor.py authorize` 命令，到服务器上运行。
5. 点 **Test connection**，再点 **Save**。之后照常听写即可。

界面语言为中文时，按钮和标签显示为对应的中文译文；本文使用英文原名以便对照。
