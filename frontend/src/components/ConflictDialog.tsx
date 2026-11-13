import {
  AlertTriangle,
  Check,
  Clipboard,
  LoaderCircle,
  RefreshCw,
  ShieldAlert,
  Terminal,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api, type ConflictPreview, type TunnelStatus } from "../lib/api";
import { copyText, formatTime } from "../lib/format";
import { Modal } from "./Modal";

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  status: TunnelStatus | null;
  onCleaned: (message: string) => void;
  onManualCommandCopied?: () => void;
}

export function ConflictDialog({
  open,
  onOpenChange,
  status,
  onCleaned,
  onManualCommandCopied,
}: Props) {
  const [preview, setPreview] = useState<ConflictPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [cleaning, setCleaning] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const inspect = useCallback(async () => {
    setAcknowledged(false);
    if (!status) return;
    setLoading(true);
    setError(null);
    setPreview(null);
    try {
      setPreview(await api.previewConflict(status.tunnel.id));
    } catch (inspectError) {
      setError(inspectError instanceof Error ? inspectError.message : "远端诊断失败");
    } finally {
      setLoading(false);
    }
  }, [status]);

  useEffect(() => {
    if (open) void inspect();
    if (!open) {
      setPreview(null);
      setError(null);
      setAcknowledged(false);
    }
  }, [inspect, open]);

  const clean = async () => {
    if (!status || !preview?.can_clean || !acknowledged) return;
    setCleaning(true);
    setError(null);
    try {
      const result = await api.cleanConflict(status.tunnel.id, preview.fingerprint);
      onCleaned(result.message);
      onOpenChange(false);
    } catch (cleanError) {
      setError(cleanError instanceof Error ? cleanError.message : "清理失败");
      setPreview(null);
    } finally {
      setCleaning(false);
    }
  };

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title="检查远端端口占用"
      description="只处理当前隧道、当前监听端口的单一 sshd 会话。"
      width="wide"
    >
      <div className="space-y-5 px-7 py-6">
        <div className="flex items-start gap-3 rounded-2xl border border-[#ecd7a9] bg-[#fff9ea] p-4 text-[#76571d]">
          <ShieldAlert size={20} className="mt-0.5 shrink-0" />
          <div className="text-sm leading-6">
            先重新读取监听 PID、进程名和命令行，再检查 TCP 监听。TCP 可达不能验证转发目标；
            清理会中断该 sshd 会话的全部转发。无法唯一确认 sshd 时会禁止清理并给出手工命令。
          </div>
        </div>

        {loading ? (
          <div className="flex min-h-48 items-center justify-center gap-3 text-sm text-[#647269]">
            <LoaderCircle size={18} className="animate-spin" /> 正在通过 SSH 检查远端端口...
          </div>
        ) : error ? (
          <div className="rounded-2xl border border-[#efcaca] bg-[#fff5f5] p-4 text-sm leading-6 text-[#a33e3e]">
            <div className="flex items-center gap-2 font-semibold">
              <AlertTriangle size={17} /> 未执行任何终止操作
            </div>
            <p className="mt-1">{error}</p>
          </div>
        ) : preview ? (
          <>
            <div className="grid grid-cols-3 gap-3">
              <div className="rounded-xl border border-[#e1e6df] bg-[#f8faf7] p-4">
                <div className="text-[11px] font-semibold tracking-wide text-[#819087] uppercase">
                  监听端口
                </div>
                <div className="mt-1.5 font-mono text-lg font-semibold text-[#26382e]">
                  {preview.remote_listener.address}:{preview.remote_listener.port}
                </div>
              </div>
              <div className="rounded-xl border border-[#e1e6df] bg-[#f8faf7] p-4">
                <div className="text-[11px] font-semibold tracking-wide text-[#819087] uppercase">
                  TCP 监听
                </div>
                <div
                  className={`mt-2 inline-flex items-center gap-1.5 text-sm font-semibold ${
                    preview.end_to_end_ok ? "text-[#9a7422]" : "text-[#ae4141]"
                  }`}
                >
                  {preview.end_to_end_ok ? <Check size={15} /> : <AlertTriangle size={15} />}
                  {preview.end_to_end_ok ? "可达，目标未验证" : "失败 / 超时"}
                </div>
              </div>
              <div className="rounded-xl border border-[#e1e6df] bg-[#f8faf7] p-4">
                <div className="text-[11px] font-semibold tracking-wide text-[#819087] uppercase">
                  确认有效期
                </div>
                <div className="mt-1.5 text-sm font-medium text-[#405347]">
                  {formatTime(preview.expires_at)}
                </div>
              </div>
            </div>

            <div className="overflow-hidden rounded-2xl border border-[#dfe5dc]">
              <div className="border-b border-[#e5e9e3] bg-[#f5f8f3] px-4 py-3 text-xs font-semibold text-[#4e6255]">
                远端进程
              </div>
              {preview.remote_listener.processes.length ? (
                <div className="divide-y divide-[#edf0eb]">
                  {preview.remote_listener.processes.map((process) => (
                    <div key={process.pid} className="grid grid-cols-[90px_90px_1fr] gap-4 px-4 py-3.5 text-xs">
                      <div>
                        <div className="text-[#91a097]">PID</div>
                        <div className="mt-1 font-mono text-sm font-semibold text-[#293b30]">
                          {process.pid}
                        </div>
                      </div>
                      <div>
                        <div className="text-[#91a097]">进程 / 用户</div>
                        <div className="mt-1 font-mono text-sm text-[#293b30]">
                          {process.comm} / {process.user}
                        </div>
                      </div>
                      <div>
                        <div className="flex items-center gap-2 text-[#91a097]">
                          命令摘要
                          <span
                            className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${
                              process.is_sshd
                                ? "bg-[#e7f4eb] text-[#34724b]"
                                : "bg-[#fbeaea] text-[#a43d3d]"
                            }`}
                          >
                            {process.is_sshd ? "sshd" : "非 sshd"}
                          </span>
                        </div>
                        <div className="mt-1 truncate font-mono text-[#4b5d51]" title={process.args}>
                          {process.args || "（无命令行）"}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="px-4 py-5 text-sm text-[#7d8981]">
                  无法读取监听进程详情，不能进行自动清理。
                </div>
              )}
            </div>

            <div className="rounded-xl border border-[#e1e6df] bg-white p-4 text-sm leading-6 text-[#53645a]">
              <span className="font-semibold text-[#273b30]">最近诊断：</span>
              {status?.runtime.last_error || preview.message}
            </div>

            {preview.manual_command ? (
              <div>
                <div className="mb-2 flex items-center justify-between">
                  <div className="flex items-center gap-2 text-xs font-semibold text-[#51645a]">
                    <Terminal size={14} /> 手工核对命令
                  </div>
                  <button
                    type="button"
                    onClick={() => {
                      void copyText(preview.manual_command || "").then(() =>
                        onManualCommandCopied?.(),
                      );
                    }}
                    className="inline-flex items-center gap-1.5 text-xs font-medium text-[#52735f] hover:text-[#2f513b]"
                  >
                    <Clipboard size={13} /> 复制
                  </button>
                </div>
                <pre className="overflow-x-auto rounded-xl bg-[#17241e] p-4 font-mono text-xs leading-6 text-[#cbd9d0]">
                  {preview.manual_command}
                </pre>
              </div>
            ) : null}

            <div className="rounded-xl border border-[#efcaca] bg-[#fff8f8] p-4 text-sm leading-6 text-[#854141]">
              {preview.message}
            </div>
            {preview.can_clean ? (
              <label className="flex items-start gap-3 text-sm leading-6 text-[#854141]">
                <input
                  type="checkbox"
                  checked={acknowledged}
                  onChange={(event) => setAcknowledged(event.target.checked)}
                  className="mt-1"
                />
                我已核对用途，确认终止该 sshd 会话的全部转发
              </label>
            ) : null}
          </>
        ) : null}
      </div>

      <div className="flex items-center justify-between border-t border-[#e5e9e2] bg-[#f7f9f5] px-7 py-4">
        <button
          type="button"
          onClick={() => void inspect()}
          disabled={loading || cleaning}
          className="inline-flex h-10 items-center gap-2 rounded-xl border border-[#d7ded5] bg-white px-4 text-sm font-medium text-[#506058] hover:bg-[#f0f3ed] disabled:opacity-50"
        >
          <RefreshCw size={14} className={loading ? "animate-spin" : ""} /> 重新诊断
        </button>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => onOpenChange(false)}
            className="h-10 rounded-xl border border-[#d7ded5] bg-white px-4 text-sm font-medium text-[#506058] hover:bg-[#f0f3ed]"
          >
            取消
          </button>
          <button
            type="button"
            disabled={!preview?.can_clean || !acknowledged || loading || cleaning}
            onClick={() => void clean()}
            className="inline-flex h-10 items-center gap-2 rounded-xl bg-[#a73c3c] px-5 text-sm font-semibold text-white shadow-sm transition hover:bg-[#933333] disabled:cursor-not-allowed disabled:bg-[#d9c4c4]"
          >
            {cleaning ? <LoaderCircle size={15} className="animate-spin" /> : null}
            核对完成，发送 SIGTERM
          </button>
        </div>
      </div>
    </Modal>
  );
}

