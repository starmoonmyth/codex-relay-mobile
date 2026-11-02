# Codex Relay Mobile · Codex 手机消息助手

通过 Tailscale，在手机上继续 Mac 或 Windows 上已有的 Codex、Claude Code 或 Grok CLI 聊天。电脑负责执行，手机可以发任务、上传图片、回复结构化提问并接收任务通知。

## 下载

| 程序 | Windows / Mac 文件名 | 打开后看到什么 | 用途与发布状态 |
| --- | --- | --- | --- |
| **[电脑聊天版下载](https://github.com/starmoonmyth/codex-relay-desktop/releases/latest)** | `CodexRelay.exe` / `CodexRelay.app` | 独立聊天窗口，带聊天列表和输入框 | 在电脑上查看和继续已有 Codex 文字聊天；[源码](https://github.com/starmoonmyth/codex-relay-desktop)。 |
| **[手机连接服务下载](https://github.com/starmoonmyth/codex-relay-mobile/releases/latest)** | `CodexRelayMobile.exe` / `CodexRelayMobile.app` | 电脑浏览器打开配对二维码和连接管理页 | 本项目：电脑运行服务，手机通过 Tailscale 网页发送任务、查看回复和接收提醒。 |

想在手机上操作，下载本仓库的 `CodexRelayMobile-*` 发布包；Windows EXE 和 Mac DMG 都安装在**电脑上**。手机访问配对后的网页，iPhone / Android 可将其添加为主屏幕应用（PWA），没有单独的 IPA / APK。

想用独立的电脑聊天窗口，对应的是 `CodexRelay`；本仓库的手机连接服务不会启动那个窗口。两种助手可以分别使用，使用手机连接服务不需要另装电脑聊天版，也不会用手机连接服务覆盖电脑聊天版。

文中“Codex 桌面应用”指需要另行安装的 **OpenAI 官方 Codex**，它负责原始聊天和任务执行，与上述两种社区助手分别对应不同程序。完整对应关系见 [程序与下载对照](docs/apps.md)。

这是独立社区项目，与 OpenAI 或 Anthropic 无隶属关系。Codex 路径需要本机 Codex CLI、聊天数据库和桌面内部 IPC；Claude Code 路径读取本机 JSONL 并调用官方 CLI。两类接口都会随桌面版本变化，兼容性不能覆盖所有版本。

## 功能

- 查看已有 Codex / Claude Code / Grok CLI 聊天、项目与回复，在同一原生会话中继续发送任务。
- 发送文字、多张图片或图文消息；单张图片最多 8 MB，每条消息图片合计最多 32 MB。
- 回复结构化提问中的选项或自定义答案，由持有聊天的桌面窗口即时提交。
- 查看入队、接收、执行中、完成和失败状态，恢复草稿，防止重复提交。
- 任务完成、失败、中断或需要回答时，提供页面提醒和可选 Web Push。
- 手机 PWA、二维码配对、Mac 与 Windows 设备切换。

## 快速开始

1. 从 [Releases](https://github.com/starmoonmyth/codex-relay-mobile/releases) 下载手机连接服务：Windows 选 `CodexRelayMobile-<版本>-Windows-x64.zip`，Mac Apple Silicon 选 `CodexRelayMobile-<版本>-macOS-arm64.dmg`。
2. 电脑和手机安装并连接同一个 [Tailscale](https://tailscale.com/download) 网络，允许使用 Tailscale HTTPS 和 Serve。
3. 在电脑上打开 Codex 桌面应用及目标聊天，确认其自带 CLI 可用。
4. 在电脑上运行：Windows 解压后双击 `CodexRelayMobile.exe`；Mac 将 DMG 中的 `CodexRelayMobile.app` 拖入“应用程序”后打开。
5. 电脑浏览器会打开配对与连接管理页，这是手机连接服务的正常启动界面。用手机扫描二维码，在手机网页中选择聊天、发送任务和查看回复。
6. iPhone 在 Safari 中“添加到主屏幕”，以后从主屏幕图标打开。Android 可安装为 PWA。

程序默认监听 `127.0.0.1:8879`，首次运行会配置私有 Tailscale Serve HTTPS 端口 `8444`。如 Tailscale 提示需要先启用 HTTPS，请完成提示后重新启动助手。本机连接页是 <http://127.0.0.1:8879/connect>。

Mac 发行包使用本地 ad-hoc 签名，未进行 Apple Developer ID 公证；如系统阻止打开，可在确认下载来源后，通过“系统设置 → 隐私与安全性”允许打开。Windows 发行包未做商业代码签名。

公开安装包不附带开机启动项。需要自动运行时，可自行添加到系统登录项；程序支持 `--background` 参数。

## 通知

打开聊天顶部的“通知”，开启页面提醒或系统推送。

iPhone / iPad 系统推送需要 iOS 16.4 及以上，并从主屏幕应用中申请权限。开启后先点“测试本机通知”，再点“发送测试通知”。如果本机通知正常、远程推送一直没有手机回执，可点“重新连接系统推送”重建订阅。

推送服务接受请求与手机收到通知是两个阶段，界面会分别显示回执。通知包含电脑名称、聊天标题与状态；点击后通过 Tailscale 返回聊天。电脑需要保持运行并能访问 Apple、Google、Mozilla 或 Microsoft 的推送服务。

## Grok CLI 接入

电脑先安装并登录 [Grok CLI](https://x.ai/cli)，创建一个聊天。手机侧边栏的“工作台”选择 **Grok**，再选择已有聊天；输入框上方可以选择模型或填写自定义 ID。模型目录来自本机 Grok，认证与端点由 Grok 自己管理。

适配读取 `~/.grok/sessions`，使用官方 `--resume` 和显式 `--model` 继续同一个会话。已有任务执行中时会提示等待，避免同时写入一个聊天。CLI 退出失败或返回了其他会话时会显示执行失败。工具权限沿用 Grok 的本机设置；手机暂不处理 Grok 工具审批，需要在电脑处理。

当前 Grok 接入支持文字续聊、模型选择、回复和执行状态；图片、结构化选项和 Grok 系统推送尚未开放。未安装 Grok CLI 或没有本机会话的电脑，手机会将 Grok 选项标为不可用。这条路径不读取或控制 grok.com 的网页聊天。

## 聊天状态

右上角使用文字、图标和颜色共同区分状态：蓝色旋转图标表示处理中，绿色勾选表示已完成，红色错误图标表示失败，黄色分别表示排队、待回复、待确认或中断。新任务等待接收时优先显示队列状态，连接断开会单独显示，避免沿用旧的完成状态。

## 用量与余额

选择聊天后，点击输入框上方的 **用量与余额**。Codex、Claude Code 和 Grok 显示本聊天累计 Token、缓存读取及最近一次调用的用量；Grok 的最近用量按任务轮次展示。Codex 还会展示聊天记录中已有的账号额度快照及重置时间。缺少用量时显示未提供，过期快照会明确标注。

服务商支持 `/v1/usage` 时，可显示钱包余额、API Key 可用额度、周期限额及今日实际扣费。服务端从当前 Codex 配置或 Claude Code 环境设置读取 API Key；Codex 使用登录模式时，也会尝试读取 CC Switch 中服务地址一致的当前配置。此时会明确标注其 API Key 可能与聊天登录账号不同。仅查看 Grok 本地用量，不查询 Grok 订阅余额。

余额最多每分钟查询一次，API Key 留在电脑上；查询失败不会当作余额为零。Token 统计不是账单，不根据模型价格推算余额。仅提供原生订阅登录、没有兼容余额接口的服务仍可查看本地聊天用量。

## 配置与多设备

配置保存在本机数据目录的 `config.json`：

| 系统 | 数据目录 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\CodexRelay\Mobile` |
| Mac | `~/Library/Application Support/CodexRelay/Mobile` |

首次启动自动生成这台电脑的私网地址。开源版本的 `source/peers.json` 默认为空，不包含预设设备。要在下拉菜单切换两台电脑，可停止助手后，在各自 `config.json` 中设置 `peers`：

```json
{
  "peers": [
    {"label": "Mac", "origin": "https://your-mac.your-tailnet.ts.net:8444"},
    {"label": "Windows", "origin": "https://your-pc.your-tailnet.ts.net:8444"}
  ]
}
```

此片段只替换 `peers` 字段，其余自动生成的配置应保留。两台电脑分别扫描配对、分别开启通知。可选 `codex_home` 和 `cli` 字段用于指定非默认 Codex 数据目录或 CLI 路径。电脑连接页和手机侧边栏的“工作台”都可以切换 `Codex`、`Claude Code` 、`Grok` 或全部工作台。

Claude Code 适配读取 `~/.claude/projects`（也可设置 `claude_home`），按 JSONL 会话 ID 调用本机 Claude Code CLI 的 `--resume` 继续原会话。Windows 会自动查找 VS Code Claude Code 扩展内置的 `claude.exe`，Mac 可设置 `claude_cli` 指向已安装的 CLI。Claude Code 的图片会保存为助手附件目录，并在任务中提供给 Claude Code 读取；这条路径不伪造 Claude Desktop 的内部 Electron IPC。

Claude 适配使用官方 CLI 的同一会话恢复能力。手机可以读取 CLI 写入的会话记录；Claude Desktop 窗口的更新和同步由 Claude Desktop 管理，当前适配不保证窗口实时显示 CLI 写入的消息。

在 Claude 聊天输入框上方点击“发送模型”，可按聊天选择模型或填写中转服务支持的完整模型 ID。每次发送明确指定 `--model`，与 Claude Desktop 的模型选择分别设置。发送消息保留请求模型，回复标注接口报告的模型 ID；没有模型记录时会要求先选择。详见 [模型设置](docs/configuration.md#claude-使用哪个模型手机怎么切换)。

更多信息见 [配置与排障](docs/configuration.md)。

## 从源码运行

下面的 `source/launcher.py` 启动手机连接服务和电脑配对页；独立电脑聊天版的源码不在本仓库中。

需要 Python 3.13、Tailscale，以及要使用的 Codex 桌面应用或 Claude Code CLI。命令均在仓库根目录执行。

Windows PowerShell：

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r source/requirements.txt
.venv\Scripts\python source/launcher.py
```

Mac：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r source/requirements.txt
.venv/bin/python source/launcher.py
```

## 测试和构建

在已安装依赖的虚拟环境中执行：

```sh
python -m unittest discover -s source -p "test_*.py" -v
python source/build.py --destination dist --work-dir build
```

测试使用临时数据目录和模拟投递，不向真实聊天发送消息。需要在目标系统上构建：Windows 生成 EXE，Mac 生成与构建机器架构一致的 `.app`。预构建 Mac 包为 Apple Silicon ARM64。

## 工作方式和限制

服务只读查询 Codex 聊天数据库，通过原生 CLI 队列投递普通消息，通过独立 App Server 投递图片，通过桌面聊天持有窗口的 IPC 提交提问回复。助手自己的配对、上传、通知和回执保存在独立数据目录。

Codex 桌面应用需保持运行；提问回复需要桌面已打开对应聊天。普通消息入队后，仍由原聊天的模型、认证、配额、网络与执行权限决定能否继续执行。这个工具不提供模型服务，也不改变服务商额度。

输入框上方可切换聊天模型。Codex 会通过原聊天的桌面窗口设置下一轮模型并读回确认；需要在电脑打开目标聊天。模型列表来自本机 Codex，也可填写自定义 ID。队列在执行时使用聊天模型，电脑端后续修改同样会影响排队任务。

当前不支持新建聊天、上传非图片文件或在手机处理命令执行审批。出现“不确定”回执时应先检查原聊天，避免重复发送。

## 隐私与安全

配对二维码和链接相当于这台电脑的控制凭证，请只交给自己信任的设备。配对会话有效期为 30 天，可在手机退出配对。程序使用 Tailscale 私网 HTTPS，不配置公网 Funnel，也不读取或复制模型 API 密钥。

不要提交本机 `config.json`、`mobile.sqlite`、`pairing-token`、Web Push 私钥、上传图片或聊天历史。安全说明见 [SECURITY.md](SECURITY.md)。

## 许可证

原创代码使用 [MIT License](LICENSE)。第三方库保留各自许可证，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 与 [`source/licenses`](source/licenses)。OpenAI Codex 和 Tailscale 需单独安装，不包含在本项目发行包中。
