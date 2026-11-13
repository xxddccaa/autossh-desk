from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import partial
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app.diagnostics import CommandResult, DiagnosticSnapshot
from backend.app.main import create_app
from backend.app.models import Tunnel
from backend.app.processes import ProcessRecord, marker_for
from backend.app.schemas import CheckResult, ConflictCleanRequest, RemoteListener


def snapshot(state: str) -> DiagnosticSnapshot:
    found = state in {"conflict", "healthy", "unknown_probe"}
    ssh_failed = state in {"ssh_transient", "auth_error"}
    listener = RemoteListener(
        found=found,
        address="127.0.0.1",
        port=15432,
        pids=[456] if found else [],
        processes=[
            {"pid": 456, "ppid": 1, "user": "root", "comm": "sshd", "is_sshd": True}
        ] if found else [],
        raw='LISTEN 127.0.0.1:15432 users:(("sshd",pid=456,fd=3))' if found else "",
    )
    return DiagnosticSnapshot(
        checks=[
            CheckResult(key="autossh", label="autossh", state="ok"),
            CheckResult(key="dns", label="DNS", state="ok"),
            CheckResult(key="target_tcp", label="目标", state="ok"),
            CheckResult(key="ssh_tcp", label="SSH", state="ok"),
            CheckResult(
                key="ssh_auth",
                label="认证",
                state="fail" if ssh_failed else "ok",
                detail=(
                    "kex_exchange_identification: read: Connection reset by peer"
                    if state == "ssh_transient"
                    else "Permission denied (publickey)" if state == "auth_error" else ""
                ),
            ),
            CheckResult(
                key="remote_listener",
                label="监听",
                state="fail" if state == "listener_error" else "warn" if found else "ok",
                detail="无法检查远端端口" if state == "listener_error" else "",
            ),
        ],
        remote_listener=listener,
        end_to_end_known=found and state != "unknown_probe",
        end_to_end_ok=state == "healthy",
        autossh_path="/opt/homebrew/bin/autossh",
        network_ok=True,
        ssh_auth_ok=not ssh_failed,
        ssh_failure_retryable=state == "ssh_transient",
    )


def stub_connection(app, monkeypatch):
    supervisor = app.state.supervisor
    with app.state.database.session() as session:
        tunnel_id = session.query(Tunnel).first().id
    connection = SimpleNamespace(
        supervisor=supervisor,
        tunnel_id=tunnel_id,
        state="conflict",
        runs=0,
        quick_checks=0,
        spawned=[],
        stopped=[],
    )

    def make_record():
        log_path = supervisor.settings.logs_dir / f"{tunnel_id}.log"
        pid = 10000 + len(connection.spawned)
        return ProcessRecord(
            tunnel_id=tunnel_id,
            autossh_pid=pid,
            pgid=pid,
            started_at="2026-11-05T00:00:00+00:00",
            process_signature="test",
            args_hash="test",
            autossh_path="/opt/homebrew/bin/autossh",
            marker=marker_for(tunnel_id),
            log_offset=log_path.stat().st_size if log_path.exists() else 0,
        )

    async def run(tunnel):
        connection.runs += 1
        state = connection.state
        if state == "free" and tunnel.id in supervisor._records:
            state = "healthy"
        return snapshot(state)

    async def network_ok(_tunnel):
        connection.quick_checks += 1
        return True

    async def spawn(tunnel, _autossh_path):
        record = make_record()
        connection.spawned.append(record)
        supervisor._records[tunnel.id] = record
        return record

    async def stop(record):
        connection.stopped.append(record)
        return True, "受管进程已停止"

    async def unexpected_remote(*_args, **_kwargs):
        raise AssertionError("自动重连不得终止远端会话")

    monkeypatch.setattr(supervisor.diagnostics, "run", run)
    monkeypatch.setattr(supervisor.diagnostics, "quick_network_ok", network_ok)
    monkeypatch.setattr(supervisor.diagnostics, "run_remote", unexpected_remote)
    monkeypatch.setattr(supervisor, "_spawn_autossh", spawn)
    monkeypatch.setattr(supervisor, "_read_network_path", lambda: "test")
    monkeypatch.setattr("backend.app.supervisor.find_ssh_pid", lambda record: record.autossh_pid + 1)
    monkeypatch.setattr(
        "backend.app.supervisor.verify_record",
        lambda record: supervisor._records.get(record.tunnel_id) == record,
    )
    monkeypatch.setattr("backend.app.supervisor.stop_record", stop)
    connection.make_record = make_record
    return connection


