from __future__ import annotations

import asyncio
import os
import re
import shutil
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from .config import Settings, env_setting
from .models import Tunnel
from .redaction import redact_text
from .schemas import CheckResult, RemoteListener


class CommandResult:
    def __init__(self, code: int, stdout: str, stderr: str, timed_out: bool = False):
        self.code = code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out

    @property
    def message(self) -> str:
        text = (self.stderr or self.stdout).strip()
        if self.timed_out:
            return "命令超时"
        return redact_text(text) or f"退出码 {self.code}"


def ssh_failure_is_retryable(result: CommandResult) -> bool:
    if result.ok:
        return False
    if result.timed_out:
        return True
    message = f"{result.stderr}\n{result.stdout}".lower()
    return not any(
        error in message
        for error in (
            "permission denied",
            "no such identity",
            "host key verification failed",
            "remote host identification has changed",
            "too many authentication failures",
            "no supported authentication methods",
            "bad configuration option",
            "bad owner or permissions",
            "unprotected private key file",
            "invalid format",
            "incorrect passphrase",
            "no matching host key type",
            "no matching key exchange method",
            "no matching cipher",
        )
    )


async def run_command(
    args: list[str],
    timeout: float,
    *,
    env: dict[str, str] | None = None,
) -> CommandResult:
    process_env = os.environ.copy()
    process_env.update(
        {
            "LANG": "C",
            "LC_ALL": "C",
            "SSH_ASKPASS_REQUIRE": "never",
        }
    )
    if env:
        process_env.update(env)
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=process_env,
            start_new_session=True,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        return CommandResult(
            process.returncode or 0,
            stdout.decode("utf-8", "replace"),
            stderr.decode("utf-8", "replace"),
        )
    except asyncio.TimeoutError:
        try:
            process.kill()
            await process.wait()
        except (ProcessLookupError, UnboundLocalError):
            pass
        return CommandResult(124, "", "命令超时", timed_out=True)
    except OSError as exc:
        return CommandResult(127, "", str(exc))


@dataclass(slots=True)
class SSHConfig:
    hostname: str
    port: int
    user: str | None = None
    configured_forwards: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DiagnosticSnapshot:
    checks: list[CheckResult] = field(default_factory=list)
    remote_listener: RemoteListener = field(default_factory=RemoteListener)
    end_to_end_ok: bool = False
    end_to_end_known: bool = False
    autossh_path: str | None = None
    ssh_config: SSHConfig | None = None
    network_ok: bool = False
    ssh_auth_ok: bool = False
    ssh_failure_retryable: bool = False
    error: str | None = None

    @property
    def preflight_ok(self) -> bool:
        return all(check.state in {"ok", "warn"} for check in self.checks)

    def check(self, key: str) -> CheckResult | None:
        return next((item for item in self.checks if item.key == key), None)


