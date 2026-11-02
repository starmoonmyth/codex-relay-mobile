# 配置与排障

本文针对 **Codex 手机消息助手（`CodexRelayMobile` 手机连接服务）**。独立电脑聊天版叫 `CodexRelay`，不在本仓库的源码和发行包中。文件名、用途与下载关系见 [程序与下载对照](apps.md)。

## 启动配置

首次启动会根据本机 Tailscale 状态生成 `config.json`。默认 HTTP 端口为 `8879`，Tailscale HTTPS 端口为 `8444`。如果 HTTPS 端口已由其他服务使用，程序会提示冲突，不覆盖已有代理。

需要改端口时，先在本机连接页停止助手，再编辑配置，然后重新启动。修改端口后，还需相应调整原有 Tailscale Serve 代理；程序不会清理旧的 Serve 规则。

| 字段 | 用途 |
| --- | --- |
| `origin` | 手机使用的这台电脑 HTTPS 地址，首次启动自动生成 |
| `port` | 本机回环 HTTP 监听端口 |
| `https_port` | 首次配置 Tailscale Serve 时使用的 HTTPS 端口 |
| `label` | 页面和通知中的设备名称 |
| `peers` | 设备切换列表，元素包含 `label` 和 `origin` |
| `codex_home` | 可选，Codex 数据目录，默认 `CODEX_HOME` 或 `~/.codex` |
| `cli` | 可选，本机 Codex CLI 可执行文件的绝对路径 |
| `provider` | `codex`（默认）、`claude`、`grok` 或 `all`；也可在本机连接页切换 |
| `claude_home` | 可选，Claude Code 数据目录，默认 `~/.claude` |
| `claude_cli` | 可选，Claude Code CLI 的绝对路径；Windows 会自动查找 VS Code 扩展内置 CLI |
| `claude_model` | 可选，此电脑助手的默认 Claude 模型；聊天自己的选择优先 |
| `claude_models` | 可选，模型选择框中额外列出的完整模型 ID 数组 |
| `claude_chat_models` | 手机为各个 Claude 聊天保存的模型选择，由界面管理 |
| `claude_permission_mode` | Claude Code CLI 权限模式，默认 `bypassPermissions`；可改为 `acceptEdits` 或 `manual` |

程序会尝试发现应用内置的 Codex CLI。Windows 默认检查 `%LOCALAPPDATA%\OpenAI\Codex\bin` 和 PATH；Mac 检查 `/Applications`、`~/Applications`、Homebrew 与 PATH。其他安装位置可手动配置 `cli`。

Claude Code 会话来自 `claude_home/projects` 下的 JSONL 文件。发送时使用 `claude --resume <session-id> --print`，在后台继续原会话，手机收到的回执以该 JSONL 中的新用户消息和助手消息为准。Claude Code 的图片会放在助手自己的附件目录，并将绝对路径写入任务文本，由 Claude Code 的图片读取能力处理。Claude Desktop 的 `LocalSessions_$_sendMessage` 是内部 Electron IPC，未被外部助手直接调用。

