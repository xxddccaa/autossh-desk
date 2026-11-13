# AutoSSH Desk

AutoSSH Desk 是一个在本机运行的 macOS autossh 隧道管理器，用 FastAPI + React Web UI 管理多个反向 SSH 隧道。支持 HTTPS、数据库和其他 TCP 服务，反向转发的数据路径为：

```text
远端应用 → 远端监听 → SSH 隧道 → 本机 Mac → 目标服务
```

它直接调用 `/usr/bin/ssh` 和 Homebrew 安装的 `autossh`。认证由现有 SSH Agent、Keychain 或 `~/.ssh/config` 完成，应用不会要求输入或保存 SSH 密码、私钥内容、数据库密码。仓库中的主机名、域名、IP 和进程号均为脱敏示例。

![AutoSSH Desk 界面预览（示例数据）](docs/images/autossh-desk.png)

## 功能

- 多隧道列表，新增、编辑、复制、删除和独立启停
- SSH Host alias 或 `user@host` 配置
- 启动前顺序检查 autossh、DNS、目标服务端口、远端 SSH 端口、SSH 认证和远端监听端口
- 每条隧道展示可复制的远端 socat 与 `/etc/hosts` 建议指令，仅生成、不自动执行
- 已连接的受管远端监听显示绿色通过，非受管或未确认的监听保留警告
- 非受管监听不自动复用，TCP 可达不等于转发目标正确
- 识别远端 `remote port forwarding failed`，持续退避检查，端口释放后自动恢复连接
- 只终止带本应用 marker 的受管进程
- 远端端口冲突先展示精确 PID、进程名和命令行，再进行二次确认
- 实时状态/日志、网络恢复后自动连接与指数退避重连、睡眠唤醒和网络路径变化后重新诊断
- 应用重启后重新识别仍由本应用启动的 autossh

## 环境要求

- macOS 14 或更高版本
- Python 3.12+
- Node.js + pnpm（仅用于构建前端）
- Homebrew autossh
- 可用的 SSH key / Agent / Keychain 配置

检查 autossh：

```bash
command -v autossh
autossh -V
```

缺少时手动执行：

```bash
brew install autossh
```

AutoSSH Desk 不会自动安装 autossh，也不会修改 OpenVPN 配置或 `/etc/resolver`。

## 首次安装

```bash
# 在项目目录中
./setup.sh
```

安装脚本只在项目内创建 `.venv`，并安装 Python/npm 项目依赖、构建前端。

启动：

```bash
./run.sh
```

默认监听 `http://127.0.0.1:8765` 并自动打开浏览器。如果希望手动打开：

```bash
AUTOSSH_DESK_NO_BROWSER=1 ./run.sh
```

## SSH 配置

推荐在 `~/.ssh/config` 中配置别名，然后在界面填写别名：

```sshconfig
Host remote-host
  HostName remote.example.com
  User root
  # 可选：Port 22
  # 可选：IdentityFile ~/.ssh/id_ed25519
  ServerAliveInterval 30
  ServerAliveCountMax 3
  TCPKeepAlive yes
```

并先把 key 加入 macOS Keychain 或 Agent：

```bash
ssh-add --apple-use-keychain ~/.ssh/id_ed25519
```

AutoSSH Desk 的诊断命令使用 `BatchMode=yes`，不会弹出密码输入框，也不会把认证内容写入配置或日志。

## 使用

1. 打开左侧隧道，点击“设置”确认 SSH 目标、监听端口和目标服务。
2. 点击“测试连接”查看完整链路。
3. 点击“启动”。应用先检查远端 15432，再启动 autossh。
4. 远端端口已有非受管监听时显示冲突，不复用未知会话，也不启动第二个 autossh；TCP 可达不代表服务健康。
5. 若显示“远端端口冲突”，点击“清理端口”查看远端监听详情；只有唯一 `sshd` 进程可被确认时按钮才可用。
6. “连接参数”复制 DBeaver 主机端口信息和一条本地 SSH 转发命令；“诊断”复制状态与脱敏日志。