@pytest.fixture
def connection(client, monkeypatch):
    return stub_connection(client.app, monkeypatch)


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=datetime(2026, 11, 5, tzinfo=timezone.utc))
    monkeypatch.setattr("backend.app.supervisor.utc_now", lambda: clock.now)
    return clock


@pytest.fixture
def manual_conflict(connection, monkeypatch):
    listener = snapshot("conflict").remote_listener
    probe = SimpleNamespace(value=False, commands=[])

    async def remote_listener(_tunnel):
        return listener, None

    async def end_to_end(_tunnel):
        return probe.value, "probe"

    async def run_remote(_tunnel, command, timeout=12):
        probe.commands.append(command)
        connection.state = "free"
        return CommandResult(0, "", "")

    monkeypatch.setattr(connection.supervisor.diagnostics, "remote_listener", remote_listener)
    monkeypatch.setattr(connection.supervisor.diagnostics, "end_to_end", end_to_end)
    monkeypatch.setattr(connection.supervisor.diagnostics, "run_remote", run_remote)
    return probe


def monitor(client, connection, *, force=False):
    client.portal.call(
        partial(connection.supervisor._monitor_tunnel, connection.tunnel_id, force=force)
    )


@pytest.mark.parametrize(
    "origin",
    ["preflight", "diagnose", "managed_log", "managed_probe", "managed_diagnose", "process_exit"],
)
def test_all_conflict_paths_retry_and_reconnect(client, connection, clock, origin):
    supervisor = connection.supervisor
    tunnel_id = connection.tunnel_id
    if origin == "preflight":
        client.post(f"/api/tunnels/{tunnel_id}/start")
    elif origin == "diagnose":
        client.post(f"/api/tunnels/{tunnel_id}/test")
    else:
        record = connection.make_record()
        supervisor._records[tunnel_id] = record
        supervisor._active.add(tunnel_id)
        supervisor._runtime(tunnel_id).state = "connected"
        if origin in {"managed_log", "process_exit"}:
            (supervisor.settings.logs_dir / f"{tunnel_id}.log").write_text(
                "remote port forwarding failed for listen port 15432\n", encoding="utf-8"
            )
        if origin == "process_exit":
            client.portal.call(supervisor._handle_process_exit, tunnel_id, record, 255)
        elif origin == "managed_diagnose":
            client.post(f"/api/tunnels/{tunnel_id}/test")
        else:
            client.portal.call(supervisor._check_managed, tunnel_id, record)

    runtime = supervisor.status(tunnel_id).runtime
    assert runtime.state == "port_conflict"
    assert runtime.next_retry_at == clock.now + timedelta(seconds=1)
    assert tunnel_id in supervisor._active
    assert runtime.external_tunnel is False
    assert runtime.managed is False
    assert connection.spawned == []

    calls = connection.runs
    monitor(client, connection)
    assert connection.runs == calls
    assert connection.quick_checks == 0

    clock.now = runtime.next_retry_at
    monitor(client, connection)
    runtime = supervisor.status(tunnel_id).runtime
    assert runtime.state == "port_conflict"
    assert runtime.next_retry_at == clock.now + timedelta(seconds=2)
    assert tunnel_id in supervisor._active
    assert connection.spawned == []

    connection.state = "free"
    clock.now = runtime.next_retry_at
    monitor(client, connection)
    runtime = supervisor.status(tunnel_id).runtime
    assert runtime.state == "starting"
    assert runtime.managed is True
    assert runtime.conflict is None
    assert runtime.next_retry_at is None
    assert len(connection.spawned) == 1

    monitor(client, connection)
    assert supervisor.status(tunnel_id).runtime.state == "connected"
    monitor(client, connection)
    assert len(connection.spawned) == 1