可写适配复用 Claude Code CLI 的会话恢复能力。Claude Desktop 有自己的会话索引、分支、连接器和窗口状态；识别出的桌面会话在手机只读，禁止转交普通 CLI 另起分支。桌面历史读取仍可能为空或不完整，见 [KI-002](../KNOWN_ISSUES.md#待修复)。不要将 CLI 执行成功视为桌面已同步，也不要因手机未显示内容重复发送。

### Claude 使用哪个模型，手机怎么切换

进入可写的 Claude CLI 聊天，点击输入框上方的“发送模型”，选择 Opus、Sonnet、Haiku，或填写服务商支持的完整模型 ID。选择按聊天保存在当前电脑上，下次发送时生效，不会修改电脑 Claude Code 的全局默认模型。

Claude Desktop 聊天是只读的：同一个模型入口仅记录助手偏好，不会同步修改桌面模型，也不代表能够从手机发送。桌面模型请在 Claude Desktop 中设置。

没有保存选择时，优先使用本助手的 `claude_model` 配置，否则沿用会话最近一次回复记录中的模型；没有可用模型记录时必须先选择。每次发送都明确传入 `--model`，不启用自动备用模型参数。重复查询同一条请求保留原请求的模型选择。

发送的消息显示“请求模型”，每条 Claude 回复标注会话记录中的 `message.model`。别名可由 Claude Code 或服务商解析，因此请求名称与回复名称可能不同。通过中转站时，这些名称属于接口报告信息，本助手无法独立核验上游真正运行的模型；响应速度也不能证明模型身份。

`source/peers.json` 仅用于新配置的默认设备列表。修改已运行设备的列表，应编辑数据目录中的 `config.json`。

## Grok 配置

`grok_cli` 可指定 CLI 路径，`grok_home` 可指定会话目录，默认 `GROK_HOME` 或 `~/.grok`。`grok_model` 是此助手的默认模型，`grok_chat_models` 保存各聊天的选择。可选 `grok_permission_mode` 显式传给 Grok；省略时沿用 Grok 的本机权限设置。

此适配只读取 Grok 的会话记录和模型名称，任务交给官方 CLI，不复制登录凭证，也不改写 Grok 聊天文件。后台执行使用独立监督进程记录 CLI 的退出结果，服务停止后已经提交的 Grok 任务仍可继续。图片和手机工具审批尚未开放。

Grok Bot 桌面聊天从独立本地缓存读取，标为只读，不会转交给 Grok CLI。Grok 已停止任务仍可能显示处理中，见 [KI-001](../KNOWN_ISSUES.md#待修复)；以桌面实际状态为准。

## 常见问题

### 双击为什么打开扫码网页，没有电脑聊天窗口

`CodexRelayMobile.exe` / `CodexRelayMobile.app` 是运行在电脑上的手机连接服务，双击打开配对与连接管理页属于正常行为。用手机扫码后，在手机网页中选择聊天并发送任务。

带聊天列表和输入框的独立电脑程序是 `CodexRelay.exe` / `CodexRelay.app`，可从 [电脑聊天版 Releases](https://github.com/starmoonmyth/codex-relay-desktop/releases/latest) 下载。使用手机连接服务只需安装官方 Codex 和本项目的连接服务。

### 手机打不开页面

检查两台设备的 Tailscale 都在线，手机使用的是该电脑的私网 HTTPS 地址。电脑关机、休眠或助手停止后，手机无法读取最新状态。首次启用 Serve 如出现 Tailscale HTTPS 授权提示，完成后重新启动助手。

### Codex 手机模型选择

在聊天输入框上方点“聊天模型”，选择本机 Codex 提供的模型或填写服务商支持的模型 ID。电脑需要打开目标聊天。保存后通过原生桌面接口只修改下一轮模型，并读回确认；进行中的回复不受影响，推理强度和执行权限保持原设置。

发送前会再次核对聊天模型；如果电脑端已修改，手机会提示重新确认。已排队任务使用执行时的聊天模型，之后在电脑修改模型也会影响它们。这里展示请求模型和桌面设置，无法独立验证中转服务实际使用的后台模型。

### 消息显示等待接收或等待回执

等待接收表示消息仍在原生队列中；入队不代表已执行。确认桌面 Codex 正常运行，目标聊天能继续执行，且模型连接正常。回执通过原生消息编号、内容和轮次关联；已进入历史的消息可在后续轮次继续更新。

“投递结果待确认”可能表示请求已经发送，不要立即另发相同任务。保持原文重试可查询同一条请求；先检查桌面聊天和队列状态。

### 手机选项回复没有继续执行

确认 Codex 桌面已经打开对应聊天。新回复通过聊天持有窗口即时提交；旧版已排队的回复可点“立即提交”，确认撤回精确旧队列项后再提交。缺少桌面窗口、内部 IPC 不兼容或结果不确定时，页面会显示原因。

### 收不到通知

1. iPhone 使用 iOS 16.4 或以上，添加到主屏幕后，从主屏幕应用中开启系统推送。
2. 先点“测试本机通知”。若无法显示，检查系统通知权限、专注模式和通知摘要。
3. 再点“发送测试通知”。服务接受请求只说明上游接受；手机接收、系统接受显示和点击通知各有独立回执。
4. 本机通知正常但远程长期无回执时，点“重新连接系统推送”重建订阅，再测试。
5. 确保电脑能访问相应推送服务；手机需要连接 Tailscale 才能返回本项目服务器的回执、打开聊天。回执缺失本身不能单独证明系统未显示通知。

页面提醒按聊天合并，最多两条，7 秒后收起；同一轮次和同一提问避免反复提醒。系统通知也按聊天合并。

### 更新后手机仍显示旧界面

关闭主屏幕应用后重新打开，进入通知面板查看应用与通知组件版本。前端会主动更新 Service Worker 并同步已有订阅，不会自行申请新的通知权限。

### 停止或卸载

在电脑连接页点击“停止此电脑的手机连接”，然后删除应用。数据目录保留了配对和图片；确认不再需要后可自行删除。若自行添加过系统登录项或 Tailscale Serve 规则，也需自行移除对应项。
