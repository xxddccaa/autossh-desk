from __future__ import annotations

import subprocess
from dataclasses import replace
from threading import Event

import pytest
from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.database import Database
from backend.app.diagnostics import CommandResult, DiagnosticSnapshot
from backend.app.main import create_app
from backend.app.models import Tunnel
from backend.app.processes import (
    ProcessRecord,
    marker_for,
    process_signature,
    save_record,
)
from backend.app.schemas import CheckResult, RemoteListener


def tunnel_payload(**overrides):
    payload = {
        "name": "测试隧道",
        "ssh_target": "root@remote.example.com",
        "ssh_port": None,
        "remote_bind_host": "127.0.0.1",
        "remote_port": 16001,
        "target_host": "postgres.example.com.example",
        "target_port": 5432,
        "identity_file": None,
        "server_alive_interval": 30,
        "server_alive_count_max": 3,
        "connect_timeout": 10,
        "auto_reconnect": True,
        "auto_start": False,
    }
    payload.update(overrides)
    return payload


def checks(*, dns="ok", target="ok", ssh="ok", auth="ok", listener="ok", e2e="unknown"):
    return [
        CheckResult(key="autossh", label="autossh", state="ok", detail="/opt/homebrew/bin/autossh"),
        CheckResult(key="dns", label="DNS", state=dns, detail="192.0.2.22"),
        CheckResult(key="target_tcp", label="目标端口", state=target, detail="target"),
        CheckResult(key="ssh_tcp", label="SSH 端口", state=ssh, detail="ssh"),
        CheckResult(
            key="ssh_auth",
            label="SSH 认证",
            state=auth,
            detail="Permission denied (publickey)",
        ),
        CheckResult(key="remote_listener", label="远端监听", state=listener, detail="listener"),
        CheckResult(key="end_to_end", label="端到端", state=e2e, detail="e2e"),
    ]