def test_conflict_never_reuses_tcp_reachable_listener(client, connection, clock):
    client.post(f"/api/tunnels/{connection.tunnel_id}/start")
    connection.state = "healthy"
    clock.now = connection.supervisor.status(connection.tunnel_id).runtime.next_retry_at
    monitor(client, connection)
    runtime = connection.supervisor.status(connection.tunnel_id).runtime
    assert runtime.state == "port_conflict"
    assert runtime.external_tunnel is False
    assert runtime.next_retry_at is not None
    assert runtime.conflict is not None
    assert connection.spawned == []


@pytest.mark.parametrize("origin", ["preflight", "diagnose", "legacy_external"])
def test_unmanaged_tcp_listener_is_not_reused(client, connection, origin):
    connection.state = "healthy"
    supervisor = connection.supervisor
    tunnel_id = connection.tunnel_id
    if origin == "preflight":
        client.post(f"/api/tunnels/{tunnel_id}/start")
    elif origin == "diagnose":
        client.post(f"/api/tunnels/{tunnel_id}/test")
    else:
        runtime = supervisor._runtime(tunnel_id)
        runtime.state = "connected"
        runtime.external_tunnel = True
        supervisor._active.add(tunnel_id)
        monitor(client, connection)
    runtime = supervisor.status(tunnel_id).runtime
    assert runtime.state == "port_conflict"
    assert runtime.external_tunnel is False
    assert runtime.managed is False
    assert runtime.next_retry_at is not None
    assert connection.spawned == []


def test_managed_listener_without_ssh_child_is_not_connected(client, connection, monkeypatch):
    supervisor = connection.supervisor
    record = connection.make_record()
    supervisor._records[connection.tunnel_id] = record
    supervisor._active.add(connection.tunnel_id)
    connection.state = "healthy"
    monkeypatch.setattr("backend.app.supervisor.find_ssh_pid", lambda _record: None)
    client.portal.call(supervisor._check_managed, connection.tunnel_id, record)
    assert supervisor.status(connection.tunnel_id).runtime.state == "reconnecting"
    assert next(
        check for check in supervisor.status(connection.tunnel_id).runtime.checks
        if check.key == "remote_listener"
    ).state == "warn"


@pytest.mark.parametrize("origin", ["monitor", "diagnose"])
def test_connected_managed_listener_check_is_successful(client, connection, origin):
    supervisor = connection.supervisor
    connection.state = "free"
    client.post(f"/api/tunnels/{connection.tunnel_id}/start")
    if origin == "monitor":
        monitor(client, connection)
    else:
        client.post(f"/api/tunnels/{connection.tunnel_id}/test")
    runtime = supervisor.status(connection.tunnel_id).runtime
    assert runtime.state == "connected"
    listener_check = next(check for check in runtime.checks if check.key == "remote_listener")
    assert listener_check.state == "ok"
    assert "受管" in listener_check.detail
    assert "456" in listener_check.detail


def test_unmanaged_listener_check_remains_a_warning(client, connection):
    connection.state = "healthy"
    client.post(f"/api/tunnels/{connection.tunnel_id}/start")
    runtime = connection.supervisor.status(connection.tunnel_id).runtime
    assert runtime.state == "port_conflict"
    listener_check = next(check for check in runtime.checks if check.key == "remote_listener")
    assert listener_check.state == "warn"
    assert connection.stopped == []


def test_cleanup_allows_confirmed_tcp_reachable_unmanaged_listener(
    client, connection, manual_conflict
):
    tunnel_id = connection.tunnel_id
    manual_conflict.value = True
    preview_response = client.post(f"/api/tunnels/{tunnel_id}/conflict/preview")
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["can_clean"] is True
    assert preview["end_to_end_ok"] is True
    response = client.post(
        f"/api/tunnels/{tunnel_id}/conflict/clean",
        json={"fingerprint": preview["fingerprint"], "confirm": True},
    )
    assert response.status_code == 200
    assert len(manual_conflict.commands) == 1


def test_conflict_backoff_is_bounded_without_giving_up(client, connection, clock):
    client.post(f"/api/tunnels/{connection.tunnel_id}/start")
    for delay in (1, 2, 5, 10, 20, 30, 60, 60, 60):
        runtime = connection.supervisor.status(connection.tunnel_id).runtime
        assert runtime.next_retry_at == clock.now + timedelta(seconds=delay)
        calls = connection.runs
        deadline = runtime.next_retry_at
        clock.now = deadline - timedelta(milliseconds=1)
        monitor(client, connection)
        assert connection.runs == calls
        clock.now = deadline
        monitor(client, connection)
        assert connection.runs == calls + 1
        assert connection.tunnel_id in connection.supervisor._active
    assert connection.quick_checks == 0
    assert connection.spawned == []


