# Changelog

本日志按公开版本组织。一次公开更新可包含多轮本地改动，不补发中间调试编号。发布流程见 [版本与发布规则](docs/releasing.md)。

## 1.3.8 · 正式版 · 2026-11-04

相对稳定版 1.3.7 的累计更新，包含 1.3.8-rc.1 至 rc.3 和后续本地修订。此前待验收问题已由用户实测确认解决，本版设为默认稳定下载。

- Claude Desktop 通过核对过的原生窗口发送文字，跟随桌面模型，保护原聊天身份、消息链和已有草稿。
- 统一 CLI 与桌面记录来源、模型与回执，支持跨工作台状态通知和多电脑共用一个手机通知入口。
- 电脑连接页和手机端提供 GitHub 更新检测，自动查询共享 24 小时缓存，手动查询共享一分钟冷却；只提示下载，不自动安装。

- Grok 长任务按当前用户轮次读取状态，工具输出超过历史显示窗口后仍能识别处理中、待授权和结束；手机不再将中途助手文字直接判为完成。
- 手机将离线与配对失效分开处理，离线前台自动重连，恢复后继续使用原主屏幕入口；配对在使用时续期。
- 打包应用注册当前用户登录启动和异常退出后的定时恢复，主动停止保留暂停状态；后台任务固定配对数据目录并记录服务生命周期日志。
- Claude 桌面历史按原生来源隐藏系统任务通知，从用户消息中去掉 SDK 画布上下文，保留真人文字、消息编号、代码示例和原分支。
- 本地构建同步已包含的公开版本基线，避免功能相同的公开发布仍提示升级；补充发布后的本地核对步骤。
- 用户于 2026-11-04 确认全部待验收问题实测解决；功能边界与验收范围见 KNOWN_ISSUES.md 和 docs/verification.md。

## 1.3.8-rc.3 · 预览版 · 2026-11-03

相对 1.3.8-rc.2 的更新，稳定默认下载继续保留 1.3.7。

- 电脑连接页和手机端加入 GitHub 更新检查与新版下载提示；自动查询按电脑缓存 24 小时，手动查询共享一分钟冷却。
- 按公开版本基线比较新旧，区分稳定与预览渠道，支持本地构建号和公开版本独立递增。

## 1.3.8-rc.2 · 预览版 · 2026-11-03

相对 1.3.8-rc.1 的累计更新，稳定默认下载继续保留 1.3.7。

- Claude Desktop 新增原生窗口文字代发：核对目标聊天、消息链、草稿与模型，保留投递记录以避免不确定结果下重复发送。Windows 隔离聊天已验证；Mac 原生代发尚待实机验收。
- 修订 Claude 桌面历史的附件父链、分支选择和上下文压缩读取，衔接明确保留的原始消息链，过滤内部摘要与提示，不改写原始会话文件。
- 修订 Grok CLI 原生结束事件、活动进程和旧回执判断，统一列表、聊天与投递状态。
- Claude、Grok CLI 的完成、失败、中断及可识别的待处理状态接入通知采集，保留首次历史基线与去重。
- 支持多电脑共用通知入口，沿用一个手机主屏幕图标和已有推送订阅；加入专用通知认证、跨电脑来源与点击路由。
- 改善 iOS 通知权限提示和安装诊断，按电脑名称显示安装入口。

