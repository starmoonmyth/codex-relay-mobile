# Codex Relay Mobile · Codex 手机消息助手

通过 Tailscale，在手机上访问 Mac 或 Windows 上已有的 Codex、Claude Code CLI、Claude Desktop 或 Grok CLI 聊天。电脑负责执行；可写会话支持发送任务，具体图片、选项与通知能力取决于工作台。

## 发布渠道与已知问题

**最新稳定版为 [1.3.7](https://github.com/starmoonmyth/codex-relay-mobile/releases/tag/v1.3.7)。当前源码对应 [1.3.8-rc.2 预览版](https://github.com/starmoonmyth/codex-relay-mobile/releases/tag/v1.3.8-rc.2)，它累计包含 1.3.7 之后的改动，不会替换稳定版默认下载。** 本地调试编号与公开版本独立；公开安装包的文件名、程序与通知组件使用相同的公开版本，详见 [版本与发布规则](docs/releasing.md)。

**预览验证范围：** 已修订 Grok 停止状态、Claude 桌面历史和压缩后的消息展示；这些场景仍需手机验收。Claude Desktop 新增原生窗口文字代发，Windows 已验证隔离聊天，Mac 的辅助功能代发尚未实机验收。Grok Bot 桌面记录仍为只读。请先阅读 [已知问题与接入边界](KNOWN_ISSUES.md)，不要把预览版理解为完整桌面适配。

## 下载

| 程序 | Windows / Mac 文件名 | 打开后看到什么 | 用途与发布状态 |
| --- | --- | --- | --- |
| **[电脑聊天版下载](https://github.com/starmoonmyth/codex-relay-desktop/releases/latest)** | `CodexRelay.exe` / `CodexRelay.app` | 独立聊天窗口，带聊天列表和输入框 | 在电脑上查看和继续已有 Codex 文字聊天；[源码](https://github.com/starmoonmyth/codex-relay-desktop)。 |
| **[手机连接服务下载](https://github.com/starmoonmyth/codex-relay-mobile/releases/latest)** | `CodexRelayMobile.exe` / `CodexRelayMobile.app` | 电脑浏览器打开配对二维码和连接管理页 | 本项目：电脑运行服务，手机通过 Tailscale 网页发送任务、查看回复和接收提醒。 |

想在手机上操作，下载本仓库的 `CodexRelayMobile-*` 发布包；Windows EXE 和 Mac DMG 都安装在**电脑上**。手机访问配对后的网页，iPhone / Android 可将其添加为主屏幕应用（PWA），没有单独的 IPA / APK。

想用独立的电脑聊天窗口，对应的是 `CodexRelay`；本仓库的手机连接服务不会启动那个窗口。两种助手可以分别使用，使用手机连接服务不需要另装电脑聊天版，也不会用手机连接服务覆盖电脑聊天版。

文中“Codex 桌面应用”指需要另行安装的 **OpenAI 官方 Codex**，它负责原始聊天和任务执行，与上述两种社区助手分别对应不同程序。完整对应关系见 [程序与下载对照](docs/apps.md)。

这是独立社区项目，与 OpenAI 或 Anthropic 无隶属关系。Codex 路径需要本机 Codex CLI、聊天数据库和桌面内部 IPC；Claude Code 路径读取本机 JSONL 并调用官方 CLI。Claude Desktop 的文字代发使用系统辅助功能操作原生窗口。这些接口都会随桌面版本变化，兼容性不能覆盖所有版本。

## 功能

- 查看 Codex、Claude Code CLI、Claude Desktop 与 Grok CLI 聊天；可写 CLI 会话支持续聊，Claude Desktop 可通过原生窗口代发文字，Grok Bot 桌面缓存只读。
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

适配读取 `~/.grok/sessions`，使用官方 `--resume` 和显式 `--model` 继续同一个 CLI 会话。Grok Bot 桌面记录会单独标为“Grok Bot 桌面”，按桌面本地缓存读取；它和 Grok CLI 是两套会话，桌面记录只读，手机不会把任务转发到另一套会话。已有任务执行中时会提示等待，避免同时写入一个聊天。CLI 退出失败或返回了其他会话时会显示执行失败。工具权限沿用 Grok 的本机设置；手机暂不处理 Grok 工具审批，需要在电脑处理。

当前 Grok CLI 接入支持文字续聊、模型选择和回复；执行状态仍有 [KI-001](KNOWN_ISSUES.md#待验收问题)，图片与结构化选项尚未开放；Grok CLI 已接入状态通知。可用性依据本机 CLI 会话或桌面缓存判断。这条路径不读取或控制 grok.com 的网页聊天。

## 聊天状态

右上角使用文字、图标和颜色共同区分状态：蓝色旋转图标表示处理中，绿色勾选表示已完成，红色错误图标表示失败，黄色分别表示排队、待回复、待确认或中断。新任务等待接收时优先显示队列状态，连接断开会单独显示，避免沿用旧的完成状态。

## 聊天列表状态

列表会显示旋转图标和“处理中”，并区分排队、待回复、失败和中断。任务完成后，尚未查看最终回复的聊天显示蓝绿色圆点和 **完成 · 未查看**。打开聊天并滚动到最终回复后，标记自动清除；停留在历史位置、打开列表或切到后台时不会清除。

已查看记录保存在当前浏览器，刷新和重新打开仍然保留。首次升级时以现有历史作为已查看基线；此后完成的任务会显示未查看。该标记不会修改原生聊天记录，也不与电脑原生应用或其他手机同步。

## 用量与余额

选择聊天后，点击输入框上方的 **用量与余额**，可以查看三类数据：

- **套餐用量**：读取当前原生 CLI 登录账号的周期已用比例、剩余比例和重置时间；接口有提供时展示额外额度或预付余额。Codex 通过 `account/rateLimits/read`，Claude 通过 OAuth usage 接口，Grok 通过 `_x.ai/billing` 查询。缺少接口或原生登录时明确显示不可用。
- **服务商余额 / 额度**：服务商支持兼容的 `/v1/usage` 时，展示中转站钱包余额、API Key 可用额度、周期限额和今日实际扣费。与原生套餐分别计算。
- **本聊天用量**：显示 Codex、Claude Code 和 Grok 记录的累计 Token、缓存读取以及最近调用或任务用量；实时套餐不可用时可查看已有额度快照及过期提示。

套餐实现参考 [CodexBar](https://github.com/steipete/CodexBar) 的 [Codex](https://github.com/steipete/CodexBar/blob/main/docs/codex.md)、[Claude](https://github.com/steipete/CodexBar/blob/main/docs/claude.md)、[Grok](https://github.com/steipete/CodexBar/blob/main/docs/grok.md) 接口说明，并对照 [CodexBar Windows](https://github.com/Mukund2074/codexbar-windows) 的 OAuth 用量读取方式。它们查询账号套餐接口；本地 Token 统计本身无法得出套餐余额。

原生登录由相应 CLI 管理。Claude 优先读取其配置目录下 `.credentials.json`；Mac 默认目录也支持读取已允许访问的 `Claude Code-credentials` 钥匙串项，查询不会触发钥匙串授权窗口或刷新令牌。自定义 Claude 目录只读取该目录的凭据，不借用默认账号。无法读取时显示未找到可用登录。

中转站 API Key 来自当前 Codex 配置或 Claude Code 环境设置。Codex 使用登录模式时，也会尝试读取 CC Switch 中服务地址一致的当前配置，界面明确提示其 API Key 可能与聊天登录账号不同。

查询结果缓存一分钟；Claude 返回 429 时遵循退避时间。密钥和 OAuth 令牌留在电脑上，手机只接收用量结果。查询失败不会当作余额为零，Token 统计不按模型价格推算套餐余额。

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

此片段只替换 `peers` 字段，其余自动生成的配置应保留。两台电脑分别扫描配对。通知可分别订阅，也可配置统一入口，沿用手机的一个主屏幕图标和已有订阅，见 [多电脑共用通知入口](docs/configuration.md#多电脑共用通知入口)。可选 `codex_home` 和 `cli` 字段用于指定非默认 Codex 数据目录或 CLI 路径。电脑连接页和手机侧边栏的“工作台”都可以切换 `Codex`、`Claude Code` 、`Grok` 或全部工作台。

Claude Code 适配读取 `~/.claude/projects`（也可设置 `claude_home`），按 JSONL 会话 ID 调用本机 Claude Code CLI 的 `--resume` 继续原会话。Windows 会自动查找 VS Code Claude Code 扩展内置的 `claude.exe`，Mac 可设置 `claude_cli` 指向已安装的 CLI。Claude Code 的图片会保存为助手附件目录，并在任务中提供给 Claude Code 读取；这条路径不伪造 Claude Desktop 的内部 Electron IPC。

Claude Code 适配现在按 JSONL 的 `parentUuid` 链读取单一分支，并锁定项目目录、CLI 会话 ID 和当前消息头。Claude Desktop 选择的分支会单独标为“Claude 桌面”；文字通过辅助功能投递到核对过的原生窗口，不交给普通 CLI 另起会话。上下文压缩后的历史沿原生元数据保留的消息链衔接，过滤内部摘要和提示；缺失的原始消息无法凭空恢复。旧回执如果不属于当前分支会显示在“其他分支”区域，不会改变当前聊天状态。CLI 会话发送前会检查记录来源和消息头发生变化，变化时要求刷新，避免串到另一条记录。

在可写的 Claude CLI 聊天输入框上方点击“发送模型”，可按聊天选择模型或填写中转服务支持的完整模型 ID。每次发送明确指定 `--model`。Claude Desktop 跟随原生窗口当前模型，手机不能切换该模型；发送回执显示代发时读取的模型名称。发送消息保留请求模型，回复标注接口报告的模型 ID；没有模型记录时会要求先选择。详见 [模型设置](docs/configuration.md#claude-使用哪个模型手机怎么切换)。

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

Claude Desktop 代发需要电脑登录、解锁并打开原生 Claude，会切换窗口；原聊天已有草稿、身份不明确或消息链变化时停止代发。Mac 还需允许辅助功能与自动化权限，具体边界见 [已知问题](KNOWN_ISSUES.md)。

服务只读查询 Codex 聊天数据库，通过原生 CLI 队列投递普通消息，通过独立 App Server 投递图片，通过桌面聊天持有窗口的 IPC 提交提问回复。助手自己的配对、上传、通知和回执保存在独立数据目录。

Codex 桌面应用需保持运行；提问回复需要桌面已打开对应聊天。普通消息入队后，仍由原聊天的模型、认证、配额、网络与执行权限决定能否继续执行。这个工具不提供模型服务，也不改变服务商额度。

输入框上方可切换聊天模型。Codex 会通过原聊天的桌面窗口设置下一轮模型并读回确认；需要在电脑打开目标聊天。模型列表来自本机 Codex，也可填写自定义 ID。队列在执行时使用聊天模型，电脑端后续修改同样会影响排队任务。

当前不支持新建聊天、上传非图片文件或在手机处理命令执行审批。出现“不确定”回执时应先检查原聊天，避免重复发送。

## 隐私与安全

配对二维码和链接相当于这台电脑的控制凭证，请只交给自己信任的设备。配对会话有效期为 30 天，可在手机退出配对。程序使用 Tailscale 私网 HTTPS，不配置公网 Funnel。用量查询只在电脑端读取所需凭据，不向手机传输 API 密钥或 OAuth 令牌。

不要提交本机 `config.json`、`mobile.sqlite`、`pairing-token`、Web Push 私钥、上传图片或聊天历史。安全说明见 [SECURITY.md](SECURITY.md)。

## 许可证

原创代码使用 [MIT License](LICENSE)。第三方库保留各自许可证，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 与 [`source/licenses`](source/licenses)。OpenAI Codex 和 Tailscale 需单独安装，不包含在本项目发行包中。