def test_network_change_can_retry_conflict_early(client, connection, clock):
    client.post(f"/api/tunnels/{connection.tunnel_id}/start")
    connection.state = "free"
    monitor(client, connection, force=True)
    assert len(connection.spawned) == 1


@pytest.mark.parametrize("pause", ["manual_stop", "disabled"])
def test_conflict_retry_respects_user_pause(client, connection, clock, pause):
    tunnel_id = connection.tunnel_id
    if pause == "disabled":
        client.patch(f"/api/tunnels/{tunnel_id}", json={"auto_reconnect": False})
    client.post(f"/api/tunnels/{tunnel_id}/start")
    if pause == "manual_stop":
        client.post(f"/api/tunnels/{tunnel_id}/stop")
    client.post(f"/api/tunnels/{tunnel_id}/test")
    runtime = connection.supervisor.status(tunnel_id).runtime
    assert runtime.next_retry_at is None
    assert tunnel_id not in connection.supervisor._active

    connection.state = "free"
    clock.now += timedelta(minutes=5)
    monitor(client, connection)
    client.portal.call(connection.supervisor._monitor_idle, tunnel_id)
    assert connection.spawned == []
    assert connection.quick_checks == 0

    if pause == "manual_stop":
        connection.state = "conflict"
        response = client.post(f"/api/tunnels/{tunnel_id}/start")
        assert response.json()["runtime"]["next_retry_at"] is not None
        assert tunnel_id in connection.supervisor._active


@pytest.mark.parametrize("lost_state", ["free", "conflict", "unknown_probe", "listener_error"])
def test_unmanaged_listener_release_recovers_automatically(client, connection, clock, lost_state):
    connection.state = "healthy"
    client.post(f"/api/tunnels/{connection.tunnel_id}/start")
    clock.now = connection.supervisor.status(connection.tunnel_id).runtime.next_retry_at
    connection.state = lost_state
    monitor(client, connection)
    runtime = connection.supervisor.status(connection.tunnel_id).runtime
    assert runtime.external_tunnel is False
    assert connection.tunnel_id in connection.supervisor._active

    if lost_state != "free":
        assert runtime.next_retry_at is not None
        assert connection.spawned == []
        clock.now = runtime.next_retry_at
        connection.state = "free"
        monitor(client, connection)
    monitor(client, connection)
    assert connection.supervisor.status(connection.tunnel_id).runtime.state == "connected"
    assert len(connection.spawned) == 1


def test_unmanaged_listener_release_respects_disabled_reconnect(client, connection):
    tunnel_id = connection.tunnel_id
    client.patch(f"/api/tunnels/{tunnel_id}", json={"auto_reconnect": False})
    connection.state = "healthy"
    client.post(f"/api/tunnels/{tunnel_id}/start")
    connection.state = "free"
    monitor(client, connection)
    runtime = connection.supervisor.status(tunnel_id).runtime
    assert runtime.state == "port_conflict"
    assert runtime.next_retry_at is None
    assert tunnel_id not in connection.supervisor._active
    assert connection.spawned == []


def test_unknown_probe_keeps_checking_without_spawning(client, connection, clock):
    connection.state = "unknown_probe"
    client.post(f"/api/tunnels/{connection.tunnel_id}/start")
    runtime = connection.supervisor.status(connection.tunnel_id).runtime
    assert runtime.state == "error"
    assert runtime.next_retry_at is not None
    assert runtime.external_tunnel is False
    clock.now = runtime.next_retry_at
    monitor(client, connection)
    assert connection.supervisor.status(connection.tunnel_id).runtime.next_retry_at is not None
    assert connection.spawned == []


