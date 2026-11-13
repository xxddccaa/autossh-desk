from __future__ import annotations

import asyncio
import ssl

import pytest

from backend.app import diagnostics as diagnostics_module
from backend.app.diagnostics import CommandResult, DiagnosticSnapshot, SSHConfig, run_command
from backend.app.schemas import CheckResult, RemoteListener


@pytest.mark.parametrize(
    ("result", "retryable"),
    [
        (CommandResult(255, "", "kex_exchange_identification: read: Connection reset by peer"), True),
        (CommandResult(255, "", "Connection closed by remote host"), True),
        (CommandResult(255, "", "Broken pipe"), True),
        (CommandResult(255, "", ""), True),
        (CommandResult(-15, "", ""), True),
        (CommandResult(124, "", "命令超时", timed_out=True), True),
        (CommandResult(255, "", "Permission denied (publickey)."), False),
        (CommandResult(255, "", "No such identity: /example/missing-key"), False),
        (CommandResult(255, "", "Host key verification failed."), False),
        (CommandResult(255, "", "REMOTE HOST IDENTIFICATION HAS CHANGED!"), False),
        (CommandResult(255, "", "Too many authentication failures"), False),
        (CommandResult(255, "", 'Load key "/example/key": invalid format'), False),
    ],
)
def test_ssh_transport_failures_are_not_permanent_auth_errors(app, monkeypatch, result, retryable):
    diagnostics = app.state.diagnostics
    tunnel = app.state.supervisor.list_tunnels()[0]

    async def dns_check(_host):
        return CheckResult(key="dns", label="DNS", state="ok")

    async def resolve(_tunnel):
        return SSHConfig(hostname="remote.example.com", port=22), None

    async def tcp_check(*_args):
        return True, 1, "available"

    async def run_remote(_tunnel, command, **_kwargs):
        assert command == "true"
        return result

    monkeypatch.setattr(diagnostics, "find_autossh", lambda: "autossh")
    monkeypatch.setattr(diagnostics, "_dns_check", dns_check)
    monkeypatch.setattr(diagnostics, "resolve_ssh", resolve)
    monkeypatch.setattr(diagnostics, "_tcp_check", tcp_check)
    monkeypatch.setattr(diagnostics, "run_remote", run_remote)
    checked = asyncio.run(diagnostics.run(tunnel))
    assert checked.ssh_failure_retryable is retryable
    assert checked.ssh_auth_ok is False
    assert checked.preflight_ok is False
    assert checked.check("ssh_tcp").state == "ok"
    assert checked.check("ssh_auth").state == "fail"
    assert checked.check("target_tcp").label == "目标服务端口"
    if retryable:
        assert "认证失败" not in checked.error
        assert checked.check("ssh_auth").label == "SSH 连接"


def test_wrong_http_service_is_not_adopted_even_if_tcp_is_reachable(app, monkeypatch):
    supervisor = app.state.supervisor
    diagnostics = app.state.diagnostics
    tunnel = supervisor.list_tunnels()[0]
    tunnel.target_host = "service.example.com"
    tunnel.target_port = 443

    async def reproduce():
        async def wrong_service(reader, writer):
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        async def local_command(_tunnel, command, timeout=12):
            return await run_command(["/bin/sh", "-c", command], timeout=timeout)

        monkeypatch.setattr(diagnostics, "run_remote", local_command)
        async with await asyncio.start_server(wrong_service, "127.0.0.1", 0) as server:
            tunnel.remote_port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", tunnel.remote_port)
            assert await reader.readline() == b"HTTP/1.1 404 Not Found\r\n"
            writer.close()
            await writer.wait_closed()
            with pytest.raises(ssl.SSLError):
                await asyncio.open_connection(
                    "127.0.0.1", tunnel.remote_port,
                    ssl=ssl.create_default_context(), server_hostname=tunnel.target_host,
                )
            probe_ok, detail = await diagnostics.end_to_end(tunnel)
            assert probe_ok is True
            assert "未验证转发目标" in detail
            checked = DiagnosticSnapshot(
                checks=[
                    CheckResult(key=key, label=key, state="ok")
                    for key in ("autossh", "dns", "target_tcp", "ssh_tcp", "ssh_auth", "remote_listener")
                ],
                remote_listener=RemoteListener(found=True, address="127.0.0.1", port=tunnel.remote_port),
                end_to_end_known=True,
                end_to_end_ok=True,
                autossh_path="autossh",
                ssh_auth_ok=True,
                network_ok=True,
            )

            async def checked_run(_tunnel):
                return checked

            async def unexpected_spawn(*_args):
                raise AssertionError("不能在非受管监听占用时启动或复用隧道")

            monkeypatch.setattr(diagnostics, "run", checked_run)
            monkeypatch.setattr(supervisor, "_spawn_autossh", unexpected_spawn)
            result = await supervisor._attempt_start(tunnel)
            assert result.started is False
            assert result.runtime.state == "port_conflict"
            assert result.runtime.external_tunnel is False
            assert result.runtime.managed is False
            assert result.runtime.next_retry_at is not None

    asyncio.run(reproduce())


