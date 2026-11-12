# AGENTS.md

## 隧道配置

新增、修改或排查 AutoSSH Desk 隧道前，先阅读 [docs/ssh-tunnel-setup.md](docs/ssh-tunnel-setup.md)。

必须遵守：

- 先验证 SSH 别名可用（`ssh -G <alias>`、`ssh <alias> true`），再在界面填写。
- 使用 SSH 别名时，“SSH 端口”和“私钥路径”留空，让 `~/.ssh/config` 生效；仅当别名未定义时填写。
- 区分 SSH 端口、远端监听端口和目标端口。
- 反向转发（`ssh -R`）的目标由运行 autossh 的本机解析和连接。
- 不要在文档、日志、提交或截图中写真实 IP、域名、用户名、密钥路径、隧道 ID 等敏感信息。

相关文档：

- [docs/manual-testing.md](docs/manual-testing.md)
- [docs/remote-port-conflict.md](docs/remote-port-conflict.md)
