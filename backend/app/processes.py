from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Tunnel
from .redaction import redact_text


@dataclass(slots=True)
class ProcessRecord:
    tunnel_id: str
    autossh_pid: int
    pgid: int
    started_at: str
    process_signature: str
    args_hash: str
    autossh_path: str
    marker: str
    log_offset: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProcessRecord":
        return cls(
            tunnel_id=str(data["tunnel_id"]),
            autossh_pid=int(data["autossh_pid"]),
            pgid=int(data.get("pgid", data["autossh_pid"])),
            started_at=str(data["started_at"]),
            process_signature=str(data["process_signature"]),
            args_hash=str(data["args_hash"]),
            autossh_path=str(data["autossh_path"]),
            marker=str(data["marker"]),
            log_offset=int(data.get("log_offset", 0)),
        )


def marker_for(tunnel_id: str) -> str:
    return f"AUTOSSH_DESK_PROFILE={tunnel_id}"


def build_autossh_args(tunnel: Tunnel, autossh_path: str) -> list[str]:
    args = [
        autossh_path,
        "-M",
        "0",
        "-NT",
        "-o",
        f"ConnectTimeout={tunnel.connect_timeout}",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        f"ServerAliveInterval={tunnel.server_alive_interval}",
        "-o",
        f"ServerAliveCountMax={tunnel.server_alive_count_max}",
        "-o",
        "TCPKeepAlive=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "ControlPersist=no",
        "-o",
        "ForkAfterAuthentication=no",
        "-o",
        "ClearAllForwardings=no",
        "-o",
        f"SetEnv={marker_for(tunnel.id)}",
    ]
    if tunnel.ssh_port:
        args.extend(["-p", str(tunnel.ssh_port)])
    if tunnel.identity_file:
        args.extend(["-i", str(Path(tunnel.identity_file).expanduser())])
    args.extend(
        [
            "-R",
            f"{tunnel.remote_bind_host}:{tunnel.remote_port}:{tunnel.target_host}:{tunnel.target_port}",
            tunnel.ssh_target,
        ]
    )
    return args


def args_hash(args: list[str]) -> str:
    digest = hashlib.sha256()
    for arg in args:
        digest.update(arg.encode("utf-8", "replace"))
        digest.update(b"\0")
    return digest.hexdigest()


def _ps_output(args: list[str], timeout: float = 3) -> str:
    try:
        return subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def process_signature(pid: int) -> str | None:
    if pid <= 1:
        return None
    output = _ps_output(["/bin/ps", "-p", str(pid), "-o", "lstart="]).strip()
    return output or None


def process_command(pid: int) -> str | None:
    if pid <= 1:
        return None
    output = _ps_output(["/bin/ps", "-p", str(pid), "-o", "command="]).strip()
    return output or None


def process_group(pid: int) -> int | None:
    output = _ps_output(["/bin/ps", "-p", str(pid), "-o", "pgid="]).strip()
    try:
        return int(output)
    except ValueError:
        return None


def process_table() -> list[dict[str, Any]]:
    output = _ps_output(["/bin/ps", "-axo", "pid=,ppid=,pgid=,command="])
    rows: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) != 4 or not parts[0].isdigit():
            continue
        rows.append(
            {
                "pid": int(parts[0]),
                "ppid": int(parts[1]) if parts[1].isdigit() else None,
                "pgid": int(parts[2]) if parts[2].isdigit() else None,
                "command": parts[3],
            }
        )
    return rows


def _is_zombie(row: dict[str, Any]) -> bool:
    command = str(row.get("command", "")).strip()
    return command.startswith("<defunct>") or command.startswith("[defunct]")


def verify_record(record: ProcessRecord) -> bool:
    signature = process_signature(record.autossh_pid)
    command = process_command(record.autossh_pid)
    if signature is None or command is None or signature != record.process_signature:
        return False
    if record.marker not in command:
        return False
    return "autossh" in Path(command.split(None, 1)[0]).name


def find_ssh_pid(record: ProcessRecord) -> int | None:
    rows = process_table()
    children: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        if row["ppid"] is not None:
            children.setdefault(row["ppid"], []).append(row)
    queue = list(children.get(record.autossh_pid, []))
    while queue:
        row = queue.pop(0)
        if record.marker in row["command"] and Path(
            row["command"].split(None, 1)[0]
        ).name.startswith("ssh"):
            return row["pid"]
        queue.extend(children.get(row["pid"], []))
    for row in rows:
        if row["pgid"] != record.pgid:
            continue
        binary = Path(row["command"].split(None, 1)[0]).name
        if record.marker in row["command"] and binary.startswith("ssh"):
            return row["pid"]
    return None