def test_failed_conflict_stop_keeps_tracking_process(client, connection, clock, monkeypatch):
    supervisor = connection.supervisor
    record = connection.make_record()
    supervisor._records[connection.tunnel_id] = record
    supervisor._active.add(connection.tunnel_id)

    async def failed_stop(_record):
        return False, "停止失败"

    monkeypatch.setattr("backend.app.supervisor.stop_record", failed_stop)
    client.portal.call(supervisor._check_managed, connection.tunnel_id, record)
    runtime = supervisor.status(connection.tunnel_id).runtime
    assert runtime.managed is True
    assert runtime.autossh_pid == record.autossh_pid
    assert connection.tunnel_id in supervisor._active
    assert runtime.next_retry_at is not None
    clock.now = runtime.next_retry_at
    monitor(client, connection)
    assert connection.spawned == []
    assert supervisor._records[connection.tunnel_id] == record


def test_temporary_diagnostic_error_does_not_abandon_connection(
    client, connection, clock, monkeypatch
):
    supervisor = connection.supervisor
    original_run = supervisor.diagnostics.run
    failed = False

    async def flaky_run(tunnel):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("temporary diagnostic failure")
        return await original_run(tunnel)

    monkeypatch.setattr(supervisor.diagnostics, "run", flaky_run)
    response = client.post(f"/api/tunnels/{connection.tunnel_id}/start")
    assert response.status_code == 200
    runtime = supervisor.status(connection.tunnel_id).runtime
    assert runtime.next_retry_at is not None
    clock.now = runtime.next_retry_at
    connection.state = "free"
    monitor(client, connection)
    assert len(connection.spawned) == 1


@pytest.mark.parametrize("origin", ["preflight", "diagnose", "managed", "managed_diagnose"])
def test_transient_ssh_failure_retries_without_abandoning_tunnel(client, connection, clock, origin):
    supervisor = connection.supervisor
    tunnel_id = connection.tunnel_id
    connection.state = "ssh_transient"
    managed = origin.startswith("managed")
    if managed:
        record = connection.make_record()
        supervisor._records[tunnel_id] = record
        supervisor._active.add(tunnel_id)
    if origin == "preflight":
        client.post(f"/api/tunnels/{tunnel_id}/start")
    elif origin == "managed":
        monitor(client, connection)
    else:
        client.post(f"/api/tunnels/{tunnel_id}/test")

    runtime = supervisor.status(tunnel_id).runtime
    assert runtime.state == "reconnecting"
    assert runtime.next_retry_at == clock.now + timedelta(seconds=1)
    assert "认证失败" not in runtime.last_error
    assert tunnel_id in supervisor._active
    assert connection.stopped == []
    runs = connection.runs
    monitor(client, connection)
    assert connection.runs == runs
    assert connection.quick_checks == 0

    clock.now = runtime.next_retry_at
    monitor(client, connection)
    runtime = supervisor.status(tunnel_id).runtime
    assert runtime.next_retry_at == clock.now + timedelta(seconds=2)
    connection.state = "free"
    clock.now = runtime.next_retry_at
    monitor(client, connection)
    monitor(client, connection)
    runtime = supervisor.status(tunnel_id).runtime
    assert runtime.state == "connected"
    assert runtime.managed is True
    assert runtime.next_retry_at is None
    assert len(connection.spawned) == (0 if managed else 1)
    assert connection.stopped == []


def test_transient_ssh_failures_keep_retrying_at_backoff_limit(client, connection, clock):
    connection.state = "ssh_transient"
    client.post(f"/api/tunnels/{connection.tunnel_id}/start")
    for delay in (1, 2, 5, 10, 20, 30, 60, 60, 60):
        runtime = connection.supervisor.status(connection.tunnel_id).runtime
        assert runtime.next_retry_at == clock.now + timedelta(seconds=delay)
        assert connection.tunnel_id in connection.supervisor._active
        clock.now = runtime.next_retry_at
        monitor(client, connection)
    assert connection.spawned == []
    assert connection.stopped == []


@pytest.mark.parametrize("pause", ["manual_stop", "disabled"])
def test_transient_ssh_retry_respects_user_pause(client, connection, clock, pause):
    tunnel_id = connection.tunnel_id
    connection.state = "ssh_transient"
    client.post(f"/api/tunnels/{tunnel_id}/start")
    assert connection.supervisor.status(tunnel_id).runtime.next_retry_at is not None
    if pause == "manual_stop":
        client.post(f"/api/tunnels/{tunnel_id}/stop")
    else:
        client.patch(f"/api/tunnels/{tunnel_id}", json={"auto_reconnect": False})
    connection.state = "free"
    clock.now += timedelta(minutes=2)
    monitor(client, connection, force=True)
    client.portal.call(connection.supervisor._monitor_idle, tunnel_id)
    assert connection.supervisor.status(tunnel_id).runtime.next_retry_at is None
    assert connection.spawned == []