@pytest.mark.parametrize(
    ("forwarding", "expected_key"),
    [
        ("RemoteForward 127.0.0.1:15432 service.example.com:443", "remoteforward"),
        ("LocalForward 127.0.0.1:15432 service.example.com:443", "localforward"),
        ("DynamicForward 127.0.0.1:15432", "dynamicforward"),
    ],
)
def test_configured_forwarding_blocks_managed_start(app, monkeypatch, tmp_path, forwarding, expected_key):
    diagnostics = app.state.diagnostics
    tunnel = app.state.supervisor.list_tunnels()[0]
    tunnel.ssh_target = "tunnel-host"
    tunnel.identity_file = None
    config_path = tmp_path / "ssh_config"
    config_path.write_text(
        f"Host tunnel-host\n  HostName 127.0.0.1\n  User tester\n"
        f"  ClearAllForwardings yes\n  {forwarding}\n"
    )

    async def isolated_command(args, timeout, **kwargs):
        return await run_command([args[0], "-F", str(config_path), *args[1:]], timeout, **kwargs)

    async def dns_check(_host):
        return CheckResult(key="dns", label="DNS", state="ok")

    async def tcp_check(*_args):
        return True, 1.0, "available"

    async def unexpected_remote(*_args, **_kwargs):
        raise AssertionError("带有额外转发的别名不得用于启动隧道")

    monkeypatch.setattr(diagnostics_module, "run_command", isolated_command)
    monkeypatch.setattr(diagnostics, "find_autossh", lambda: "autossh")
    monkeypatch.setattr(diagnostics, "_dns_check", dns_check)
    monkeypatch.setattr(diagnostics, "_tcp_check", tcp_check)
    monkeypatch.setattr(diagnostics, "run_remote", unexpected_remote)
    resolved, error = asyncio.run(diagnostics.resolve_ssh(tunnel))
    assert error is None
    assert expected_key in resolved.configured_forwards
    checked = asyncio.run(diagnostics.run(tunnel))
    assert checked.check("ssh_forwarding").state == "fail"


def test_diagnostic_ssh_ignores_multiplexing_and_background_config(app, tmp_path):
    diagnostics = app.state.diagnostics
    tunnel = app.state.supervisor.list_tunnels()[0]
    tunnel.ssh_target = "tunnel-host"
    config_path = tmp_path / "ssh_config"
    config_path.write_text(
        "Host tunnel-host\n  HostName 127.0.0.1\n  User tester\n  ControlMaster auto\n"
        f"  ControlPath {tmp_path / 'mux'}\n  ControlPersist yes\n"
        "  ForkAfterAuthentication yes\n  RemoteCommand echo ignored\n"
        "  RemoteForward 127.0.0.1:15432 service.example.com:443\n"
    )
    args = diagnostics.ssh_args(tunnel)
    result = asyncio.run(run_command([args[0], "-G", "-F", str(config_path), *args[1:]], timeout=8))
    assert result.ok
    lines = result.stdout.splitlines()
    assert "clearallforwardings yes" in lines
    assert "controlmaster false" in lines
    assert "controlpersist no" in lines
    assert "forkafterauthentication no" in lines
    assert all(line == "remotecommand none" for line in lines if line.startswith("remotecommand "))
