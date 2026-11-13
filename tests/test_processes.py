from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import time
from pathlib import Path
from backend.app.main import InstanceLock
from backend.app.config import Settings
from backend.app.diagnostics import CommandResult, Diagnostics
from backend.app.models import Tunnel
from backend.app.processes import (
    ProcessRecord,
    build_autossh_args,
    load_record,
    marker_for,
    process_signature,
    conflict_in_log,
    read_log,
    save_record,
    stop_record,
    verify_record,
)
from backend.app.redaction import redact_text


def sample_tunnel(tunnel_id: str = "profile-test") -> Tunnel:
    return Tunnel(
        id=tunnel_id,
        name="test",
        ssh_target="root@remote.example.com",
        ssh_port=None,
        remote_bind_host="127.0.0.1",
        remote_port=15432,
        target_host="postgres.example.com",
        target_port=5432,
        identity_file="~/.ssh/id_ed25519",
        server_alive_interval=30,
        server_alive_count_max=3,
        connect_timeout=10,
        auto_reconnect=True,
        auto_start=False,
    )


def make_managed_process(tmp_path: Path, tunnel_id: str):
    executable = tmp_path / "autossh"
    executable.symlink_to("/bin/sh")
    marker = marker_for(tunnel_id)
    process = subprocess.Popen(
        [str(executable), "-c", "read ignored", marker],
        stdin=subprocess.PIPE,
        start_new_session=True,
    )
    signature = process_signature(process.pid)
    assert signature
    record = ProcessRecord(
        tunnel_id=tunnel_id,
        autossh_pid=process.pid,
        pgid=process.pid,
        started_at="2026-01-01T00:00:00+00:00",
        process_signature=signature,
        args_hash="test",
        autossh_path=str(executable),
        marker=marker,
    )
    return process, record


def wait_exit(process: subprocess.Popen, timeout: float = 3) -> int:
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)
        raise AssertionError("进程没有按预期退出")


def test_autossh_args_include_marker_keepalive_and_defaults():
    tunnel = sample_tunnel("abc-123")
    args = build_autossh_args(tunnel, "/opt/homebrew/bin/autossh")
    assert "SetEnv=AUTOSSH_DESK_PROFILE=abc-123" in args
    assert "TCPKeepAlive=yes" in args
    assert "ExitOnForwardFailure=yes" in args
    assert "ControlMaster=no" in args
    assert "ControlPath=none" in args
    assert "ControlPersist=no" in args
    assert "ForkAfterAuthentication=no" in args
    assert "BatchMode=yes" in args
    assert "127.0.0.1:15432:postgres.example.com:5432" in args
    assert args[-1] == "root@remote.example.com"
    assert not any("password" in arg.lower() for arg in args)


def test_diagnostic_ssh_disables_forwarding_and_multiplexing(tmp_path: Path):
    diagnostics = Diagnostics(Settings(data_dir=tmp_path, frontend_dist=tmp_path))
    args = diagnostics.ssh_args(sample_tunnel())
    assert "ClearAllForwardings=yes" in args
    assert "ControlMaster=no" in args
    assert "ControlPath=none" in args
    assert "ControlPersist=no" in args
    assert "ForkAfterAuthentication=no" in args