def test_tunnel_crud_duplicate_and_uniqueness(client: TestClient):
    initial = client.get("/api/tunnels")
    assert initial.status_code == 200
    assert len(initial.json()) == 1

    created = client.post("/api/tunnels", json=tunnel_payload())
    assert created.status_code == 201, created.text
    created_body = created.json()
    tunnel_id = created_body["tunnel"]["id"]
    assert created_body["runtime"]["state"] == "stopped"

    conflicting = client.post("/api/tunnels", json=tunnel_payload(name="重复端口"))
    assert conflicting.status_code == 409

    patched = client.patch(
        f"/api/tunnels/{tunnel_id}",
        json={"name": "已编辑隧道", "server_alive_interval": 45},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["tunnel"]["name"] == "已编辑隧道"
    assert patched.json()["tunnel"]["server_alive_interval"] == 45

    duplicated = client.post(f"/api/tunnels/{tunnel_id}/duplicate")
    assert duplicated.status_code == 201, duplicated.text
    duplicate_body = duplicated.json()["tunnel"]
    assert duplicate_body["id"] != tunnel_id
    assert duplicate_body["remote_port"] != 16001
    assert duplicate_body["auto_start"] is False
    with client.app.state.database.session() as session:
        assert session.get(Tunnel, duplicate_body["id"]).manually_stopped is True

    deleted = client.delete(f"/api/tunnels/{tunnel_id}")
    assert deleted.status_code == 204
    assert client.get(f"/api/tunnels/{tunnel_id}").status_code == 404


def test_invalid_tunnel_input_is_rejected(client: TestClient):
    response = client.post("/api/tunnels", json=tunnel_payload(remote_port=70000))
    assert response.status_code == 422
    response = client.post("/api/tunnels", json=tunnel_payload(ssh_target="-oProxyCommand=bad"))
    assert response.status_code == 422
    response = client.post(
        "/api/tunnels",
        json=tunnel_payload(password="must-not-be-stored"),
    )
    assert response.status_code == 422


def test_mutating_api_requires_local_session_token(test_settings: Settings):
    secure_settings = Settings(
        data_dir=test_settings.data_dir,
        frontend_dist=test_settings.frontend_dist,
        instance_lock=False,
        strict_security=True,
        monitor_interval=3600,
    )
    app = create_app(secure_settings)
    with TestClient(app, base_url="http://127.0.0.1:8765") as secure_client:
        session = secure_client.get("/api/session")
        assert session.status_code == 200
        token = session.json()["token"]
        rejected = secure_client.post(
            "/api/tunnels",
            json=tunnel_payload(remote_port=16010),
        )
        assert rejected.status_code == 403
        accepted = secure_client.post(
            "/api/tunnels",
            json=tunnel_payload(remote_port=16010),
            headers={"X-AutoSSH-Desk-Token": token},
        )
        assert accepted.status_code == 201, accepted.text
        legacy = secure_client.post(
            "/api/tunnels",
            json=tunnel_payload(remote_port=16011),
            headers={"X-WikiTunnel-Token": token},
        )
        assert legacy.status_code == 201, legacy.text
        foreign_origin = secure_client.get(
            "/api/tunnels",
            headers={"Origin": "https://evil.example"},
        )
        assert foreign_origin.status_code == 403


@pytest.mark.parametrize(
    "scenario", ["healthy", "dns", "target", "auth", "conflict", "unknown_probe"]
)
def test_start_preflight_states(client: TestClient, monkeypatch, scenario: str):
    app = client.app
    supervisor = app.state.supervisor
    tunnel_id = client.get("/api/tunnels").json()[0]["tunnel"]["id"]

    async def fake_run(_tunnel):
        if scenario == "healthy":
            listener = RemoteListener(
                found=True,
                address="127.0.0.1",
                port=15432,
                pids=[123],
                processes=[],
                raw="LISTEN 127.0.0.1:15432 users:((\"sshd\",pid=123,fd=3))",
            )
            return DiagnosticSnapshot(
                checks=checks(listener="warn", e2e="ok"),
                remote_listener=listener,
                end_to_end_ok=True,
                end_to_end_known=True,
                autossh_path="/opt/homebrew/bin/autossh",
                network_ok=True,
                ssh_auth_ok=True,
            )
        if scenario == "target":
            return DiagnosticSnapshot(
                checks=checks(target="fail", ssh="unknown", auth="unknown", listener="unknown", e2e="unknown"),
                autossh_path="/opt/homebrew/bin/autossh",
                network_ok=False,
                error="目标不可达",
            )
        if scenario == "dns":
            return DiagnosticSnapshot(
                checks=checks(
                    dns="fail",
                    target="unknown",
                    ssh="unknown",
                    auth="unknown",
                    listener="unknown",
                    e2e="unknown",
                ),
                autossh_path="/opt/homebrew/bin/autossh",
                network_ok=False,
                error="DNS 解析失败",
            )
        if scenario == "auth":
            return DiagnosticSnapshot(
                checks=checks(auth="fail", listener="unknown", e2e="unknown"),
                autossh_path="/opt/homebrew/bin/autossh",
                network_ok=True,
                ssh_auth_ok=False,
                error="SSH 认证失败",
            )
        if scenario == "unknown_probe":
            listener = RemoteListener(
                found=True,
                address="127.0.0.1",
                port=15432,
                pids=[456],
                processes=[],
                raw="LISTEN 127.0.0.1:15432 users:((\"sshd\",pid=456,fd=3))",
            )
            return DiagnosticSnapshot(
                checks=checks(listener="warn", e2e="unknown"),
                remote_listener=listener,
                end_to_end_ok=False,
                end_to_end_known=False,
                autossh_path="/opt/homebrew/bin/autossh",
                network_ok=True,
                ssh_auth_ok=True,
                error="无法执行端到端探测",
            )
        listener = RemoteListener(
            found=True,
            address="127.0.0.1",
            port=15432,
            pids=[456],
            processes=[
                {
                    "pid": 456,
                    "ppid": 1,
                    "user": "root",
                    "comm": "sshd",
                    "args": "sshd: root@notty",
                    "is_sshd": True,
                }
            ],
            raw="LISTEN 127.0.0.1:15432 users:((\"sshd\",pid=456,fd=3))",
        )
        return DiagnosticSnapshot(
            checks=checks(listener="warn", e2e="fail"),
            remote_listener=listener,
            end_to_end_ok=False,
            end_to_end_known=True,
            autossh_path="/opt/homebrew/bin/autossh",
            network_ok=True,
            ssh_auth_ok=True,
            error="远端端口冲突",
        )

    monkeypatch.setattr(supervisor.diagnostics, "run", fake_run)
    response = client.post(f"/api/tunnels/{tunnel_id}/start")
    assert response.status_code == 200, response.text
    body = response.json()
    runtime = body["runtime"]
    if scenario == "healthy":
        assert body["started"] is False
        assert runtime["state"] == "port_conflict"
        assert runtime["external_tunnel"] is False
        assert runtime["managed"] is False
        assert runtime["next_retry_at"] is not None
    elif scenario in {"dns", "target"}:
        assert runtime["state"] == "target_unreachable"
        assert runtime["next_retry_at"] is not None
        assert "数据库" not in runtime["last_error"]
        assert "数据库" not in body["message"]
    elif scenario == "auth":
        assert runtime["state"] == "error"
        assert "Permission denied" in runtime["last_error"]
    elif scenario == "conflict":
        assert runtime["state"] == "port_conflict"
        assert runtime["next_retry_at"] is not None
        assert tunnel_id in supervisor._active
        assert body["started"] is False
    else:
        assert runtime["state"] == "error"
        assert "无法执行 TCP 监听探测" in runtime["last_error"]


def test_conflict_preview_and_clean_require_exact_confirmation(
    client: TestClient, monkeypatch
):
    app = client.app
    supervisor = app.state.supervisor
    tunnel_id = client.get("/api/tunnels").json()[0]["tunnel"]["id"]
    listener = RemoteListener(
        found=True,
        address="127.0.0.1",
        port=15432,
        pids=[987],
        processes=[
            {
                "pid": 987,
                "ppid": 1,
                "user": "root",
                "comm": "sshd",
                "args": "sshd: root@notty",
                "is_sshd": True,
            }
        ],
        raw="LISTEN 127.0.0.1:15432 users:((\"sshd\",pid=987,fd=3))",
    )

    async def fake_listener(_tunnel):
        return listener, None

    async def fake_e2e(_tunnel):
        return False, "nc timeout"

    captured_commands: list[str] = []

    async def fake_remote(_tunnel, command, timeout=12):
        captured_commands.append(command)
        return CommandResult(0, "", "")

    monkeypatch.setattr(supervisor.diagnostics, "remote_listener", fake_listener)
    monkeypatch.setattr(supervisor.diagnostics, "end_to_end", fake_e2e)
    monkeypatch.setattr(supervisor.diagnostics, "run_remote", fake_remote)

    preview_response = client.post(f"/api/tunnels/{tunnel_id}/conflict/preview")
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["can_clean"] is True
    assert preview["remote_listener"]["pids"] == [987]
    assert "kill -TERM 987" in preview["manual_command"]

    rejected = client.post(
        f"/api/tunnels/{tunnel_id}/conflict/clean",
        json={"fingerprint": preview["fingerprint"], "confirm": False},
    )
    assert rejected.status_code == 409
    assert captured_commands == []

    cleaned = client.post(
        f"/api/tunnels/{tunnel_id}/conflict/clean",
        json={"fingerprint": preview["fingerprint"], "confirm": True},
    )
    assert cleaned.status_code == 200, cleaned.text
    assert captured_commands
    command = captured_commands[0]
    assert 'pid=987' in command
    assert 'port=15432' in command
    assert 'kill -TERM "$pid"' in command
    assert "pkill" not in command
    assert "killall" not in command


@pytest.mark.parametrize("legacy_marker", [False, True])
def test_existing_managed_process_is_adopted_after_app_restart(
    test_settings: Settings, legacy_marker: bool
):
    app = create_app(test_settings)
    with app.state.database.session() as session:
        tunnel_id = session.query(Tunnel).first().id
    executable = test_settings.data_dir / "autossh"
    executable.symlink_to("/bin/sh")
    marker = f"WIKITUNNEL_PROFILE={tunnel_id}" if legacy_marker else marker_for(tunnel_id)
    process = subprocess.Popen(
        [str(executable), "-c", "read ignored", marker],
        stdin=subprocess.PIPE,
        start_new_session=True,
    )
    signature = process_signature(process.pid)
    assert signature
    save_record(
        test_settings.records_dir,
        ProcessRecord(
            tunnel_id=tunnel_id,
            autossh_pid=process.pid,
            pgid=process.pid,
            started_at="2026-11-02T00:00:00+00:00",
            process_signature=signature,
            args_hash="adopted",
            autossh_path=str(executable),
            marker=marker,
            log_offset=0,
        ),
    )
    try:
        with TestClient(app) as restart_client:
            status = restart_client.get("/api/tunnels").json()[0]
            assert status["runtime"]["managed"] is True
            assert status["runtime"]["autossh_pid"] == process.pid
            duplicate = restart_client.post(f"/api/tunnels/{tunnel_id}/start")
            assert duplicate.status_code == 200
            assert duplicate.json()["started"] is False
            assert duplicate.json()["runtime"]["autossh_pid"] == process.pid
    finally:
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


@pytest.mark.parametrize("diagnosed_offline", [False, True])
def test_idle_tunnel_starts_autossh_when_vpn_recovers(
    client: TestClient, monkeypatch, diagnosed_offline: bool
):
    supervisor = client.app.state.supervisor
    tunnel_id = client.get("/api/tunnels").json()[0]["tunnel"]["id"]
    network_available = False
    spawned: list[str] = []

    async def fake_network(_tunnel):
        return network_available

    async def fake_run(_tunnel):
        if not network_available:
            return DiagnosticSnapshot(
                checks=checks(target="fail"),
                autossh_path="/opt/homebrew/bin/autossh",
                error="目标数据库不可达",
            )
        return DiagnosticSnapshot(
            checks=checks(),
            autossh_path="/opt/homebrew/bin/autossh",
            network_ok=True,
            ssh_auth_ok=True,
        )

    async def fake_spawn(tunnel, _autossh_path):
        spawned.append(tunnel.id)
        return ProcessRecord(
            tunnel_id=tunnel.id,
            autossh_pid=12345,
            pgid=12345,
            started_at="2026-11-04T00:00:00+00:00",
            process_signature="test",
            args_hash="test",
            autossh_path="/opt/homebrew/bin/autossh",
            marker=marker_for(tunnel.id),
        )

    monkeypatch.setattr(supervisor.diagnostics, "quick_network_ok", fake_network)
    monkeypatch.setattr(supervisor.diagnostics, "run", fake_run)
    monkeypatch.setattr(supervisor, "_spawn_autossh", fake_spawn)
    monkeypatch.setattr("backend.app.supervisor.find_ssh_pid", lambda _record: None)

    if diagnosed_offline:
        assert client.post(f"/api/tunnels/{tunnel_id}/test").json()["runtime"]["state"] == "target_unreachable"
    else:
        assert client.get(f"/api/tunnels/{tunnel_id}").json()["runtime"]["state"] == "stopped"
    client.portal.call(supervisor._monitor_idle, tunnel_id)
    assert spawned == []

    network_available = True
    client.portal.call(supervisor._monitor_idle, tunnel_id)
    runtime = client.get(f"/api/tunnels/{tunnel_id}").json()["runtime"]
    assert spawned == [tunnel_id]
    assert runtime["managed"] is True
    assert runtime["state"] == "starting"

    client.portal.call(supervisor._monitor_idle, tunnel_id)
    assert spawned == [tunnel_id]


def test_background_monitor_auto_connects_without_click(test_settings: Settings, monkeypatch):
    settings = replace(test_settings, monitor_interval=0.02)
    app = create_app(settings)
    network_available = Event()
    connected = Event()
    supervisor = app.state.supervisor

    async def fake_network(_tunnel):
        return network_available.is_set()

    async def fake_run(_tunnel):
        managed = _tunnel.id in supervisor._records
        return DiagnosticSnapshot(
            checks=checks(listener="warn" if managed else "ok", e2e="ok" if managed else "unknown"),
            remote_listener=RemoteListener(found=managed),
            autossh_path="/opt/homebrew/bin/autossh",
            network_ok=True,
            ssh_auth_ok=True,
            end_to_end_known=managed,
            end_to_end_ok=managed,
        )

    async def fake_spawn(tunnel, _autossh_path):
        record = ProcessRecord(
            tunnel_id=tunnel.id,
            autossh_pid=12345,
            pgid=12345,
            started_at="2026-11-04T00:00:00+00:00",
            process_signature="test",
            args_hash="test",
            autossh_path="/opt/homebrew/bin/autossh",
            marker=marker_for(tunnel.id),
        )
        supervisor._records[tunnel.id] = record
        return record

    original_publish = app.state.broker.publish

    def publish(event):
        original_publish(event)
        if event.get("runtime", {}).get("state") == "connected":
            connected.set()

    monkeypatch.setattr(app.state.diagnostics, "quick_network_ok", fake_network)
    monkeypatch.setattr(app.state.diagnostics, "run", fake_run)
    monkeypatch.setattr(app.state.broker, "publish", publish)
    monkeypatch.setattr(supervisor, "_spawn_autossh", fake_spawn)
    monkeypatch.setattr("backend.app.supervisor.find_ssh_pid", lambda record: record.autossh_pid + 1)
    monkeypatch.setattr("backend.app.supervisor.verify_record", lambda record: supervisor._records.get(record.tunnel_id) == record)

    with TestClient(app) as client:
        tunnel_id = client.get("/api/tunnels").json()[0]["tunnel"]["id"]
        assert client.get(f"/api/tunnels/{tunnel_id}").json()["runtime"]["state"] == "stopped"
        network_available.set()
        assert connected.wait(2), "VPN 可达后后台监控未自动连接隧道"
        assert client.get(f"/api/tunnels/{tunnel_id}").json()["runtime"]["state"] == "connected"


def test_manual_stop_prevents_auto_connection_after_app_restart(test_settings: Settings, monkeypatch):
    app = create_app(test_settings)
    with TestClient(app) as client:
        tunnel_id = client.get("/api/tunnels").json()[0]["tunnel"]["id"]
        response = client.post(f"/api/tunnels/{tunnel_id}/stop")
        assert response.json()["state"] == "stopped"

    restarted = create_app(test_settings)

    async def unexpected_network_check(_tunnel):
        raise AssertionError("手动停止后不应探测网络")

    monkeypatch.setattr(restarted.state.diagnostics, "quick_network_ok", unexpected_network_check)
    with TestClient(restarted) as client:
        client.portal.call(restarted.state.supervisor._monitor_idle, tunnel_id)
        assert client.get(f"/api/tunnels/{tunnel_id}").json()["runtime"]["state"] == "stopped"

        async def healthy_external(_tunnel):
            return DiagnosticSnapshot(
                checks=checks(listener="warn", e2e="ok"),
                remote_listener=RemoteListener(found=True),
                autossh_path="/opt/homebrew/bin/autossh",
                network_ok=True,
                ssh_auth_ok=True,
                end_to_end_known=True,
                end_to_end_ok=True,
            )

        monkeypatch.setattr(restarted.state.diagnostics, "run", healthy_external)
        started = client.post(f"/api/tunnels/{tunnel_id}/start")
        assert started.json()["runtime"]["state"] == "port_conflict"
        assert started.json()["runtime"]["external_tunnel"] is False
        with restarted.state.database.session() as session:
            assert session.get(Tunnel, tunnel_id).manually_stopped is False


def test_disabled_auto_reconnect_does_not_start_idle_tunnel(client: TestClient, monkeypatch):
    tunnel_id = client.get("/api/tunnels").json()[0]["tunnel"]["id"]
    response = client.patch(f"/api/tunnels/{tunnel_id}", json={"auto_reconnect": False})
    assert response.status_code == 200

    async def unexpected_network_check(_tunnel):
        raise AssertionError("关闭自动重连后不应探测网络")

    supervisor = client.app.state.supervisor
    monkeypatch.setattr(supervisor.diagnostics, "quick_network_ok", unexpected_network_check)
    client.portal.call(supervisor._monitor_idle, tunnel_id)
    assert client.get(f"/api/tunnels/{tunnel_id}").json()["runtime"]["state"] == "stopped"


def test_existing_database_adds_manual_stop_state(test_settings: Settings):
    existing = Database(test_settings.database_path)
    with existing.session() as session:
        existing_id = session.query(Tunnel).first().id
    with existing.engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE tunnels DROP COLUMN manually_stopped")
    existing.engine.dispose()

    migrated = Database(test_settings.database_path)
    with migrated.session() as session:
        tunnel = session.query(Tunnel).first()
        assert tunnel is not None
        assert tunnel.id == existing_id
        assert tunnel.manually_stopped is False
    migrated.engine.dispose()


def test_data_dir_and_environment_preserve_existing_installation(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in (
        "AUTOSSH_DESK_DATA_DIR", "WIKITUNNEL_DATA_DIR",
        "AUTOSSH_DESK_PORT", "WIKITUNNEL_PORT",
    ):
        monkeypatch.delenv(name, raising=False)

    support = tmp_path / "Library" / "Application Support"
    new_data_dir = support / "AutoSSH Desk"
    old_data_dir = support / "WikiTunnel"
    settings = Settings.from_env()
    assert settings.data_dir == new_data_dir
    assert settings.database_path == new_data_dir / "autossh-desk.sqlite3"

    old_data_dir.mkdir(parents=True)
    (old_data_dir / "wikitunnel.sqlite3").touch()
    settings = Settings.from_env()
    assert settings.data_dir == old_data_dir
    assert settings.database_path == old_data_dir / "wikitunnel.sqlite3"

    monkeypatch.setenv("WIKITUNNEL_PORT", "8766")
    assert Settings.from_env().port == 8766
    monkeypatch.setenv("AUTOSSH_DESK_PORT", "8767")
    assert Settings.from_env().port == 8767

    new_data_dir.mkdir(parents=True)
    (new_data_dir / "autossh-desk.sqlite3").touch()
    assert Settings.from_env().data_dir == new_data_dir