def find_profile_processes(tunnel_id: str) -> list[dict[str, Any]]:
    markers = (marker_for(tunnel_id), f"WIKITUNNEL_PROFILE={tunnel_id}")
    return [
        row
        for row in process_table()
        if any(marker in row["command"] for marker in markers) and "autossh" in row["command"]
    ]


def process_group_members(record: ProcessRecord) -> list[dict[str, Any]]:
    return [
        row
        for row in process_table()
        if row["pgid"] == record.pgid and not _is_zombie(row)
    ]


def group_is_fully_managed(record: ProcessRecord) -> bool:
    members = process_group_members(record)
    if not members:
        return False
    for member in members:
        command = member["command"]
        binary = Path(command.split(None, 1)[0]).name
        if record.marker not in command or not (
            binary == "autossh" or binary.startswith("ssh")
        ):
            return False
    return True


async def stop_record(record: ProcessRecord, timeout: float = 5.0) -> tuple[bool, str]:
    if not verify_record(record):
        return False, "PID 已失效或命令行 marker 不匹配，未发送信号"
    pid = record.autossh_pid
    group_managed = group_is_fully_managed(record)
    try:
        if group_managed:
            os.killpg(record.pgid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True, "进程已退出"
    except PermissionError as exc:
        return False, redact_text(str(exc))

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if group_managed:
            if not process_group_members(record):
                return True, "已发送 SIGTERM，受管进程组已退出"
        elif not verify_record(record):
            return True, "已发送 SIGTERM 并退出"
        await asyncio.sleep(0.2)

    if group_managed:
        if not process_group_members(record):
            return True, "受管进程组已退出"
        if not group_is_fully_managed(record):
            return False, "进程组成员发生变化，未发送 SIGKILL"
    elif not verify_record(record):
        return False, "进程身份已变化，未发送 SIGKILL"
    try:
        if group_managed:
            os.killpg(record.pgid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True, "进程已退出"
    except PermissionError as exc:
        return False, redact_text(str(exc))
    return True, "SIGTERM 超时，已发送 SIGKILL"


def record_path(records_dir: Path, tunnel_id: str) -> Path:
    return records_dir / f"{tunnel_id}.json"


def save_record(records_dir: Path, record: ProcessRecord) -> None:
    records_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(records_dir, 0o700)
    path = record_path(records_dir, record.tunnel_id)
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(record.to_dict(), handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def load_record(records_dir: Path, tunnel_id: str) -> ProcessRecord | None:
    path = record_path(records_dir, tunnel_id)
    try:
        with path.open(encoding="utf-8") as handle:
            return ProcessRecord.from_dict(json.load(handle))
    except (OSError, ValueError, KeyError):
        return None


def delete_record(records_dir: Path, tunnel_id: str) -> None:
    try:
        record_path(records_dir, tunnel_id).unlink()
    except FileNotFoundError:
        pass


def open_log(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.chmod(path, 0o600)
    return descriptor


def read_log(path: Path, tail_lines: int = 1000) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = deque(handle, maxlen=tail_lines)
        return redact_text("\n".join(line.rstrip("\n") for line in lines), 200_000)
    except FileNotFoundError:
        return ""


def clear_log(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(descriptor)
    os.chmod(path, 0o600)


def rotate_log(path: Path, max_bytes: int = 4 * 1024 * 1024) -> None:
    try:
        if path.stat().st_size <= max_bytes:
            return
        rotated = path.with_suffix(path.suffix + ".1")
        try:
            rotated.unlink()
        except FileNotFoundError:
            pass
        path.rename(rotated)
        os.chmod(rotated, 0o600)
    except FileNotFoundError:
        pass


def read_log_from_offset(path: Path, offset: int, limit: int = 256 * 1024) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(0 if size < offset else offset)
            data = handle.read(limit)
        return redact_text(data.decode("utf-8", "replace"), 200_000)
    except FileNotFoundError:
        return ""


def conflict_in_log(path: Path, offset: int = 0) -> bool:
    text = read_log_from_offset(path, offset).lower()
    return "remote port forwarding failed" in text or "cannot listen to port" in text


def auth_error_in_log(path: Path, offset: int = 0) -> bool:
    text = read_log_from_offset(path, offset).lower()
    return "permission denied" in text or "no such identity" in text


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