def test_stop_signals_only_managed_process_group(tmp_path: Path):
    tunnel_id = "stop-test"
    process, record = make_managed_process(tmp_path, tunnel_id)
    decoy = subprocess.Popen(
        ["/usr/bin/python3", "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        assert verify_record(record)
        ok, message = asyncio.run(stop_record(record, timeout=2))
        assert ok, message
        wait_exit(process)
        time.sleep(0.2)
        assert decoy.poll() is None
        assert not verify_record(record)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if decoy.poll() is None:
            decoy.terminate()
            decoy.wait(timeout=2)


def test_stale_pid_never_receives_signal(tmp_path: Path):
    decoy = subprocess.Popen(
        ["/usr/bin/python3", "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    record = ProcessRecord(
        tunnel_id="stale",
        autossh_pid=decoy.pid,
        pgid=decoy.pid,
        started_at="2026-01-01T00:00:00+00:00",
        process_signature="not-the-real-signature",
        args_hash="test",
        autossh_path="/usr/bin/python3",
        marker=marker_for("stale"),
    )
    try:
        ok, message = asyncio.run(stop_record(record, timeout=0.5))
        assert ok is False
        assert "未发送信号" in message
        assert decoy.poll() is None
    finally:
        decoy.terminate()
        decoy.wait(timeout=2)


def test_record_and_log_permissions_and_redaction(tmp_path: Path):
    record = ProcessRecord(
        tunnel_id="permissions",
        autossh_pid=123,
        pgid=123,
        started_at="2026-01-01T00:00:00+00:00",
        process_signature="signature",
        args_hash="hash",
        autossh_path="/opt/homebrew/bin/autossh",
        marker=marker_for("permissions"),
    )
    records_dir = tmp_path / "records"
    save_record(records_dir, record)
    metadata = records_dir / "permissions.json"
    assert stat.S_IMODE(metadata.stat().st_mode) == 0o600
    assert load_record(records_dir, "permissions") == record

    log_path = tmp_path / "logs" / "permissions.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        "password=hunter2\nAuthorization: Bearer abc.def.ghi\n"
        "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret-key-body\n"
        "-----END OPENSSH PRIVATE KEY-----\nnormal line\n",
        encoding="utf-8",
    )
    os.chmod(log_path, 0o600)
    displayed = read_log(log_path)
    assert "hunter2" not in displayed
    assert "abc.def.ghi" not in displayed
    assert "secret-key-body" not in displayed
    assert "normal line" in displayed
    assert "PRIVATE KEY REDACTED" in displayed


def test_new_session_ignores_conflict_from_previous_log(tmp_path: Path):
    log_path = tmp_path / "session.log"
    old_line = b"remote port forwarding failed for listen port 15432\n"
    log_path.write_bytes(old_line)
    offset = log_path.stat().st_size
    assert conflict_in_log(log_path, 0)
    assert not conflict_in_log(log_path, offset)
    with log_path.open("ab") as handle:
        handle.write(b"remote port forwarding failed for listen port 15432\n")
    assert conflict_in_log(log_path, offset)


def test_redaction_handles_passphrase_and_secret_assignments():
    text = "passphrase: super-secret\napi_key = xyz123\nok"
    redacted = redact_text(text)
    assert "super-secret" not in redacted
    assert "xyz123" not in redacted
    assert "<redacted>" in redacted


def test_end_to_end_falls_back_when_nc_is_missing(tmp_path: Path):
    diagnostics = Diagnostics(
        Settings(
            data_dir=tmp_path,
            frontend_dist=tmp_path,
            instance_lock=False,
            strict_security=False,
        )
    )
    captured: list[str] = []

    async def fake_remote(_tunnel, command, timeout=12):
        captured.append(command)
        return CommandResult(0, "", "")

    diagnostics.run_remote = fake_remote  # type: ignore[method-assign]
    ok, detail = asyncio.run(diagnostics.end_to_end(sample_tunnel()))
    assert ok is True
    assert "/dev/tcp/127.0.0.1/15432" in captured[0]

    async def missing_probe(_tunnel, command, timeout=12):
        captured.append(command)
        return CommandResult(127, "", "AUTOSSH_DESK_NO_TCP_PROBE")

    diagnostics.run_remote = missing_probe  # type: ignore[method-assign]
    ok, detail = asyncio.run(diagnostics.end_to_end(sample_tunnel()))
    assert ok is None
    assert "无法执行 TCP 监听探测" in detail


def test_single_instance_lock(tmp_path: Path):
    lock_path = tmp_path / "instance.lock"
    first = InstanceLock(lock_path)
    second = InstanceLock(lock_path)
    first.acquire()
    try:
        try:
            second.acquire()
        except RuntimeError as exc:
            assert "已有实例" in str(exc)
        else:
            raise AssertionError("第二个实例不应获得锁")
    finally:
        first.release()
    second.acquire()
    second.release()