class Diagnostics:
    def __init__(self, settings: Settings):
        self.settings = settings

    def find_autossh(self) -> str | None:
        override = env_setting("AUTOSSH", "")
        if override:
            return override
        return shutil.which("autossh")

    async def resolve_ssh(self, tunnel: Tunnel) -> tuple[SSHConfig | None, str | None]:
        args = [self.settings.ssh_executable, "-G", "-o", "ClearAllForwardings=no"]
        if tunnel.ssh_port:
            args.extend(["-p", str(tunnel.ssh_port)])
        if tunnel.identity_file:
            args.extend(["-i", str(Path(tunnel.identity_file).expanduser())])
        args.append(tunnel.ssh_target)
        result = await run_command(args, timeout=8)
        if not result.ok:
            return None, result.message
        values: dict[str, str] = {}
        configured_forwards: list[str] = []
        for line in result.stdout.splitlines():
            key, _, value = line.partition(" ")
            values[key] = value.strip()
            if key in {"localforward", "remoteforward", "dynamicforward"}:
                configured_forwards.append(key)
        hostname = values.get("hostname")
        if not hostname:
            return None, "SSH 配置中没有可解析的 hostname"
        try:
            port = int(values.get("port", "22"))
        except ValueError:
            port = 22
        return SSHConfig(
            hostname=hostname,
            port=port,
            user=values.get("user") or None,
            configured_forwards=configured_forwards,
        ), None

    def ssh_args(self, tunnel: Tunnel) -> list[str]:
        args = [
            self.settings.ssh_executable,
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "ControlMaster=no",
            "-o",
            "ControlPath=none",
            "-o",
            "ControlPersist=no",
            "-o",
            "ForkAfterAuthentication=no",
            "-o",
            "RemoteCommand=none",
            "-o",
            f"ConnectTimeout={tunnel.connect_timeout}",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=2",
            "-o",
            "LogLevel=ERROR",
        ]
        if tunnel.ssh_port:
            args.extend(["-p", str(tunnel.ssh_port)])
        if tunnel.identity_file:
            args.extend(["-i", str(Path(tunnel.identity_file).expanduser())])
        args.append(tunnel.ssh_target)
        return args

    async def run_remote(
        self, tunnel: Tunnel, remote_command: str, timeout: float = 12
    ) -> CommandResult:
        return await run_command(
            [*self.ssh_args(tunnel), remote_command],
            timeout=timeout,
        )

    async def _tcp_check(
        self,
        host: str,
        port: int,
        timeout: float,
    ) -> tuple[bool, int, str]:
        started = time.perf_counter()
        writer: asyncio.StreamWriter | None = None
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host=host, port=port), timeout=timeout
            )
            elapsed = max(0, int((time.perf_counter() - started) * 1000))
            return True, elapsed, f"{host}:{port} 可连接"
        except asyncio.TimeoutError:
            return False, int(timeout * 1000), f"{host}:{port} 连接超时"
        except OSError as exc:
            return False, None, f"{host}:{port} 不可达：{redact_text(str(exc), 240)}"
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

    async def _dns_check(self, host: str) -> CheckResult:
        started = time.perf_counter()
        try:
            loop = asyncio.get_running_loop()
            addresses = await asyncio.wait_for(
                loop.getaddrinfo(host, None, type=socket.SOCK_STREAM), timeout=6
            )
            unique = sorted({item[4][0] for item in addresses})
            latency = int((time.perf_counter() - started) * 1000)
            return CheckResult(
                key="dns",
                label="目标 DNS 解析",
                state="ok",
                detail=", ".join(unique[:4]) or host,
                latency_ms=latency,
            )
        except (socket.gaierror, asyncio.TimeoutError, OSError) as exc:
            return CheckResult(
                key="dns",
                label="目标 DNS 解析",
                state="fail",
                detail=redact_text(str(exc), 240) or "解析失败",
            )

    async def quick_network_ok(self, tunnel: Tunnel) -> bool:
        """Check only local network reachability, without opening an SSH command."""
        try:
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.getaddrinfo(tunnel.target_host, None, type=socket.SOCK_STREAM),
                timeout=3,
            )
        except (socket.gaierror, asyncio.TimeoutError, OSError):
            return False
        target_ok, _, _ = await self._tcp_check(
            tunnel.target_host, tunnel.target_port, min(3, tunnel.connect_timeout)
        )
        if not target_ok:
            return False
        ssh_config, _ = await self.resolve_ssh(tunnel)
        if ssh_config is None:
            return False
        ssh_ok, _, _ = await self._tcp_check(
            ssh_config.hostname, ssh_config.port, min(3, tunnel.connect_timeout)
        )
        return ssh_ok

    async def parse_remote_listener(
        self, tunnel: Tunnel, raw: str
    ) -> RemoteListener:
        pids = sorted({int(value) for value in re.findall(r"pid=(\d+)", raw)})
        listener = RemoteListener(
            found=bool(raw.strip()),
            address=tunnel.remote_bind_host,
            port=tunnel.remote_port,
            pids=pids,
            raw=redact_text(raw.strip(), 1600),
        )
        if not pids or not raw.strip():
            return listener
        pid_list = ",".join(str(pid) for pid in pids)
        command = f"ps -o pid= -o ppid= -o user= -o comm= -o args= -p {pid_list}"
        result = await self.run_remote(tunnel, command, timeout=8)
        if not result.ok:
            return listener
        for line in result.stdout.splitlines():
            parts = line.split(None, 4)
            if len(parts) < 4 or not parts[0].isdigit():
                continue
            command_line = parts[4] if len(parts) > 4 else parts[3]
            listener.processes.append(
                {
                    "pid": int(parts[0]),
                    "ppid": int(parts[1]) if parts[1].isdigit() else None,
                    "user": parts[2],
                    "comm": Path(parts[3]).name,
                    "args": redact_text(command_line, 500),
                    "is_sshd": Path(parts[3]).name == "sshd"
                    or Path(command_line.split(None, 1)[0]).name == "sshd",
                }
            )
        return listener

    async def remote_listener(self, tunnel: Tunnel) -> tuple[RemoteListener, str | None]:
        port = tunnel.remote_port
        remote_command = (
            f"if command -v ss >/dev/null 2>&1; then "
            f"ss -H -ltnp 'sport = :{port}'; "
            f"elif command -v lsof >/dev/null 2>&1; then "
            f"lsof -nP -iTCP:{port} -sTCP:LISTEN || true; "
            f"else echo AUTOSSH_DESK_NO_LISTENER_TOOL >&2; exit 127; fi"
        )
        result = await self.run_remote(tunnel, remote_command, timeout=12)
        if not result.ok:
            return RemoteListener(found=False, port=port), result.message
        return await self.parse_remote_listener(tunnel, result.stdout), None

    async def end_to_end(self, tunnel: Tunnel) -> tuple[bool | None, str]:
        connect_host = tunnel.remote_bind_host
        if connect_host in {"0.0.0.0", "localhost"}:
            connect_host = "127.0.0.1"
        elif connect_host == "::":
            connect_host = "::1"
        host = connect_host
        port = tunnel.remote_port
        command = (
            "if command -v nc >/dev/null 2>&1; then "
            f"nc -z -w 3 {host} {port}; "
            "elif command -v bash >/dev/null 2>&1; then "
            "if command -v timeout >/dev/null 2>&1; then "
            f"timeout 3 bash -c 'exec 3<>/dev/tcp/{host}/{port}'; "
            "else "
            f"bash -c 'exec 3<>/dev/tcp/{host}/{port}'; "
            "fi; "
            "elif command -v python3 >/dev/null 2>&1; then "
            f"python3 -c 'import socket; s=socket.create_connection((\"{host}\", {port}), 3); s.close()'; "
            "else echo AUTOSSH_DESK_NO_TCP_PROBE >&2; exit 127; fi"
        )
        result = await self.run_remote(tunnel, command, timeout=8)
        if result.ok:
            return True, (
                f"远端 {connect_host}:{tunnel.remote_port} 接受 TCP 连接；"
                "仅验证监听，未验证转发目标或服务协议"
            )
        if result.code == 127 or "AUTOSSH_DESK_NO_TCP_PROBE" in result.stderr:
            return None, "远端缺少 nc、bash /dev/tcp 和 python3，无法执行 TCP 监听探测"
        return False, result.message

    async def run(self, tunnel: Tunnel) -> DiagnosticSnapshot:
        snapshot = DiagnosticSnapshot()

        autossh_path = self.find_autossh()
        snapshot.autossh_path = autossh_path
        if autossh_path:
            snapshot.checks.append(
                CheckResult(
                    key="autossh",
                    label="autossh",
                    state="ok",
                    detail=autossh_path,
                )
            )
        else:
            snapshot.checks.append(
                CheckResult(
                    key="autossh",
                    label="autossh",
                    state="fail",
                    detail="未找到，请执行 brew install autossh",
                )
            )
            snapshot.error = "缺少 autossh"
            return snapshot

        dns = await self._dns_check(tunnel.target_host)
        snapshot.checks.append(dns)
        if dns.state != "ok":
            snapshot.error = dns.detail
            snapshot.checks.append(
                CheckResult(
                    key="target_tcp",
                    label="目标服务端口",
                    state="unknown",
                    detail="DNS 检查未通过",
                )
            )
        else:
            target_ok, latency, detail = await self._tcp_check(
                tunnel.target_host, tunnel.target_port, tunnel.connect_timeout
            )
            snapshot.checks.append(
                CheckResult(
                    key="target_tcp",
                    label="目标服务端口",
                    state="ok" if target_ok else "fail",
                    detail=detail,
                    latency_ms=latency,
                )
            )
            if not target_ok:
                snapshot.error = "目标服务不可达"

        ssh_config, ssh_error = await self.resolve_ssh(tunnel)
        snapshot.ssh_config = ssh_config
        if ssh_config is None:
            snapshot.checks.append(
                CheckResult(
                    key="ssh_tcp",
                    label="远端 SSH 端口",
                    state="fail",
                    detail=ssh_error or "SSH 配置无法解析",
                )
            )
            snapshot.error = snapshot.error or "SSH 配置无法解析"
            snapshot.checks.append(
                CheckResult(
                    key="ssh_auth",
                    label="SSH 认证",
                    state="unknown",
                    detail="SSH 配置解析失败",
                )
            )
            return snapshot

        forwarding_ok = not ssh_config.configured_forwards
        snapshot.checks.append(
            CheckResult(
                key="ssh_forwarding",
                label="SSH 转发隔离",
                state="ok" if forwarding_ok else "fail",
                detail=(
                    "SSH 别名未配置额外转发"
                    if forwarding_ok
                    else "SSH 别名包含额外转发，请使用不含 LocalForward、RemoteForward 或 DynamicForward 的专用别名"
                ),
            )
        )
        if not forwarding_ok:
            snapshot.error = snapshot.check("ssh_forwarding").detail
            return snapshot

        ssh_ok, latency, detail = await self._tcp_check(
            ssh_config.hostname, ssh_config.port, min(tunnel.connect_timeout, 8)
        )
        snapshot.checks.append(
            CheckResult(
                key="ssh_tcp",
                label="远端 SSH 端口",
                state="ok" if ssh_ok else "fail",
                detail=detail,
                latency_ms=latency,
            )
        )
        if not ssh_ok:
            snapshot.error = snapshot.error or "远端 SSH 不可达"
            snapshot.checks.append(
                CheckResult(
                    key="ssh_auth",
                    label="SSH 认证",
                    state="unknown",
                    detail="SSH 端口不可达",
                )
            )
            return snapshot

        auth = await self.run_remote(tunnel, "true", timeout=min(tunnel.connect_timeout + 3, 20))
        snapshot.ssh_auth_ok = auth.ok
        snapshot.ssh_failure_retryable = ssh_failure_is_retryable(auth)
        snapshot.checks.append(
            CheckResult(
                key="ssh_auth",
                label="SSH 连接" if snapshot.ssh_failure_retryable else "SSH 认证",
                state="ok" if auth.ok else "fail",
                detail="认证成功" if auth.ok else auth.message,
            )
        )
        if not auth.ok:
            snapshot.error = "SSH 连接暂时失败" if snapshot.ssh_failure_retryable else "SSH 认证失败"
            return snapshot

        listener, listener_error = await self.remote_listener(tunnel)
        snapshot.remote_listener = listener
        if listener_error:
            snapshot.error = snapshot.error or listener_error
        snapshot.checks.append(
            CheckResult(
                key="remote_listener",
                label=f"远端 {tunnel.remote_port} 监听",
                state="fail" if listener_error else ("warn" if listener.found else "ok"),
                detail=(
                    f"端口已被监听；PID {', '.join(map(str, listener.pids)) or '未知'}"
                    if listener.found
                    else "端口空闲"
                )
                if listener_error is None
                else listener_error,
            )
        )

        if listener.found:
            end_to_end_ok, e2e_detail = await self.end_to_end(tunnel)
            snapshot.end_to_end_ok = end_to_end_ok is True
            snapshot.end_to_end_known = end_to_end_ok is not None
            snapshot.checks.append(
                CheckResult(
                    key="end_to_end",
                    label="远端监听 TCP（目标未验证）",
                    state=(
                        "ok"
                        if end_to_end_ok is True
                        else "fail"
                        if end_to_end_ok is False
                        else "unknown"
                    ),
                    detail=e2e_detail,
                )
            )
        else:
            snapshot.checks.append(
                CheckResult(
                    key="end_to_end",
                    label="远端监听 TCP（目标未验证）",
                    state="unknown",
                    detail="远端端口未监听",
                )
            )

        snapshot.network_ok = dns.state == "ok" and len(snapshot.checks) > 2 and (
            snapshot.check("target_tcp") is not None
            and snapshot.check("target_tcp").state == "ok"
            and ssh_ok
        )
        return snapshot

