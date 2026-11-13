from __future__ import annotations

import asyncio
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from .config import Settings
from .conflict import ConflictError, ConflictService
from .database import Database
from .diagnostics import DiagnosticSnapshot, Diagnostics
from .events import EventBroker
from .models import Tunnel
from .processes import (
    ProcessRecord,
    args_hash,
    auth_error_in_log,
    build_autossh_args,
    clear_log,
    conflict_in_log,
    delete_record,
    find_ssh_pid,
    load_record,
    marker_for,
    now_iso,
    open_log,
    process_signature,
    read_log,
    rotate_log,
    save_record,
    stop_record,
    verify_record,
)
from .redaction import redact_text
from .schemas import (
    ConflictCleanRequest,
    ConflictPreview,
    RemoteListener,
    RuntimeView,
    StartResponse,
    TunnelRead,
    TunnelStatus,
)


BACKOFF_SECONDS = (1, 2, 5, 10, 20, 30, 60)
NETWORK_WAIT_STATES = {"target_unreachable", "ssh_unreachable"}
TRANSIENT_STATES = NETWORK_WAIT_STATES | {"reconnecting", "port_conflict"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SupervisorError(RuntimeError):
    pass


class TunnelBusyError(SupervisorError):
    pass


class TunnelSupervisor:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        diagnostics: Diagnostics,
        broker: EventBroker,
    ) -> None:
        self.database = database
        self.settings = settings
        self.diagnostics = diagnostics
        self.broker = broker
        self.conflicts = ConflictService(diagnostics)
        self._runtimes: dict[str, RuntimeView] = {}
        self._records: dict[str, ProcessRecord] = {}
        self._handles: dict[str, asyncio.subprocess.Process] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._active: set[str] = set()
        self._stopping: set[str] = set()
        self._retry_attempts: dict[str, int] = {}
        self._tail_tasks: dict[str, asyncio.Task[None]] = {}
        self._watch_tasks: dict[str, asyncio.Task[None]] = {}
        self._post_start_tasks: set[asyncio.Task[None]] = set()
        self._monitor_task: asyncio.Task[None] | None = None
        self._shutdown = False
        self._last_wall_time = time.time()
        self._network_fingerprint = ""
        self._started = False

    def _lock(self, tunnel_id: str) -> asyncio.Lock:
        return self._locks.setdefault(tunnel_id, asyncio.Lock())

    def _runtime(self, tunnel_id: str) -> RuntimeView:
        runtime = self._runtimes.get(tunnel_id)
        if runtime is None:
            runtime = RuntimeView(tunnel_id=tunnel_id, updated_at=utc_now())
            self._runtimes[tunnel_id] = runtime
        return runtime

    def _get_tunnel(self, tunnel_id: str) -> Tunnel:
        with self.database.session() as session:
            tunnel = session.get(Tunnel, tunnel_id)
            if tunnel is None:
                raise SupervisorError("隧道不存在")
            session.expunge(tunnel)
            return tunnel

    def list_tunnels(self) -> list[Tunnel]:
        with self.database.session() as session:
            tunnels = list(session.scalars(select(Tunnel).order_by(Tunnel.created_at)).all())
            for tunnel in tunnels:
                session.expunge(tunnel)
            return tunnels

    def _set_manually_stopped(self, tunnel_id: str, stopped: bool) -> None:
        with self.database.session() as session:
            tunnel = session.get(Tunnel, tunnel_id)
            if tunnel is None:
                raise SupervisorError("隧道不存在")
            if tunnel.manually_stopped != stopped:
                tunnel.manually_stopped = stopped
                session.commit()

    def _status(self, tunnel: Tunnel) -> TunnelStatus:
        return TunnelStatus(
            tunnel=TunnelRead.model_validate(tunnel),
            runtime=self._runtime(tunnel.id).model_copy(deep=True),
        )

    def statuses(self) -> list[TunnelStatus]:
        return [self._status(tunnel) for tunnel in self.list_tunnels()]

    def status(self, tunnel_id: str) -> TunnelStatus:
        return self._status(self._get_tunnel(tunnel_id))

    def _publish_runtime(self, tunnel_id: str) -> None:
        runtime = self._runtime(tunnel_id)
        runtime.updated_at = utc_now()
        self.broker.publish(
            {
                "type": "runtime",
                "tunnel_id": tunnel_id,
                "runtime": runtime.model_dump(mode="json"),
            }
        )

    def _transition(
        self,
        runtime: RuntimeView,
        state: str,
        *,
        error: str | None = None,
        activity: str | None = None,
    ) -> None:
        previous = runtime.state
        runtime.state = state  # type: ignore[assignment]
        if state == "connected":
            if previous in TRANSIENT_STATES:
                runtime.reconnect_count += 1
            if previous != "connected" or runtime.stable_since is None:
                runtime.stable_since = utc_now()
            if runtime.managed and runtime.ssh_pid is not None and runtime.remote_listener.found:
                listener_pids = ", ".join(map(str, runtime.remote_listener.pids)) or "未知"
                runtime.checks = [
                    check.model_copy(update={
                        "state": "ok",
                        "detail": f"受管 SSH 运行中，远端监听已建立；PID {listener_pids}",
                    })
                    if check.key == "remote_listener" and check.state == "warn"
                    else check
                    for check in runtime.checks
                ]
        else:
            runtime.stable_since = None
        if error is not None:
            runtime.last_error = redact_text(error, 1000)
        if activity is not None:
            runtime.activity = activity
        self._publish_runtime(runtime.tunnel_id)

    def _apply_snapshot(self, runtime: RuntimeView, snapshot: DiagnosticSnapshot) -> None:
        runtime.checks = snapshot.checks
        runtime.remote_listener = snapshot.remote_listener
        if snapshot.error:
            runtime.last_error = redact_text(snapshot.error, 1000)

    @staticmethod
    def _snapshot_healthy(snapshot: DiagnosticSnapshot) -> bool:
        target = snapshot.check("target_tcp")
        ssh = snapshot.check("ssh_tcp")
        return bool(
            target is not None
            and target.state == "ok"
            and ssh is not None
            and ssh.state == "ok"
            and snapshot.ssh_auth_ok
            and snapshot.remote_listener.found
            and snapshot.end_to_end_known
            and snapshot.end_to_end_ok
        )

    @staticmethod
    def _target_unreachable(snapshot: DiagnosticSnapshot) -> bool:
        dns = snapshot.check("dns")
        target = snapshot.check("target_tcp")
        return bool(
            (dns is not None and dns.state == "fail")
            or (target is not None and target.state == "fail")
        )

    def _process_fields(self, runtime: RuntimeView, record: ProcessRecord | None) -> None:
        if record is None:
            runtime.managed = False
            runtime.autossh_pid = None
            runtime.ssh_pid = None
            return
        runtime.managed = True
        runtime.autossh_pid = record.autossh_pid
        runtime.ssh_pid = find_ssh_pid(record)

    async def start_background(self) -> None:
        if self._started:
            return
        self._started = True
        for tunnel in self.list_tunnels():
            runtime = self._runtime(tunnel.id)
            runtime.updated_at = utc_now()
            record = load_record(self.settings.records_dir, tunnel.id)
            if record is not None and verify_record(record):
                self._records[tunnel.id] = record
                self._active.add(tunnel.id)
                self._process_fields(runtime, record)
                try:
                    started_at = datetime.fromisoformat(record.started_at)
                    if started_at.tzinfo is None:
                        started_at = started_at.replace(tzinfo=timezone.utc)
                    runtime.started_at = started_at
                except ValueError:
                    runtime.started_at = utc_now()
                runtime.state = "reconnecting"
                runtime.activity = "已接管上次运行的 autossh，正在重新诊断"
                self._start_tail(tunnel.id)
            elif record is not None:
                delete_record(self.settings.records_dir, tunnel.id)
                runtime.activity = "发现失效的 PID 记录，已清除"
            if tunnel.manually_stopped and not tunnel.auto_start:
                runtime.activity = "自动连接已暂停，点击启动恢复"
            self._publish_runtime(tunnel.id)
        self._network_fingerprint = await asyncio.to_thread(self._read_network_path)
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        for tunnel in self.list_tunnels():
            if tunnel.auto_start:
                self._set_manually_stopped(tunnel.id, False)
                if tunnel.id not in self._records:
                    asyncio.create_task(self._auto_start(tunnel.id))

    async def _auto_start(self, tunnel_id: str) -> None:
        try:
            await self.start_tunnel(tunnel_id, manual=False)
        except (SupervisorError, OSError):
            pass

    async def shutdown(self) -> None:
        self._shutdown = True
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        tasks = list(self._tail_tasks.values()) + list(self._post_start_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def start_tunnel(self, tunnel_id: str, manual: bool = True) -> StartResponse:
        async with self._lock(tunnel_id):
            tunnel = self._get_tunnel(tunnel_id)
            runtime = self._runtime(tunnel_id)
            if not manual and tunnel.manually_stopped:
                return StartResponse(
                    started=False,
                    message="隧道已手动停止，请点击启动恢复自动连接",
                    runtime=runtime.model_copy(deep=True),
                )
            if manual:
                self._set_manually_stopped(tunnel_id, False)
                tunnel.manually_stopped = False
            record = self._records.get(tunnel_id)
            if record is not None and verify_record(record):
                self._process_fields(runtime, record)
                return StartResponse(
                    started=False,
                    message="该隧道已由 AutoSSH Desk 管理并正在运行",
                    runtime=runtime.model_copy(deep=True),
                )
            if record is not None:
                self._clear_record(tunnel_id)
            if manual:
                self._retry_attempts[tunnel_id] = 0
            return await self._attempt_start(tunnel)

    async def _attempt_start(self, tunnel: Tunnel) -> StartResponse:
        tunnel_id = tunnel.id
        runtime = self._runtime(tunnel_id)
        self._active.add(tunnel_id)
        runtime.external_tunnel = False
        runtime.next_retry_at = None
        self._transition(runtime, "starting", activity="正在执行启动前检查")
        try:
            snapshot = await self.diagnostics.run(tunnel)
        except OSError as exc:
            self._transition(runtime, "error", error=str(exc), activity="启动检查失败，等待重试")
            self._schedule_retry(tunnel, str(exc))
            return StartResponse(
                started=False,
                message=f"启动检查失败：{redact_text(str(exc))}",
                runtime=runtime.model_copy(deep=True),
            )
        self._apply_snapshot(runtime, snapshot)

        autossh_check = snapshot.check("autossh")
        if autossh_check is not None and autossh_check.state == "fail":
            self._active.discard(tunnel_id)
            runtime.next_retry_at = None
            self._transition(runtime, "error", error=autossh_check.detail, activity="缺少 autossh")
            return StartResponse(started=False, message=autossh_check.detail, runtime=runtime.model_copy(deep=True))

        forwarding_check = snapshot.check("ssh_forwarding")
        if forwarding_check is not None and forwarding_check.state == "fail":
            self._active.discard(tunnel_id)
            self._transition(
                runtime, "error", error=forwarding_check.detail, activity="请修正 SSH 别名配置后重新启动"
            )
            return StartResponse(
                started=False, message=forwarding_check.detail, runtime=runtime.model_copy(deep=True)
            )

        if self._target_unreachable(snapshot):
            self._transition(
                runtime,
                "target_unreachable",
                error="VPN 或目标服务网络不可达",
                activity="正在等待目标网络恢复",
            )
            self._schedule_retry(tunnel, "目标网络不可达")
            return StartResponse(
                started=False,
                message="目标服务不可达，隧道未启动",
                runtime=runtime.model_copy(deep=True),
            )

        ssh_check = snapshot.check("ssh_tcp")
        if ssh_check is not None and ssh_check.state == "fail":
            self._transition(
                runtime,
                "ssh_unreachable",
                error="远端 SSH 端口不可达",
                activity="正在等待 SSH 网络恢复",
            )
            self._schedule_retry(tunnel, "远端 SSH 不可达")
            return StartResponse(
                started=False,
                message="远端 SSH 不可达，隧道未启动",
                runtime=runtime.model_copy(deep=True),
            )

        auth_check = snapshot.check("ssh_auth")
        if auth_check is not None and auth_check.state == "fail":
            if snapshot.ssh_failure_retryable:
                self._retry_ssh_failure(tunnel, auth_check.detail)
                return StartResponse(
                    started=False,
                    message=runtime.last_error or "SSH 连接暂时失败，等待重试",
                    runtime=runtime.model_copy(deep=True),
                )
            self._active.discard(tunnel_id)
            runtime.next_retry_at = None
            self._transition(runtime, "error", error=auth_check.detail, activity="SSH 认证失败")
            return StartResponse(
                started=False,
                message=f"SSH 认证失败：{auth_check.detail}",
                runtime=runtime.model_copy(deep=True),
            )

        listener_check = snapshot.check("remote_listener")
        if listener_check is not None and listener_check.state == "fail":
            self._transition(runtime, "error", error=listener_check.detail, activity="无法检查远端端口")
            self._schedule_retry(tunnel, listener_check.detail)
            return StartResponse(
                started=False,
                message=listener_check.detail,
                runtime=runtime.model_copy(deep=True),
            )

        if snapshot.remote_listener.found:
            self._unmanaged_listener(tunnel, snapshot)
            return StartResponse(
                started=False,
                message=runtime.last_error or "远端端口已被非受管会话占用",
                runtime=runtime.model_copy(deep=True),
            )

        try:
            record = await self._spawn_autossh(tunnel, snapshot.autossh_path or "")
        except (OSError, SupervisorError) as exc:
            self._transition(runtime, "error", error=str(exc), activity="autossh 启动失败")
            self._schedule_retry(tunnel, str(exc))
            return StartResponse(
                started=False,
                message=f"autossh 启动失败：{exc}",
                runtime=runtime.model_copy(deep=True),
            )

        runtime.external_tunnel = False
        runtime.conflict = None
        runtime.next_retry_at = None
        runtime.started_at = utc_now()
        runtime.stable_since = None
        self._process_fields(runtime, record)
        self._transition(runtime, "starting", activity="autossh 已启动，正在等待 SSH 转发")
        task = asyncio.create_task(self._post_start_check(tunnel_id, 1.2))
        self._post_start_tasks.add(task)
        task.add_done_callback(self._post_start_tasks.discard)
        return StartResponse(
            started=True,
            message="autossh 已启动，正在建立隧道",
            runtime=runtime.model_copy(deep=True),
        )

    def _unmanaged_listener(self, tunnel: Tunnel, snapshot: DiagnosticSnapshot) -> None:
        runtime = self._runtime(tunnel.id)
        runtime.external_tunnel = False
        self._process_fields(runtime, None)
        runtime.conflict = self._conflict_payload(snapshot, "远端端口已被非受管会话占用")
        if not snapshot.end_to_end_known:
            error = "远端端口已有非受管监听，但无法执行 TCP 监听探测"
            self._transition(runtime, "error", error=error, activity="未复用未知监听，等待重新检查")
            self._schedule_retry(tunnel, error)
            return
        error = (
            "远端端口已被非受管会话占用；TCP 可达不能确认转发目标，未复用该监听"
            if snapshot.end_to_end_ok
            else "远端端口已被非受管会话占用，TCP 监听探测失败"
        )
        self._enter_port_conflict(tunnel, error)

    def _enter_port_conflict(self, tunnel: Tunnel, error: str) -> None:
        runtime = self._runtime(tunnel.id)
        runtime.external_tunnel = False
        runtime.conflict = runtime.conflict or {"message": error}
        if tunnel.manually_stopped:
            activity = "已手动停止，自动连接已暂停"
        elif not tunnel.auto_reconnect:
            activity = "自动重连已关闭，请检查远端端口"
        else:
            activity = "等待远端端口释放，将自动重试"
        self._transition(runtime, "port_conflict", error=error, activity=activity)
        self._schedule_retry(tunnel, error)

    def _retry_ssh_failure(self, tunnel: Tunnel, detail: str) -> None:
        runtime = self._runtime(tunnel.id)
        managed = tunnel.id in self._records
        retry_enabled = tunnel.auto_reconnect and not tunnel.manually_stopped
        self._transition(
            runtime,
            "reconnecting",
            error=f"SSH 连接暂时失败：{detail}",
            activity=(
                "SSH 诊断暂时失败，保留受管 autossh 并重新检查"
                if managed
                else "等待退避后自动重试 SSH 连接" if retry_enabled else "自动连接已暂停"
            ),
        )
        if managed and not tunnel.auto_reconnect and not tunnel.manually_stopped:
            runtime.next_retry_at = utc_now() + timedelta(seconds=self.settings.monitor_interval)
            self._publish_runtime(tunnel.id)
        else:
            self._schedule_retry(tunnel, runtime.last_error or detail)

    def _schedule_retry(self, tunnel: Tunnel, reason: str) -> None:
        runtime = self._runtime(tunnel.id)
        if not tunnel.auto_reconnect or tunnel.manually_stopped:
            self._active.discard(tunnel.id)
            runtime.next_retry_at = None
            self._publish_runtime(tunnel.id)
            return
        attempt = self._retry_attempts.get(tunnel.id, 0)
        delay = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
        self._retry_attempts[tunnel.id] = attempt + 1
        runtime.next_retry_at = utc_now() + timedelta(seconds=delay)
        runtime.last_error = redact_text(reason, 1000)
        self._active.add(tunnel.id)
        self._publish_runtime(tunnel.id)

    async def _spawn_autossh(self, tunnel: Tunnel, autossh_path: str) -> ProcessRecord:
        if not autossh_path:
            raise SupervisorError("autossh 路径不存在")
        self.settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.settings.data_dir, 0o700)
        log_path = self.settings.logs_dir / f"{tunnel.id}.log"
        rotate_log(log_path)
        descriptor = open_log(log_path)
        os.write(
            descriptor,
            f"\n--- AutoSSH Desk autossh session {now_iso()} ---\n".encode("utf-8"),
        )
        os.fsync(descriptor)
        log_offset = log_path.stat().st_size
        args = build_autossh_args(tunnel, autossh_path)
        environment = os.environ.copy()
        environment.update(
            {
                "AUTOSSH_GATETIME": "0",
                "AUTOSSH_POLL": "30",
                "AUTOSSH_PATH": self.settings.ssh_executable,
                "SSH_ASKPASS_REQUIRE": "never",
                "LANG": "C",
                "LC_ALL": "C",
            }
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=descriptor,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self.settings.data_dir),
                env=environment,
                start_new_session=True,
            )
        except OSError as exc:
            os.close(descriptor)
            raise SupervisorError(redact_text(str(exc))) from exc
        os.close(descriptor)
        signature = process_signature(process.pid)
        if signature is None:
            process.terminate()
            raise SupervisorError("无法读取新进程启动签名")
        record = ProcessRecord(
            tunnel_id=tunnel.id,
            autossh_pid=process.pid,
            pgid=process.pid,
            started_at=now_iso(),
            process_signature=signature,
            args_hash=args_hash(args),
            autossh_path=autossh_path,
            marker=marker_for(tunnel.id),
            log_offset=log_offset,
        )
        save_record(self.settings.records_dir, record)
        self._records[tunnel.id] = record
        self._handles[tunnel.id] = process
        self._start_tail(tunnel.id)
        watch = asyncio.create_task(self._watch_process(tunnel.id, process, record))
        self._watch_tasks[tunnel.id] = watch
        watch.add_done_callback(lambda _: self._watch_tasks.pop(tunnel.id, None))
        return record

    async def _watch_process(
        self,
        tunnel_id: str,
        process: asyncio.subprocess.Process,
        record: ProcessRecord,
    ) -> None:
        return_code = await process.wait()
        async with self._lock(tunnel_id):
            if self._records.get(tunnel_id) != record:
                return
            if tunnel_id in self._stopping:
                return
            self._handle_process_exit(tunnel_id, record, return_code)

    def _handle_process_exit(self, tunnel_id: str, record: ProcessRecord, code: int) -> None:
        runtime = self._runtime(tunnel_id)
        log_path = self.settings.logs_dir / f"{tunnel_id}.log"
        had_conflict = conflict_in_log(log_path, record.log_offset)
        had_auth_error = auth_error_in_log(log_path, record.log_offset)
        self._clear_record(tunnel_id)
        self._process_fields(runtime, None)
        if had_conflict:
            self._enter_port_conflict(
                self._get_tunnel(tunnel_id),
                "remote port forwarding failed：远端端口已被旧会话占用",
            )
            return
        if had_auth_error:
            self._active.discard(tunnel_id)
            runtime.next_retry_at = None
            self._transition(
                runtime,
                "error",
                error="SSH 认证失败，请检查 Agent、Keychain 或 SSH 配置",
                activity="autossh 已退出",
            )
            return
        if tunnel_id not in self._active:
            self._transition(runtime, "stopped", activity="隧道已停止")
            return
        tunnel = self._get_tunnel(tunnel_id)
        self._transition(
            runtime,
            "reconnecting",
            error=f"autossh 已退出（退出码 {code}）",
            activity="等待指数退避后重试",
        )
        self._schedule_retry(tunnel, runtime.last_error or "autossh 已退出")

    async def _post_start_check(self, tunnel_id: str, delay: float) -> None:
        await asyncio.sleep(delay)
        async with self._lock(tunnel_id):
            if tunnel_id not in self._active or tunnel_id in self._stopping:
                return
            record = self._records.get(tunnel_id)
            if record is None or not verify_record(record):
                return
            await self._check_managed(tunnel_id, record, early=True)

    async def _check_managed(
        self, tunnel_id: str, record: ProcessRecord, *, early: bool = False
    ) -> None:
        tunnel = self._get_tunnel(tunnel_id)
        runtime = self._runtime(tunnel_id)
        log_path = self.settings.logs_dir / f"{tunnel_id}.log"
        if conflict_in_log(log_path, record.log_offset):
            await self._terminate_for_conflict(tunnel, record)
            return
        self._process_fields(runtime, record)
        snapshot = await self.diagnostics.run(tunnel)
        self._apply_snapshot(runtime, snapshot)
        if conflict_in_log(log_path, record.log_offset):
            await self._terminate_for_conflict(tunnel, record)
            return
        self._process_fields(runtime, record)
        forwarding_check = snapshot.check("ssh_forwarding")
        if forwarding_check is not None and forwarding_check.state == "fail":
            await self._terminate_record(tunnel_id, record)
            self._active.discard(tunnel_id)
            runtime.next_retry_at = None
            self._transition(
                runtime, "error", error=forwarding_check.detail, activity="请修正 SSH 别名配置后重新启动"
            )
            return
        ssh_check = snapshot.check("ssh_tcp")
        auth_check = snapshot.check("ssh_auth")
        if self._target_unreachable(snapshot):
            ok, stop_message = await self._terminate_record(tunnel_id, record)
            self._transition(
                runtime,
                "target_unreachable",
                error="VPN 或目标服务网络不可达",
                activity=(
                    "已停止 autossh，等待目标网络恢复"
                    if ok
                    else f"停止 autossh 失败：{stop_message}"
                ),
            )
            if ok:
                self._schedule_retry(tunnel, "目标网络不可达")
            return
        if ssh_check is not None and ssh_check.state == "fail":
            ok, stop_message = await self._terminate_record(tunnel_id, record)
            self._transition(
                runtime,
                "ssh_unreachable",
                error="远端 SSH 端口不可达",
                activity=(
                    "已停止 autossh，等待 SSH 网络恢复"
                    if ok
                    else f"停止 autossh 失败：{stop_message}"
                ),
            )
            if ok:
                self._schedule_retry(tunnel, "远端 SSH 不可达")
            return
        if auth_check is not None and auth_check.state == "fail":
            if snapshot.ssh_failure_retryable:
                self._retry_ssh_failure(tunnel, auth_check.detail)
                return
            await self._terminate_record(tunnel_id, record)
            runtime.next_retry_at = None
            self._active.discard(tunnel_id)
            self._transition(
                runtime,
                "error",
                error=f"SSH 认证失败：{auth_check.detail}",
                activity="已停止自动重试，请检查 SSH 配置",
            )
            return
        if snapshot.remote_listener.found:
            if not snapshot.end_to_end_known:
                runtime.next_retry_at = utc_now() + timedelta(
                    seconds=self.settings.monitor_interval
                )
                self._transition(
                    runtime,
                    "error",
                    error="远端端口已被监听，但无法完成端到端探测",
                    activity="保持当前 autossh，不使用不可靠的清理判断",
                )
            elif snapshot.end_to_end_ok:
                if runtime.ssh_pid is None:
                    runtime.next_retry_at = utc_now() + timedelta(seconds=self.settings.monitor_interval)
                    self._transition(
                        runtime,
                        "reconnecting",
                        activity="autossh 尚无受管 SSH 子进程，不能确认监听属于当前隧道",
                    )
                    return
                runtime.next_retry_at = None
                runtime.external_tunnel = False
                runtime.conflict = None
                self._transition(runtime, "connected", activity="受管 SSH 转发已建立；服务协议需另行验证")
            else:
                await self._terminate_for_conflict(tunnel, record)
            return
        runtime.next_retry_at = utc_now() + timedelta(seconds=self.settings.monitor_interval)
        self._transition(
            runtime,
            "reconnecting",
            error=None if early else runtime.last_error,
            activity="autossh 正在建立远端监听",
        )

    async def _terminate_for_conflict(self, tunnel: Tunnel, record: ProcessRecord) -> None:
        runtime = self._runtime(tunnel.id)
        ok, message = await self._terminate_record(tunnel.id, record)
        runtime.conflict = runtime.conflict or {"message": message}
        error = "remote port forwarding failed：远端端口被旧会话占用"
        if not ok and tunnel.id in self._records:
            error += f"；停止 autossh 失败：{message}"
        self._enter_port_conflict(tunnel, error)

    async def _terminate_record(
        self, tunnel_id: str, record: ProcessRecord
    ) -> tuple[bool, str]:
        ok, message = await stop_record(record)
        if ok or not verify_record(record):
            self._clear_record(tunnel_id)
        runtime = self._runtime(tunnel_id)
        self._process_fields(runtime, self._records.get(tunnel_id))
        self._publish_runtime(tunnel_id)
        return ok, message

    async def stop_tunnel(self, tunnel_id: str, state_if_no_process: str = "stopped") -> RuntimeView:
        self._get_tunnel(tunnel_id)
        async with self._lock(tunnel_id):
            self._set_manually_stopped(tunnel_id, True)
            runtime = self._runtime(tunnel_id)
            record = self._records.get(tunnel_id)
            self._stopping.add(tunnel_id)
            if record is not None:
                self._transition(runtime, "stopping", activity="正在停止 AutoSSH Desk 管理的进程")
                ok, message = await self._terminate_record(tunnel_id, record)
                self._active.discard(tunnel_id)
                runtime.next_retry_at = None
                if ok:
                    runtime.last_error = None
                    self._transition(runtime, "stopped", activity="已手动停止，自动连接已暂停")
                else:
                    self._transition(runtime, "error", error=message, activity="停止失败")
            else:
                self._active.discard(tunnel_id)
                runtime.next_retry_at = None
                runtime.external_tunnel = False
                runtime.last_error = None
                self._transition(runtime, state_if_no_process, activity="已手动停止，自动连接已暂停")
            self._stopping.discard(tunnel_id)
            self._stop_tail(tunnel_id)
            return runtime.model_copy(deep=True)

    async def restart_tunnel(self, tunnel_id: str) -> StartResponse:
        await self.stop_tunnel(tunnel_id)
        return await self.start_tunnel(tunnel_id, manual=True)

    async def suspend_for_config_change(self, tunnel_id: str) -> None:
        self._get_tunnel(tunnel_id)
        async with self._lock(tunnel_id):
            record = self._records.get(tunnel_id)
            if record is not None and verify_record(record):
                raise TunnelBusyError("请先停止该隧道再修改或删除配置")
            self._active.discard(tunnel_id)
            runtime = self._runtime(tunnel_id)
            runtime.next_retry_at = None
            runtime.external_tunnel = False
            self._transition(runtime, "stopped", activity="配置即将修改，监控已暂停")
            self._stop_tail(tunnel_id)

    async def diagnose_tunnel(self, tunnel_id: str) -> RuntimeView:
        async with self._lock(tunnel_id):
            tunnel = self._get_tunnel(tunnel_id)
            runtime = self._runtime(tunnel_id)
            snapshot = await self.diagnostics.run(tunnel)
            self._apply_snapshot(runtime, snapshot)
            forwarding_check = snapshot.check("ssh_forwarding")
            if forwarding_check is not None and forwarding_check.state == "fail":
                self._active.discard(tunnel_id)
                runtime.next_retry_at = None
                self._transition(
                    runtime, "error", error=forwarding_check.detail, activity="请修正 SSH 别名配置后重新启动"
                )
                return runtime.model_copy(deep=True)
            ssh_check = snapshot.check("ssh_tcp")
            auth_check = snapshot.check("ssh_auth")
            if self._target_unreachable(snapshot):
                self._transition(
                    runtime,
                    "target_unreachable",
                    error="目标服务不可达",
                    activity="诊断完成，等待网络恢复",
                )
                return runtime.model_copy(deep=True)
            if ssh_check is not None and ssh_check.state == "fail":
                self._transition(
                    runtime,
                    "ssh_unreachable",
                    error="远端 SSH 不可达",
                    activity="诊断完成，等待网络恢复",
                )
                return runtime.model_copy(deep=True)
            if auth_check is not None and auth_check.state == "fail":
                if snapshot.ssh_failure_retryable:
                    self._retry_ssh_failure(tunnel, auth_check.detail)
                    return runtime.model_copy(deep=True)
                self._transition(
                    runtime,
                    "error",
                    error=f"SSH 认证失败：{auth_check.detail}",
                    activity="诊断完成",
                )
                return runtime.model_copy(deep=True)
            record = self._records.get(tunnel_id)
            if record is not None and verify_record(record):
                self._process_fields(runtime, record)
                if conflict_in_log(
                    self.settings.logs_dir / f"{tunnel_id}.log", record.log_offset
                ):
                    await self._terminate_for_conflict(tunnel, record)
                    return runtime.model_copy(deep=True)
                if not snapshot.remote_listener.found:
                    self._transition(
                        runtime, "reconnecting", activity="诊断完成，等待远端监听"
                    )
                elif not snapshot.end_to_end_known:
                    self._transition(
                        runtime,
                        "error",
                        error="远端端口已监听，但无法完成端到端探测",
                        activity="保持当前 autossh，未执行清理",
                    )
                elif runtime.ssh_pid is None:
                    self._transition(runtime, "reconnecting", activity="尚无受管 SSH 子进程，未认定监听健康")
                elif self._snapshot_healthy(snapshot):
                    runtime.next_retry_at = None
                    self._transition(runtime, "connected", activity="诊断通过")
                else:
                    await self._terminate_for_conflict(tunnel, record)
            elif snapshot.remote_listener.found:
                self._unmanaged_listener(tunnel, snapshot)
            else:
                runtime.external_tunnel = False
                runtime.conflict = None
                if (
                    tunnel_id in self._active
                    and tunnel.auto_reconnect
                    and not tunnel.manually_stopped
                ):
                    runtime.next_retry_at = utc_now()
                    self._transition(runtime, "reconnecting", activity="远端端口空闲，等待自动重连")
                elif runtime.state != "stopped":
                    self._transition(runtime, "stopped", activity="诊断完成")
                else:
                    self._transition(runtime, "stopped", activity="诊断完成，远端端口空闲")
            return runtime.model_copy(deep=True)

    def _conflict_payload(
        self, snapshot: DiagnosticSnapshot, message: str
    ) -> dict[str, Any]:
        return {
            "message": message,
            "end_to_end_ok": snapshot.end_to_end_ok,
            "remote_listener": snapshot.remote_listener.model_dump(mode="json"),
        }

    async def preview_conflict(self, tunnel_id: str) -> ConflictPreview:
        async with self._lock(tunnel_id):
            tunnel = self._get_tunnel(tunnel_id)
            record = self._records.get(tunnel_id)
            if record is not None and verify_record(record):
                raise ConflictError("AutoSSH Desk 管理的 autossh 仍在运行，请先停止后再检查远端残留")
            return await self.conflicts.preview(tunnel)

    async def clean_conflict(
        self, tunnel_id: str, request: ConflictCleanRequest
    ) -> tuple[RemoteListener, str]:
        async with self._lock(tunnel_id):
            tunnel = self._get_tunnel(tunnel_id)
            record = self._records.get(tunnel_id)
            if record is not None and verify_record(record):
                raise ConflictError("AutoSSH Desk 管理的 autossh 仍在运行，不能清理远端会话")
            listener, message = await self.conflicts.clean(
                tunnel, request.fingerprint, request.confirm
            )
            runtime = self._runtime(tunnel_id)
            runtime.external_tunnel = False
            runtime.conflict = None
            runtime.remote_listener = RemoteListener(
                found=False,
                address=tunnel.remote_bind_host,
                port=tunnel.remote_port,
            )
            if tunnel.auto_reconnect and not tunnel.manually_stopped:
                self._active.add(tunnel_id)
                self._retry_attempts[tunnel_id] = 0
                runtime.next_retry_at = utc_now()
                self._transition(runtime, "reconnecting", activity="等待远端端口释放后自动重连")
            else:
                self._active.discard(tunnel_id)
                runtime.next_retry_at = None
                self._publish_runtime(tunnel_id)
            return listener, message

    def get_log(self, tunnel_id: str) -> str:
        self._get_tunnel(tunnel_id)
        return read_log(self.settings.logs_dir / f"{tunnel_id}.log")

    def clear_log(self, tunnel_id: str) -> None:
        self._get_tunnel(tunnel_id)
        clear_log(self.settings.logs_dir / f"{tunnel_id}.log")
        self.broker.publish({"type": "log_cleared", "tunnel_id": tunnel_id})

    def forget_tunnel(self, tunnel_id: str) -> None:
        self._active.discard(tunnel_id)
        self._stopping.discard(tunnel_id)
        self._runtimes.pop(tunnel_id, None)
        self._retry_attempts.pop(tunnel_id, None)
        self._stop_tail(tunnel_id)

    def _clear_record(self, tunnel_id: str) -> None:
        self._records.pop(tunnel_id, None)
        self._handles.pop(tunnel_id, None)
        delete_record(self.settings.records_dir, tunnel_id)

    def _start_tail(self, tunnel_id: str) -> None:
        existing = self._tail_tasks.get(tunnel_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._tail_log(tunnel_id))
        self._tail_tasks[tunnel_id] = task

    def _stop_tail(self, tunnel_id: str) -> None:
        task = self._tail_tasks.pop(tunnel_id, None)
        if task is not None:
            task.cancel()

    async def _tail_log(self, tunnel_id: str) -> None:
        path = self.settings.logs_dir / f"{tunnel_id}.log"
        try:
            position = path.stat().st_size
        except FileNotFoundError:
            position = 0
        try:
            while not self._shutdown:
                await asyncio.sleep(0.7)
                try:
                    size = path.stat().st_size
                    if size < position:
                        position = 0
                    if size <= position:
                        continue
                    with path.open("rb") as handle:
                        handle.seek(position)
                        data = handle.read(256 * 1024)
                        position = handle.tell()
                except FileNotFoundError:
                    position = 0
                    continue
                text = data.decode("utf-8", "replace")
                lines = [redact_text(line, 4000) for line in text.splitlines() if line]
                if lines:
                    self.broker.publish(
                        {"type": "log", "tunnel_id": tunnel_id, "lines": lines}
                    )
        except asyncio.CancelledError:
            return

    async def _monitor_loop(self) -> None:
        try:
            while not self._shutdown:
                await asyncio.sleep(self.settings.monitor_interval)
                current_wall = time.time()
                wake_gap = current_wall - self._last_wall_time
                self._last_wall_time = current_wall
                wake_detected = wake_gap > max(30, self.settings.monitor_interval * 3)
                current_path = await asyncio.to_thread(self._read_network_path)
                path_changed = current_path != self._network_fingerprint
                self._network_fingerprint = current_path
                if wake_detected:
                    self.broker.publish({"type": "system", "message": "检测到系统唤醒，正在重新诊断"})
                if path_changed:
                    self.broker.publish({"type": "system", "message": "网络路径发生变化，正在重新诊断"})
                for tunnel_id in tuple(self._active):
                    try:
                        await self._monitor_tunnel(
                            tunnel_id,
                            force=wake_detected or path_changed,
                        )
                    except (SupervisorError, OSError):
                        continue
                await asyncio.gather(
                    *(self._monitor_idle(tunnel.id) for tunnel in self.list_tunnels()),
                    return_exceptions=True,
                )
        except asyncio.CancelledError:
            return

    async def _monitor_idle(self, tunnel_id: str) -> None:
        tunnel = self._get_tunnel(tunnel_id)
        if not self._idle_can_connect(tunnel):
            return
        if not await self.diagnostics.quick_network_ok(tunnel):
            return
        async with self._lock(tunnel_id):
            tunnel = self._get_tunnel(tunnel_id)
            if not self._shutdown and self._idle_can_connect(tunnel):
                await self._attempt_start(tunnel)

    def _idle_can_connect(self, tunnel: Tunnel) -> bool:
        return (
            tunnel.auto_reconnect
            and not tunnel.manually_stopped
            and tunnel.id not in self._active
            and self._runtime(tunnel.id).state
            in {"stopped", "target_unreachable", "ssh_unreachable"}
        )

    async def _monitor_tunnel(self, tunnel_id: str, *, force: bool) -> None:
        async with self._lock(tunnel_id):
            if tunnel_id not in self._active or tunnel_id in self._stopping:
                return
            runtime = self._runtime(tunnel_id)
            record = self._records.get(tunnel_id)
            if record is not None:
                if not verify_record(record):
                    self._handle_process_exit(tunnel_id, record, -1)
                    return
                if (
                    runtime.state == "reconnecting"
                    and runtime.next_retry_at is not None
                    and utc_now() < runtime.next_retry_at
                    and not force
                ):
                    return
                await self._check_managed(tunnel_id, record)
                if runtime.stable_since is not None:
                    stable_seconds = (utc_now() - runtime.stable_since).total_seconds()
                    if stable_seconds >= 60:
                        self._retry_attempts[tunnel_id] = 0
                return

            if runtime.external_tunnel:
                tunnel = self._get_tunnel(tunnel_id)
                runtime.external_tunnel = False
                self._enter_port_conflict(tunnel, "此前复用的监听不属于本应用，已停止将其认定为健康隧道")
                return

            if runtime.next_retry_at is None:
                return
            tunnel = self._get_tunnel(tunnel_id)
            if not tunnel.auto_reconnect or tunnel.manually_stopped:
                self._active.discard(tunnel_id)
                runtime.next_retry_at = None
                self._publish_runtime(tunnel_id)
                return
            if force and runtime.state in TRANSIENT_STATES:
                runtime.next_retry_at = utc_now()
            if utc_now() >= runtime.next_retry_at:
                await self._attempt_start(tunnel)
                return
            if runtime.state in NETWORK_WAIT_STATES:
                network_ok = await self.diagnostics.quick_network_ok(tunnel)
                if network_ok:
                    await self._attempt_start(tunnel)

    def _read_network_path(self) -> str:
        try:
            result = subprocess.run(
                ["/sbin/route", "-n", "get", "default"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
                check=False,
            )
            values: dict[str, str] = {}
            for line in result.stdout.splitlines():
                key, _, value = line.partition(":")
                values[key.strip()] = value.strip()
            return f"{values.get('interface', '')}|{values.get('gateway', '')}"
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"

