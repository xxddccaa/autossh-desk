from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from .diagnostics import Diagnostics
from .models import Tunnel
from .schemas import ConflictPreview, RemoteListener


class ConflictError(RuntimeError):
    pass


@dataclass(slots=True)
class StoredPreview:
    tunnel_id: str
    listener_fingerprint: str
    listener: RemoteListener
    created_at: float
    expires_at: datetime
    can_clean: bool
    tcp_probe_ok: bool


def listener_fingerprint(tunnel: Tunnel, listener: RemoteListener) -> str:
    payload = {
        "tunnel_id": tunnel.id,
        "bind_host": tunnel.remote_bind_host,
        "port": tunnel.remote_port,
        "pids": sorted(listener.pids),
        "raw": listener.raw.strip(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _manual_command(tunnel: Tunnel, listener: RemoteListener) -> str:
    port = tunnel.remote_port
    pid_text = ",".join(str(pid) for pid in listener.pids) or "<PID>"
    ssh_port = f" -p {tunnel.ssh_port}" if tunnel.ssh_port else ""
    identity = f' -i "{tunnel.identity_file}"' if tunnel.identity_file else ""
    return (
        f'ssh{ssh_port}{identity} {tunnel.ssh_target} '
        f'"ss -ltnp \'sport = :{port}\'; '
        f'ps -o pid,ppid,user,comm,args -p {pid_text}"\n'
        f"# 在远端主机上核对 PID 后：kill -TERM {pid_text}"
    )


class ConflictService:
    def __init__(self, diagnostics: Diagnostics):
        self.diagnostics = diagnostics
        self._previews: dict[str, StoredPreview] = {}

    def _prune(self) -> None:
        now = time.monotonic()
        for token, preview in list(self._previews.items()):
            if now - preview.created_at > 180:
                self._previews.pop(token, None)

    async def preview(self, tunnel: Tunnel) -> ConflictPreview:
        self._prune()
        listener, listener_error = await self.diagnostics.remote_listener(tunnel)
        if listener_error:
            raise ConflictError(f"无法读取远端监听：{listener_error}")
        if not listener.found:
            raise ConflictError("远端端口当前未监听，无需清理")

        end_to_end_ok, end_to_end_detail = await self.diagnostics.end_to_end(tunnel)
        if end_to_end_ok is None:
            raise ConflictError(
                f"无法确认 TCP 监听状态：{end_to_end_detail}。不会提供自动清理。"
            )

        candidates = [
            process for process in listener.processes if process.get("is_sshd") is True
        ]
        can_clean = (
            len(listener.pids) == 1
            and len(candidates) == 1
            and candidates[0].get("pid") == listener.pids[0]
            and f"{tunnel.remote_bind_host}:{tunnel.remote_port}" in listener.raw.split()
        )
        if can_clean:
            message = (
                "已确认唯一的 sshd 会话占用该地址和端口。TCP 可达不代表转发目标正确。"
                "清理将中断该会话的所有转发，发起端自动重连仍可能重新占用；请核对用途后再次确认。"
            )
        else:
            message = (
                "无法安全确认唯一的 sshd 监听进程。AutoSSH Desk 不会自动终止，请使用手工命令核对。"
            )
        token = secrets.token_urlsafe(24)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=120)
        self._previews[token] = StoredPreview(
            tunnel_id=tunnel.id,
            listener_fingerprint=listener_fingerprint(tunnel, listener),
            listener=listener,
            created_at=time.monotonic(),
            expires_at=expires_at,
            can_clean=can_clean,
            tcp_probe_ok=end_to_end_ok,
        )
        return ConflictPreview(
            tunnel_id=tunnel.id,
            fingerprint=token,
            can_clean=can_clean,
            remote_listener=listener,
            end_to_end_ok=end_to_end_ok,
            message=message,
            manual_command=_manual_command(tunnel, listener),
            expires_at=expires_at,
        )

    async def clean(
        self, tunnel: Tunnel, fingerprint: str, confirm: bool
    ) -> tuple[RemoteListener, str]:
        self._prune()
        if not confirm:
            raise ConflictError("必须完成二次确认后才能清理远端端口")
        preview = self._previews.pop(fingerprint, None)
        if preview is None or preview.tunnel_id != tunnel.id:
            raise ConflictError("清理确认已失效，请重新诊断")
        if preview.expires_at <= datetime.now(timezone.utc):
            raise ConflictError("清理确认已过期，请重新诊断")
        if not preview.can_clean:
            raise ConflictError("没有可安全确认的单一 sshd 监听进程")

        listener, listener_error = await self.diagnostics.remote_listener(tunnel)
        if listener_error:
            raise ConflictError(f"重新检查远端端口失败：{listener_error}")
        if listener_fingerprint(tunnel, listener) != preview.listener_fingerprint:
            raise ConflictError("远端监听状态已经变化，已取消清理，请重新诊断")
        end_to_end_ok, end_to_end_detail = await self.diagnostics.end_to_end(tunnel)
        if end_to_end_ok is None:
            raise ConflictError(f"无法确认 TCP 监听状态：{end_to_end_detail}。已取消清理。")
        if end_to_end_ok != preview.tcp_probe_ok:
            raise ConflictError("远端 TCP 监听状态已经变化，已取消清理，请重新核对用途并确认")
        candidates = [
            process for process in listener.processes if process.get("is_sshd") is True
        ]
        if len(listener.pids) != 1 or len(candidates) != 1:
            raise ConflictError("远端进程信息无法安全确认，已取消清理")
        pid = listener.pids[0]
        port = tunnel.remote_port
        command = (
            "set -eu; "
            f'pid={pid}; port={port}; bind_host={tunnel.remote_bind_host}; '
            '[ "$pid" -gt 1 ] || exit 41; '
            'ancestor=$$; while [ "$ancestor" -gt 1 ]; do '
            '[ "$pid" -ne "$ancestor" ] || exit 42; '
            'ancestor=$(ps -o ppid= -p "$ancestor" | tr -d " "); '
            '[ -n "$ancestor" ] || exit 42; done; '
            'comm=$(ps -o comm= -p "$pid" 2>/dev/null | sed -n "1p"); '
            '[ "${comm##*/}" = sshd ] || exit 43; '
            'listeners=$(ss -H -ltnp "sport = :$port" 2>/dev/null '
            '| awk -v endpoint="$bind_host:$port" \'$4 == endpoint {print}\'); '
            'case "$listeners" in *"pid=$pid,"*|*"pid=$pid)"*) ;; *) exit 44;; esac; '
            'kill -TERM "$pid"'
        )
        result = await self.diagnostics.run_remote(tunnel, command, timeout=12)
        if not result.ok:
            raise ConflictError(
                f"安全校验或终止失败，未确认清理：{result.message}。请使用手工命令。"
            )
        return listener, f"已向远端 PID {pid} 发送 SIGTERM"

