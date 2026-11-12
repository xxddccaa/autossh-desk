# SSH 隧道配置指南

本文说明如何在 AutoSSH Desk 中新增一条反向 SSH 隧道，以及如何避免把 SSH 端口、远端监听端口和目标端口混淆。文中的主机、域名、用户名、端口和路径均为脱敏示例，请替换为本地实际值。

## 先分清三个端口

| 名称 | 配置位置 | 作用 |
| --- | --- | --- |
| SSH 端口 | `~/.ssh/config` 的 `Port`，或界面的“SSH 端口” | 本机连接远端 SSH 服务的端口 |
| 远端监听端口 | 界面的“远端监听端口” | 反向隧道在 SSH 主机上打开的端口 |
| 目标端口 | 界面的“目标端口” | 隧道最终转发的目标服务端口 |

只有 SSH 端口用于建立 SSH 连接；另外两个端口用于 `ssh -R` 的转发规则。

示例：

```bash
ssh -R 127.0.0.1:15432:target.example.com:5432 tunnel-host
```

- `tunnel-host`：SSH 别名，实际主机和 SSH 端口由 `~/.ssh/config` 决定。
- `127.0.0.1:15432`：远端监听地址和端口。
- `target.example.com:5432`：目标服务；该地址由运行 `ssh` 的本机解析和连接。

## 推荐配置流程

### 1. 配置并验证 SSH 别名

在 `~/.ssh/config` 中准备一个别名：

```sshconfig
Host tunnel-host
  HostName remote.example.com
  User user
  Port 22
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
  ServerAliveInterval 30
  ServerAliveCountMax 3
```

验证别名解析出的主机、用户和端口：

```bash
ssh -G tunnel-host | grep -E '^(hostname|user|port|identityfile) '
```

验证网络和认证：

```bash
ssh -o BatchMode=yes -o ConnectTimeout=8 tunnel-host true
```

执行 `ssh -o BatchMode=yes -o ConnectTimeout=8 tunnel-host true` 必须成功。失败时不要继续创建或启动隧道，先修网络、DNS、SSH 端口、密钥或 SSH Agent。

### 2. 确认远端监听端口空闲

```bash
ssh -o BatchMode=yes tunnel-host "ss -ltnp 'sport = :15432'"
```

或：

```bash
ssh -o BatchMode=yes tunnel-host "lsof -nP -iTCP:15432 -sTCP:LISTEN"
```

输出为空才表示端口空闲。端口被占用时不要直接结束进程，先按 `docs/remote-port-conflict.md` 核对 PID、进程名和命令行，再决定是否清理。

### 3. 在界面填写

| 界面字段 | 填写方式 |
| --- | --- |
| SSH 目标 | 已验证的 SSH 别名，例如 `tunnel-host` |
| SSH 端口 | 别名已配置 `Port` 时留空；仅当别名没有端口时才填写 |
| 私钥路径 | 别名或 SSH Agent/Keychain 已配置时留空；否则填写 |
| 远端监听地址 | 通常为 `127.0.0.1` |
| 远端监听端口 | 远端主机上空闲的端口，例如 `15432` |
| 目标主机 | 目标服务域名或地址，例如 `target.example.com` |
| 目标端口 | 目标服务端口，例如 `5432` |
| 自动重连 | 建议开启 |
| 随应用启动 | 按需开启 |

关键点：界面里的“SSH 端口”不是“远端监听端口”。使用别名时，SSH 端口通常应留空，让 `~/.ssh/config` 生效；如果在界面再填一个不同的端口，可能与别名配置不一致。

### 4. 启动并验证

在界面点击“测试连接”和“启动”。也可以从远端主机验证监听：

```bash
ssh -o BatchMode=yes tunnel-host "ss -ltnp 'sport = :15432'"
```

再验证端到端 TCP：

```bash
ssh -o BatchMode=yes tunnel-host "nc -z -w 4 127.0.0.1 15432"
```

如果目标是 HTTPS，可进一步验证：

```bash
ssh -o BatchMode=yes tunnel-host "curl -skI --max-time 8 https://127.0.0.1:15432/"
```

其他协议使用对应的客户端验证。

## 常见错误

| 现象 | 优先检查 |
| --- | --- |
| “SSH 不可达”或“远端 SSH 不可达” | `ssh -G <alias>` 的 HostName/Port 是否正确；`ssh <alias> true` 是否成功；别名是否已失效 |
| 认证失败 | 用户、密钥路径、SSH Agent/Keychain、`IdentityFile` |
| `remote port forwarding failed` | 远端监听端口被占用；按 `docs/remote-port-conflict.md` 处理 |
| 本机目标 DNS 解析失败 | 本机 VPN、DNS、路由；`ssh -R` 的目标由本机解析 |
| 目标 TCP 不通 | 本机到目标服务的 VPN、防火墙或路由 |
| SSH 已连接但远端访问失败 | 远端监听地址是否只绑定了 `127.0.0.1`；端口是否写错；目标服务是否正常 |

## 最重要的原则

1. **先验证别名，再填界面。** 不要根据记忆猜 SSH 主机或端口。
2. **使用别名时，SSH 端口和私钥路径优先留空。** 让 OpenSSH 配置统一管理。
3. **区分 SSH 端口、远端监听端口和目标端口。**
4. **`ssh -R` 的目标由本机解析和连接。** 远端主机不需要能解析目标域名，但本机必须能访问目标。
5. **远端端口冲突不要盲目 kill。** 先确认 PID 和进程，再按文档清理。
6. **提交到 GitHub 前脱敏。** 不要写真实 IP、域名、用户名、密钥路径、隧道 ID、日志或内部端口映射。

## 脱敏配置示例

```text
名称: example-tunnel
SSH 目标: tunnel-host
SSH 端口: 留空（由 SSH 别名提供）
私钥路径: 留空（由 SSH 配置或 Agent 提供）
远端监听地址: 127.0.0.1
远端监听端口: 15432
目标主机: target.example.com
目标端口: 5432
自动重连: 开启
随应用启动: 按需
```
