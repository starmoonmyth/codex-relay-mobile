# 程序与下载对照

## 根据使用场景选择

| 想做什么 | 对应程序 | 在哪台设备上运行 | 当前仓库提供什么 |
| --- | --- | --- | --- |
| 在电脑独立聊天窗口中查看、继续 Codex 聊天 | Codex 消息助手（电脑聊天版），`CodexRelay.exe` / `CodexRelay.app` | Windows / Mac | 当前未发布该程序的源码和安装包 |
| 在手机发送任务、查看回复、上传图片、回答提问和接收提醒 | Codex 手机消息助手（手机连接服务），`CodexRelayMobile.exe` / `CodexRelayMobile.app` | 连接服务运行在 Windows / Mac；手机使用网页或 PWA | `source/` 源码和 Releases 中的 `CodexRelayMobile-*` 发布包 |
| 在官方应用中管理原始聊天、使用模型并执行任务 | OpenAI 官方 Codex 桌面应用 | Windows / Mac | 需要用户单独安装，发行包不包含官方 Codex |

手机连接服务与电脑聊天版是独立程序。它们使用已有 Codex 聊天；用户只使用手机连接服务时，安装官方 Codex 和手机连接服务即可。

## 发布附件分别是什么

| 文件 | 作用 |
| --- | --- |
| `CodexRelayMobile-<版本>-Windows-x64.zip` | Windows 手机连接服务。解压后运行 `CodexRelayMobile.exe`，电脑浏览器打开二维码连接页 |
| `CodexRelayMobile-<版本>-macOS-arm64.dmg` | Mac Apple Silicon 手机连接服务。安装并运行 `CodexRelayMobile.app`，电脑浏览器打开二维码连接页 |
| `Source code (zip)` / `Source code (tar.gz)` | GitHub 按该发行标签生成的源码包，内容是手机连接服务；需要安装 Python 依赖运行或自行构建 |
| `SHA256SUMS.txt` | 对应发行版附件的 SHA256 校验值 |
| `dependencies-windows.json` / `dependencies-macos.json` | 构建依赖清单，供检查依赖使用 |

下载入口：[GitHub Releases](https://github.com/starmoonmyth/codex-relay-mobile/releases)。能力范围以所下载版本的发行说明为准。

## 打开后应该看到什么

手机连接服务双击后打开的是**电脑配对管理页**，用来给手机扫码和管理此电脑的连接。配对完成后，真正的任务输入框和回复列表在**手机网页**中。

手机首次扫描二维码后，在网页中选择这台电脑的聊天。iPhone 使用 Safari“添加到主屏幕”，Android 也可安装为 PWA；系统推送的要求见 [配置与排障](configuration.md#收不到通知)。电脑需要保持运行，手机和电脑的 Tailscale 需要在线。

独立电脑聊天版打开的是带聊天列表、消息内容和输入框的原生窗口。对应文件名为 `CodexRelay`，本仓库暂不提供该版本的下载。

## 更新、停止和卸载

更新时按程序名对应替换：`CodexRelayMobile` 更新手机连接服务，`CodexRelay` 更新电脑聊天版。两个程序可以同时保留。

在手机连接服务的电脑连接页点击“停止此电脑的手机连接”，会停止手机远程连接；电脑聊天版有自己的窗口和退出操作。

手机连接服务的数据保存在 Windows 的 `%LOCALAPPDATA%\CodexRelay\Mobile` 或 Mac 的 `~/Library/Application Support/CodexRelay/Mobile`。其中包含配对、通知、图片和回执，更新程序时应保留。普通更新只需替换应用本体，具体配置见 [配置与排障](configuration.md)。
