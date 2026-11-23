# 听写反馈

某次听写结果不对时，几秒钟就能报告。分析器会提出安全的修改建议（热词、替换规则、流式参数）；由你决定接受或拒绝；
接受的修改写入服务器词表，并通过重新转写录音来验证效果。工具位于 [`tools/feedback`](../../../tools/feedback/README.md)。

## 要求

- Windows，Python 3.10 以上且带 tkinter（python.org 安装包自带）。读取 `feedback.toml` 需要 Python 3.11 以上（或安装 `tomli`）。
  装了 numpy 时会使用它。
- Handy Remote 已开启历史记录并保留录音。
- 使用本仓库的服务器（它支持 `AuditQuery`、`LexiconGet` 和 `LexiconPut` 请求）。工具从不在服务器上开 shell：它使用客户端自己的连接设置
  （`%APPDATA%\com.musingzone.handy-remote\remote_connection.json`）走同一个协议，所以受限的客户端密钥就够用。
- 一个分析器后端（见下文），或设为 `off`。

## 安装

把 `tools\feedback` 复制到固定位置，例如 `C:\handy-remote\tools\feedback`，然后：

```powershell
cd C:\handy-remote\tools\feedback
py -3 voice_feedback.py status    # 设置、连接、报告情况
py -3 voice_feedback.py pending   # 告诉 Handy Remote 托盘工具在哪里
```

之后 Handy Remote 托盘菜单中会出现 **反馈上一次识别…**、**查看改进建议** 和 **听写反馈使用说明**。想快速报告，可以给
`handy --voice-feedback report`（Handy Remote 0.10.1-remote.5 及以上；在远程桌面窗口中会在远端电脑上打开报告，见
[RDP](../RDP.md)）或 `pyw -3 C:\handy-remote\tools\feedback\voice_feedback.py report` 绑定一个自选的快捷键。

可选设置：把 `feedback.example.toml` 复制为 `%APPDATA%\handy-remote\feedback.toml` 后编辑（报告文件夹、界面语言
`auto`/`en`/`zh-CN`、连接覆盖、分析器）。

## 五个步骤

| 步骤               | 谁 / 何时       | 发生什么                                                                                                                                                                                                       |
| ------------------ | --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. **报告**        | 你，马上        | 快捷键或托盘 **反馈上一次识别…**。最新一次听写已预选；改正错误的部分后按 **Ctrl+Enter**。可选择用改正后的文字替换刚上屏的文字（Ctrl+Z 可撤销）。                                                               |
| 2. **分析**        | 后台，约 1 分钟 | 托盘图标出现**蓝点**。你的改正、服务器对这次听写的记录和当前词表交给分析器；它判断问题类型，只提出安全的修改。替换规则会先在你的历史记录上试跑，让你看到会影响多少条过去的听写。需要改代码的想法记入待办清单。 |
| 3. **托盘圆点**    | 分析完成时      | 圆点变成**橙色**，一直保留到你查看为止。不会自动弹窗。                                                                                                                                                         |
| 4. **审阅**        | 你，方便的时候  | 点击托盘图标。每条建议都附有理由，有把握的已预先勾选。选择 **应用所选**、**全部拒绝** 或 **稍后再看**。                                                                                                        |
| 5. **应用 + 验证** | 后台，几秒钟    | 接受的修改写入服务器的 `lexicon.json`（旧版本在服务器和本地都有备份），下一次听写立即生效，无需重启。被报告的录音会重新转写并与你的改正比较。变好或不变：不打扰你。变差或验证失败：橙点再次出现并附带说明。    |

托盘图标状态：无圆点 = 没有待处理事项；蓝点 = 正在分析报告；橙点 = 有建议或通知等你查看。

## 分析器后端

在 `feedback.toml` 中设置 `[analyzer] backend`。

| 后端              | 运行方式                                                                                                                                                                                     |
| ----------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `copilot`（默认） | GitHub Copilot CLI（PATH 中的 `copilot` 或 `analyzer.copilot.path`），无界面运行，禁用 shell 和写文件工具，按 [`ANALYZE.md`](../../../tools/feedback/ANALYZE.md) 分析                        |
| `openai`          | 任意兼容 OpenAI 的 `/chat/completions` 接口（`base_url`、`model`）。API 密钥来自 `api_key_env` 指定的环境变量，或 `api_key_credential` 指定的 Windows 凭据管理器通用凭据；从不写在配置文件里 |
| `off`             | 只保存报告，不做分析也不提建议                                                                                                                                                               |

示例：使用兼容 OpenAI 的接口，密钥存放在凭据管理器中：

```powershell
cmdkey /generic:handy-remote-feedback /user:api /pass
```

```toml
[analyzer]
backend = "openai"

[analyzer.openai]
base_url = "https://api.openai.com/v1"
model = "gpt-4o-mini"
api_key_credential = "handy-remote-feedback"
```

## 发送给分析器的内容

只有文字：识别结果和改正后的文字、你的标签和备注、服务器的逐段记录（草稿、原始和最终文字、时长、停顿、延迟）、当前词表、
Handy 的自定义词以及最近的修改记录。音频只留在你的电脑和服务器上，仅用于重新转写验证。设为 `backend = "off"` 时，任何内容都不会离开你的机器。

## 值得报告的问题

- 反复识别错的人名、产品名和技术术语（热词、替换规则）。
- 某个词总是被同音词替代。
- 明显错误的标点（多余的逗号、缺少断句）。
- 漏字吞字、延迟高（服务器记录有助于定位原因）。

每次报告一句话、只改错误的部分，分析效果最好。

## 数据

| 内容                             | 位置                                                                |
| -------------------------------- | ------------------------------------------------------------------- |
| 报告（录音、改正、分析、决定）   | `%LOCALAPPDATA%\handy-remote\feedback\reports\`（或 `reports_dir`） |
| 修改记录、待办清单、本地词表备份 | 同一文件夹中的 `changelog.jsonl`、`backlog.md`、`lexicon-history\`  |
| 日志、通知、托盘状态             | `%LOCALAPPDATA%\handy-remote`（`pending.json`）                     |
| 服务器端词表历史                 | `<服务器主目录>\config\lexicon-history\`（最近 50 个）              |

每台电脑只处理自己提交的报告，因此 `reports_dir` 可以是共享文件夹。
