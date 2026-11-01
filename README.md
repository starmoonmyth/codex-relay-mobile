# Codex Relay Mobile · Codex 手机消息助手

通过 Tailscale，在手机上继续 Mac 或 Windows 上已有的 Codex 聊天。电脑负责执行，手机可以发任务、上传图片、回复结构化提问并接收任务通知。

这是独立社区项目，与 OpenAI 无隶属关系。连接桌面聊天需要本机 Codex CLI、聊天数据库和桌面内部 IPC；接口会随桌面版本变化，兼容性不能覆盖所有版本。

## 功能

- 查看已有聊天、项目与回复，在同一聊天中继续发送任务。
- 发送文字、多张图片或图文消息；单张图片最多 8 MB，每条消息图片合计最多 32 MB。
- 回复结构化提问中的选项或自定义答案，由持有聊天的桌面窗口即时提交。
- 查看入队、接收、执行中、完成和失败状态，恢复草稿，防止重复提交。
- 任务完成、失败、中断或需要回答时，提供页面提醒和可选 Web Push。
- 手机 PWA、二维码配对、Mac 与 Windows 设备切换。

## 快速开始

1. 从 [Releases](../../releases) 下载 Windows EXE 或 Mac Apple Silicon DMG。
2. 电脑和手机安装并连接同一个 [Tailscale](https://tailscale.com/download) 网络，允许使用 Tailscale HTTPS 和 Serve。
3. 在电脑上打开 Codex 桌面应用及目标聊天，确认其自带 CLI 可用。
4. Windows 双击 EXE；Mac 将 DMG 中的应用拖入“应用程序”后打开。
5. 助手启动后，电脑浏览器会打开连接页。用手机扫描二维码完成配对。
6. iPhone 在 Safari 中“添加到主屏幕”，以后从主屏幕图标打开。Android 可安装为 PWA。

程序默认监听 `127.0.0.1:8879`，首次运行会配置私有 Tailscale Serve HTTPS 端口 `8444`。如 Tailscale 提示需要先启用 HTTPS，请完成提示后重新启动助手。本机连接页是 <http://127.0.0.1:8879/connect>。

Mac 发行包使用本地 ad-hoc 签名，未进行 Apple Developer ID 公证；如系统阻止打开，可在确认下载来源后，通过“系统设置 → 隐私与安全性”允许打开。Windows 发行包未做商业代码签名。

公开安装包不附带开机启动项。需要自动运行时，可自行添加到系统登录项；程序支持 `--background` 参数。

## 通知

打开聊天顶部的“通知”，开启页面提醒或系统推送。

iPhone / iPad 系统推送需要 iOS 16.4 及以上，并从主屏幕应用中申请权限。开启后先点“测试本机通知”，再点“发送测试通知”。如果本机通知正常、远程推送一直没有手机回执，可点“重新连接系统推送”重建订阅。

推送服务接受请求与手机收到通知是两个阶段，界面会分别显示回执。通知包含电脑名称、聊天标题与状态；点击后通过 Tailscale 返回聊天。电脑需要保持运行并能访问 Apple、Google、Mozilla 或 Microsoft 的推送服务。

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

此片段只替换 `peers` 字段，其余自动生成的配置应保留。两台电脑分别扫描配对、分别开启通知。可选 `codex_home` 和 `cli` 字段用于指定非默认 Codex 数据目录或 CLI 路径。

更多信息见 [配置与排障](docs/configuration.md)。

## 从源码运行

需要 Python 3.13、Tailscale 和可用的 Codex 桌面应用。命令均在仓库根目录执行。

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

当前不支持新建聊天、切换模型、上传非图片文件或在手机处理命令执行审批。出现“不确定”回执时应先检查原聊天，避免重复发送。

## 隐私与安全

配对二维码和链接相当于这台电脑的控制凭证，请只交给自己信任的设备。配对会话有效期为 30 天，可在手机退出配对。程序使用 Tailscale 私网 HTTPS，不配置公网 Funnel，也不读取或复制模型 API 密钥。

不要提交本机 `config.json`、`mobile.sqlite`、`pairing-token`、Web Push 私钥、上传图片或聊天历史。安全说明见 [SECURITY.md](SECURITY.md)。

## 许可证

原创代码使用 [MIT License](LICENSE)。第三方库保留各自许可证，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 与 [`source/licenses`](source/licenses)。OpenAI Codex 和 Tailscale 需单独安装，不包含在本项目发行包中。