@pytest.mark.parametrize("origin", ["preflight", "managed"])
def test_explicit_auth_rejection_still_requires_attention(client, connection, origin):
    supervisor = connection.supervisor
    tunnel_id = connection.tunnel_id
    connection.state = "auth_error"
    if origin == "managed":
        record = connection.make_record()
        supervisor._records[tunnel_id] = record
        supervisor._active.add(tunnel_id)
        monitor(client, connection)
        assert connection.stopped == [record]
    else:
        client.post(f"/api/tunnels/{tunnel_id}/start")
    runtime = supervisor.status(tunnel_id).runtime
    assert runtime.state == "error"
    assert runtime.next_retry_at is None
    assert tunnel_id not in supervisor._active
    connection.state = "free"
    client.portal.call(supervisor._monitor_idle, tunnel_id)
    assert connection.spawned == []


@pytest.mark.parametrize("recovered_probe", [True, None])
def test_cleanup_rechecks_health_before_signalling(
    client, connection, manual_conflict, recovered_probe
):
    tunnel_id = connection.tunnel_id
    client.post(f"/api/tunnels/{tunnel_id}/start")
    preview = client.post(f"/api/tunnels/{tunnel_id}/conflict/preview").json()
    assert preview["can_clean"] is True
    manual_conflict.value = recovered_probe
    response = client.post(
        f"/api/tunnels/{tunnel_id}/conflict/clean",
        json={"fingerprint": preview["fingerprint"], "confirm": True},
    )
    assert response.status_code == 409
    assert manual_conflict.commands == []


def test_cleanup_cancels_when_reachable_listener_becomes_unreachable(
    client, connection, manual_conflict
):
    tunnel_id = connection.tunnel_id
    manual_conflict.value = True
    preview = client.post(f"/api/tunnels/{tunnel_id}/conflict/preview").json()
    manual_conflict.value = False
    response = client.post(
        f"/api/tunnels/{tunnel_id}/conflict/clean",
        json={"fingerprint": preview["fingerprint"], "confirm": True},
    )
    assert response.status_code == 409
    assert manual_conflict.commands == []


def test_cleanup_requires_exact_listener_address(client, connection, manual_conflict, monkeypatch):
    listener = snapshot("healthy").remote_listener
    listener.raw = listener.raw.replace("127.0.0.1:15432", "127.0.0.2:15432")

    async def remote_listener(_tunnel):
        return listener, None

    monkeypatch.setattr(connection.supervisor.diagnostics, "remote_listener", remote_listener)
    manual_conflict.value = True
    response = client.post(f"/api/tunnels/{connection.tunnel_id}/conflict/preview")
    assert response.status_code == 200
    assert response.json()["can_clean"] is False
    assert manual_conflict.commands == []


@pytest.mark.parametrize("origin", ["preflight", "diagnose", "managed"])
def test_configured_ssh_forwarding_prevents_start(client, connection, monkeypatch, origin):
    supervisor = connection.supervisor
    checked = snapshot("free")
    checked.checks.append(CheckResult(key="ssh_forwarding", label="转发隔离", state="fail", detail="SSH 别名包含额外转发"))

    async def run(_tunnel):
        return checked

    monkeypatch.setattr(supervisor.diagnostics, "run", run)
    if origin == "preflight":
        client.post(f"/api/tunnels/{connection.tunnel_id}/start")
    elif origin == "diagnose":
        client.post(f"/api/tunnels/{connection.tunnel_id}/test")
    else:
        record = connection.make_record()
        supervisor._records[connection.tunnel_id] = record
        supervisor._active.add(connection.tunnel_id)
        client.portal.call(supervisor._check_managed, connection.tunnel_id, record)
        assert connection.stopped == [record]
    runtime = supervisor.status(connection.tunnel_id).runtime
    assert runtime.state == "error"
    assert runtime.next_retry_at is None
    assert connection.tunnel_id not in supervisor._active
    assert connection.spawned == []