**仍待验收：** [KI-001 / KI-002](KNOWN_ISSUES.md#待验收问题) 的真实手机场景，以及 Mac Claude 原生代发。Grok Bot 桌面仍只读；Claude Desktop 暂不支持手机切模型、图片或工具审批。详见 [验证范围](docs/verification.md)。

## 1.3.8-rc.1 · 预览版 · 2026-11-03

相对公开版 1.3.7 的累计更新，尚未达到稳定发布条件，1.3.7 保留为默认稳定下载。

- 为手机投递绑定工作台、记录来源、文件路径和消息头；过期目标拒绝投递，旧分支回执单独展示。
- 区分 Claude Desktop、Claude Code CLI、Grok Bot 桌面和 Grok CLI。识别出的桌面记录只读，避免经 CLI 写入另一套会话。
- 加入 Claude 消息父链和叶节点读取，以及部分中断信号识别；这些改动仍有历史加载回归，不能视为已完成适配。
- 改进 Grok CLI 回执关联和结束结果读取；停止任务的状态仍可能误报。
- 保留可写 CLI 聊天的模型选择；Claude Desktop 模型入口仅保存助手偏好，不同步桌面设置。
- 新增已知问题清单、接入边界与独立的公开版本发布规则。

**未修复：** [KI-001](KNOWN_ISSUES.md#待修复)（Grok 停止后仍可能显示处理中）、[KI-002](KNOWN_ISSUES.md#待修复)（Claude Desktop 正文可能为空或不完整）。本候选版不包含对这两项最新反馈的修复。

## 1.3.7

- Read native Codex, Claude and Grok subscription usage separately from compatible API provider balances and local token totals.
- Show available quota windows, reset times and credits, with account-scoped caching and Claude rate-limit backoff.
- Add processing and completed-unread indicators to the chat list; clear unread only when the final reply is visible near the bottom of a foreground chat.
- Preserve device-local read markers across refreshes and keep transcript reads separate from native conversation writes.

## 1.3.6

- Add a mobile usage and balance panel for the selected chat, including cumulative tokens, cache reads and the latest model call or Grok task.
- Read compatible provider balances, quotas and actual daily deductions using locally configured API keys, with a one-minute query cache.
- Identify CC Switch credential sources, distinguish recorded account limits from provider balances, and label expired snapshots or unavailable data.

## 1.3.5

- Add Grok CLI text sessions, per-chat model selection, original-session resume and verified CLI completion receipts.
- Protect running Grok sessions from overlapping submissions and keep CLI execution supervision independent of the web service.
- Add distinct status colors, SVG icons, readable labels, reduced motion support and mobile layouts down to 320 pixels.
- Show queued submissions, unanswered questions, uncertain delivery and disconnected states instead of retaining an older completed badge.

## 1.3.4

- Add Codex model selection to the shared mobile model dialog, using the local Codex model catalog and custom model IDs.
- Synchronize the original desktop chat's next-turn model through its owner and confirm it by reading settings back.
- Check the desktop model before submitting a selected-model task and require confirmation if it has changed.
- Keep existing queued tasks subject to the original chat's execution-time model settings.

## 1.3.3

- Add per-chat Claude model selection on mobile, including custom provider model IDs.
- Pass an explicit model to every Claude CLI run and preserve it when retrying the same request.
- Display requested models on sent messages and provider-reported models on Claude replies.
- Reuse the last recorded response model when no preference is saved; require a selection when no model is known.

## 1.3.2

- Fix Claude Code receipt matching when mobile image attachment instructions are appended to the CLI prompt.
- Mark completed Claude Code tasks as completed in the mobile view after the assistant response is written to the JSONL transcript.

## 1.3.1

- Add an optional Claude Code provider that reads local JSONL sessions and resumes them with the official CLI.
- Add a local provider selector for Codex, Claude Code, or both, with namespaced Claude session IDs.
- Pass uploaded images to Claude Code through a controlled attachment directory and document the Desktop IPC limitation.

## 1.2.3

- Publish Mac Apple Silicon and Windows desktop packages with a Chinese mobile PWA.
- Continue existing Codex chats with text, multiple images, draft recovery, and delivery receipts.
- Submit structured question replies through the desktop chat owner, including recovery of older queued replies.
- Match receipts across trailing newline differences and retain message and turn associations beyond recent history.
- Update notification workers, synchronize existing subscriptions, and track provider, device, and display receipts separately.
- Deduplicate task notifications, merge reminders by chat, and automatically dismiss page reminders.
- Adapt the chat layout to mobile visual viewport height and offset while preserving reading position.
- Start with an empty device list so each installation discovers and configures its own Tailscale address.