启用“自动重连”后，只要 AutoSSH Desk 保持运行，即使隧道目前显示“未启动”，应用也会定期检查目标服务和远端 SSH 是否可达。手动恢复 OpenVPN 后，网络通畅时会自动执行启动检查并建立 autossh 隧道，无需点击“启动”；无法连接时继续等待网络恢复。“随应用启动”会在打开 AutoSSH Desk 时立即尝试连接，即使当时 VPN 尚不可达。手动点击“停止”会暂停该隧道的自动连接，直到再次点击“启动”；如果启用了“随应用启动”，下次打开 AutoSSH Desk 时仍会尝试连接。复制出的隧道保持未启动，首次点击“启动”后才会开始自动重连。

远端端口冲突不会关闭自动重连：应用按 1、2、5、10、20、30、60 秒退避，之后持续按 60 秒退避检查（实际检查在后台监控周期内执行）。端口释放后自动重建 autossh；非受管监听即使 TCP 可达也不会被复用或标记已连接。界面会显示“自动保持连接”和下次重试时间，无需再次点击“启动”。应用不会自动终止远端占用进程；占用长期不释放时仍可使用“清理端口”核对用途并二次确认，清理后自动重连。清理会中断该 sshd 会话的全部转发，不会停止发起端的自动重连工具。

SSH 探测中的连接重置、连接关闭、超时或无错误输出的非零退出，也会持续退避重试，不会仅凭 SSH 非零退出码就认定为认证失败。受管 autossh 仍在运行时，临时探测失败会保留该进程并重新诊断，避免无谓地中断隧道。只有明确的认证拒绝、密钥或主机指纹错误、额外转发配置或缺少 autossh 时才暂停重试，等待人工处理；不会自动跳过主机指纹验证。

运行中的隧道必须先停止才能编辑或删除配置。应用退出时不会自动停止运行中的 autossh，重新打开后可以重新接管；需要停止时只使用界面中的“停止”。

## 数据与权限

```text
~/Library/Application Support/AutoSSH Desk/
├── autossh-desk.sqlite3
├── instance.lock
├── records/<tunnel-id>.json
└── logs/<tunnel-id>.log
```

从 WikiTunnel 升级时会继续使用旧的 `~/Library/Application Support/WikiTunnel/` 及 `wikitunnel.sqlite3`，不搬动已有数据库、日志或正在运行的 autossh。旧的 `WIKITUNNEL_*` 环境变量、会话 token 请求头和进程标记也继续兼容；新配置建议使用 `AUTOSSH_DESK_*`。

配置目录权限为 `0700`，进程记录和日志文件权限为 `0600`。日志读取、实时推送和诊断复制会脱敏密码、Bearer token、secret 赋值和私钥块。

## 自动化测试

```bash
.venv/bin/pytest
pnpm --dir frontend build
```

当前测试覆盖隧道 CRUD、端口唯一约束、本地会话 token、非受管监听拒绝复用、错误 HTTP/TLS 服务的 TCP 误判回归、SSH 转发与连接复用隔离、目标不可达、认证失败、各入口端口冲突后的持续重试与恢复、退避上限、端口释放后的自动恢复、冲突清理二次确认与 TCP 状态变化保护、VPN 恢复后自动连接、手动停止后暂停重连、旧数据库升级、日志脱敏、日志会话边界、文件权限、单实例锁、marker 参数、应用重启接管和只停止本应用进程。

前端冒烟测试使用本机 Google Chrome：

```bash
pnpm --dir frontend test:e2e
```

真实网络与远端手工会话的验收步骤见 [docs/manual-testing.md](docs/manual-testing.md)。`remote port forwarding failed` 的原因与处理见 [docs/remote-port-conflict.md](docs/remote-port-conflict.md)。

## 已知范围

- 这是浏览器页面，不提供原生菜单栏图标。关闭浏览器不会停止后端；停止后端不会主动终止已运行的 autossh。
- “随应用启动”表示打开 AutoSSH Desk 时启动隧道，不创建 macOS 登录 LaunchAgent。
- 状态通知使用浏览器 Notification API，可在页面右上角开启或关闭，只有页面保持打开时有效。