def test_forward_conflict_during_diagnostics_is_not_reported_connected(
    client, connection, monkeypatch
):
    supervisor = connection.supervisor
    record = connection.make_record()
    supervisor._records[connection.tunnel_id] = record
    supervisor._active.add(connection.tunnel_id)

    async def run(_tunnel):
        (supervisor.settings.logs_dir / f"{connection.tunnel_id}.log").write_text(
            "remote port forwarding failed for listen port 15432\n", encoding="utf-8"
        )
        return snapshot("healthy")

    monkeypatch.setattr(supervisor.diagnostics, "run", run)
    client.portal.call(supervisor._check_managed, connection.tunnel_id, record)
    assert supervisor.status(connection.tunnel_id).runtime.state == "port_conflict"
    assert connection.stopped == [record]


@pytest.mark.parametrize("pause", [None, "manual_stop", "disabled"])
def test_confirmed_cleanup_automatically_reconnects_unless_paused(
    client, connection, manual_conflict, clock, pause
):
    tunnel_id = connection.tunnel_id
    client.post(f"/api/tunnels/{tunnel_id}/start")
    if pause == "manual_stop":
        client.post(f"/api/tunnels/{tunnel_id}/stop")
    elif pause == "disabled":
        client.patch(f"/api/tunnels/{tunnel_id}", json={"auto_reconnect": False})
    preview = client.post(f"/api/tunnels/{tunnel_id}/conflict/preview").json()
    response = client.post(
        f"/api/tunnels/{tunnel_id}/conflict/clean",
        json={"fingerprint": preview["fingerprint"], "confirm": True},
    )
    assert response.status_code == 200
    assert len(manual_conflict.commands) == 1
    runtime = connection.supervisor.status(tunnel_id).runtime
    assert runtime.conflict is None
    if pause is None:
        assert runtime.next_retry_at == clock.now
        monitor(client, connection)
        assert len(connection.spawned) == 1
    else:
        assert runtime.next_retry_at is None
        monitor(client, connection)
        client.portal.call(connection.supervisor._monitor_idle, tunnel_id)
        assert connection.spawned == []


def test_cleanup_and_background_reconnect_are_serialized(client, connection, monkeypatch):
    supervisor = connection.supervisor
    tunnel_id = connection.tunnel_id
    client.post(f"/api/tunnels/{tunnel_id}/start")

    async def exercise():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_clean(*_args):
            entered.set()
            await release.wait()
            return snapshot("conflict").remote_listener, "清理完成"

        monkeypatch.setattr(supervisor.conflicts, "clean", slow_clean)
        cleanup = asyncio.create_task(
            supervisor.clean_conflict(
                tunnel_id, ConflictCleanRequest(fingerprint="test", confirm=True)
            )
        )
        await entered.wait()
        connection.state = "free"
        retry = asyncio.create_task(supervisor._monitor_tunnel(tunnel_id, force=True))
        await asyncio.sleep(0)
        try:
            assert not retry.done()
            assert connection.spawned == []
        finally:
            release.set()
            await asyncio.gather(cleanup, retry)
        assert len(connection.spawned) == 1

    client.portal.call(exercise)


@pytest.mark.parametrize("failure", ["conflict", "ssh_transient"])
def test_background_monitor_reconnects_without_clicks(test_settings, monkeypatch, failure):
    app = create_app(replace(test_settings, monitor_interval=0.01))
    connection = stub_connection(app, monkeypatch)
    connection.state = failure
    failed = Event()
    connected = Event()
    original_publish = app.state.broker.publish

    def publish(event):
        original_publish(event)
        state = event.get("runtime", {}).get("state")
        if state == ("port_conflict" if failure == "conflict" else "reconnecting"):
            failed.set()
        elif state == "connected":
            connected.set()

    monkeypatch.setattr(app.state.broker, "publish", publish)
    monkeypatch.setattr("backend.app.supervisor.BACKOFF_SECONDS", (0.02, 0.04, 0.06))
    with TestClient(app):
        assert failed.wait(2), "后台监控未检测连接故障"
        connection.state = "free"
        assert connected.wait(2), "故障消失后未自动恢复连接"
        assert len(connection.spawned) == 1
        assert connection.supervisor.status(connection.tunnel_id).runtime.managed is True
