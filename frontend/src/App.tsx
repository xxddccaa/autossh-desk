import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  AlertCircle,
  ArrowRight,
  Bell,
  BellOff,
  Check,
  ChevronRight,
  CircleAlert,
  Clipboard,
  Clock3,
  Copy,
  Globe2,
  Info,
  KeyRound,
  Layers3,
  LoaderCircle,
  Network,
  Play,
  Plus,
  RefreshCw,
  RotateCw,
  Server,
  Settings2,
  ShieldAlert,
  ShieldCheck,
  Square,
  Terminal,
  Trash2,
  Wifi,
  WifiOff,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { ConflictDialog } from "./components/ConflictDialog";
import { DeleteDialog } from "./components/DeleteDialog";
import { RemoteSetupPanel } from "./components/RemoteSetupPanel";
import { StatusBadge, StatusDot } from "./components/StatusBadge";
import { TunnelFormDialog } from "./components/TunnelFormDialog";
import { useLiveEvents } from "./hooks/useLiveEvents";
import {
  api,
  type CheckResult,
  type CheckState,
  type Tunnel,
  type TunnelFields,
  type TunnelState,
  type TunnelStatus,
  type TunnelEvent,
} from "./lib/api";
import {
  copyText,
  connectionText,
  elapsedSince,
  formatDuration,
  formatTime,
  stateMeta,
} from "./lib/format";

interface ToastState {
  id: number;
  tone: "success" | "error" | "info";
  message: string;
}

function useClock(): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return now;
}

function checkTone(state: CheckState): {
  className: string;
  icon: ReactNode;
} {
  if (state === "ok") {
    return {
      className: "border-[#d7e7db] bg-[#f6fbf7]",
      icon: <Check size={14} className="text-[#2f8050]" />,
    };
  }
  if (state === "warn") {
    return {
      className: "border-[#ecd9ae] bg-[#fffaf0]",
      icon: <CircleAlert size={14} className="text-[#a57720]" />,
    };
  }
  if (state === "fail") {
    return {
      className: "border-[#edd0d0] bg-[#fff8f8]",
      icon: <AlertCircle size={14} className="text-[#b24545]" />,
    };
  }
  return {
    className: "border-[#e3e7e1] bg-[#f8faf7]",
    icon: <Clock3 size={14} className="text-[#849087]" />,
  };
}

function CheckCard({ check }: { check: CheckResult }) {
  const tone = checkTone(check.state);
  return (
    <div
      role="group"
      aria-label={`${check.label}：${check.state === "ok" ? "通过" : check.state === "warn" ? "待确认" : check.state === "fail" ? "失败" : "等待检查"}`}
      data-testid={`check-${check.key}`}
      data-state={check.state}
      className={`flex min-h-[92px] gap-3 rounded-2xl border p-4 ${tone.className}`}
    >
      <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-white shadow-sm">
        {tone.icon}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center justify-between gap-3">
          <h4 className="text-sm font-semibold text-[#26382e]">{check.label}</h4>
          {check.latency_ms !== null ? (
            <span className="shrink-0 font-mono text-[10px] text-[#829087]">
              {check.latency_ms} ms
            </span>
          ) : null}
        </div>
        <p className="mt-1.5 line-clamp-2 text-xs leading-5 text-[#718078]" title={check.detail}>
          {check.detail || "等待检查"}
        </p>
      </div>
    </div>
  );
}

function InfoCell({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="rounded-xl border border-[#e7ebe4] bg-white px-3.5 py-3">
      <div className="text-[10px] font-semibold tracking-[0.08em] text-[#8b978f] uppercase">
        {label}
      </div>
      <div className={`mt-1.5 text-sm font-medium text-[#2c3d33] ${mono ? "font-mono" : ""}`}>
        {value}
      </div>
    </div>
  );
}

function Sidebar({
  statuses,
  selectedId,
  onSelect,
  onCreate,
  autosshPath,
}: {
  statuses: TunnelStatus[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onCreate: () => void;
  autosshPath: string | null | undefined;
}) {
  const connected = statuses.filter((item) => item.runtime.state === "connected").length;
  const attention = statuses.filter((item) =>
    ["port_conflict", "error", "ssh_unreachable", "target_unreachable"].includes(
      item.runtime.state,
    ),
  ).length;

  return (
    <aside className="flex w-[318px] shrink-0 flex-col bg-[#17251f] px-4 py-5 text-[#edf4ef]">
      <div className="flex items-center gap-3 px-2">
        <div className="relative flex h-10 w-10 items-center justify-center rounded-[13px] bg-[#d8f2d2] text-[#21472f] shadow-[0_8px_30px_rgba(0,0,0,0.15)]">
          <Network size={21} strokeWidth={2.2} />
          <span className="absolute -right-0.5 -bottom-0.5 h-3 w-3 rounded-full border-2 border-[#17251f] bg-[#74c68b]" />
        </div>
        <div>
          <div className="text-[17px] font-semibold tracking-[-0.02em]">AutoSSH Desk</div>
          <div className="mt-0.5 text-[10px] font-medium tracking-[0.14em] text-[#8fa49a] uppercase">
            Reverse tunnel desk
          </div>
        </div>
      </div>

      <button
        type="button"
        onClick={onCreate}
        className="mt-7 flex h-11 w-full items-center justify-center gap-2 rounded-xl bg-[#d8f2d2] text-sm font-semibold text-[#1e402a] shadow-[0_6px_22px_rgba(0,0,0,0.12)] transition hover:bg-[#e1f6dc]"
      >
        <Plus size={16} strokeWidth={2.5} /> 新增隧道
      </button>

      <div className="mt-6 flex items-center justify-between px-2 text-[11px] font-medium tracking-[0.08em] text-[#8ca097] uppercase">
        <span>隧道</span>
        <span>{statuses.length}</span>
      </div>

      <div className="scrollbar-thin mt-3 flex-1 space-y-2 overflow-y-auto">
        {statuses.map((item) => {
          const active = selectedId === item.tunnel.id;
          return (
            <button
              type="button"
              key={item.tunnel.id}
              onClick={() => onSelect(item.tunnel.id)}
              className={`group w-full rounded-2xl border px-3.5 py-3.5 text-left transition ${
                active
                  ? "border-white/10 bg-white/[0.09] shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]"
                  : "border-transparent hover:border-white/[0.06] hover:bg-white/[0.045]"
              }`}
            >
              <div className="flex items-center justify-between gap-3">
                <div className="flex min-w-0 items-center gap-2.5">
                  <StatusDot state={item.runtime.state} />
                  <span className="truncate text-sm font-semibold text-[#f0f6f1]">
                    {item.tunnel.name}
                  </span>
                </div>
                <ChevronRight
                  size={14}
                  className={`shrink-0 transition ${active ? "text-[#b8d5c2]" : "text-[#52675c] group-hover:text-[#8ca097]"}`}
                />
              </div>
              <div className="mt-2 flex items-center gap-1.5 pl-[20px] font-mono text-[10px] text-[#8da197]">
                <span className="truncate">{item.tunnel.ssh_target}</span>
                <span className="text-[#52685d]">/</span>
                <span>:{item.tunnel.remote_port}</span>
              </div>
              <div className="mt-2 pl-[20px]">
                <StatusBadge state={item.runtime.state} compact />
              </div>
            </button>
          );
        })}
        {statuses.length === 0 ? (
          <div className="rounded-2xl border border-dashed border-white/10 px-4 py-8 text-center text-xs leading-6 text-[#8ca097]">
            还没有隧道
            <br />
            点击上方按钮创建
          </div>
        ) : null}
      </div>

      <div className="mt-5 rounded-2xl border border-white/[0.07] bg-black/[0.08] p-3.5">
        <div className="flex items-center justify-between text-xs">
          <span className="text-[#81958b]">运行概况</span>
          <span className="font-medium text-[#dce8df]">
            {connected} 连接 · {attention} 需处理
          </span>
        </div>
        <div className="mt-3 flex items-center gap-2 text-[10px] text-[#8da197]">
          <span
            className={`h-1.5 w-1.5 rounded-full ${
              autosshPath ? "bg-[#6fc98b]" : "bg-[#e08567]"
            }`}
          />
          <span className="truncate">
            {autosshPath ? "autossh 已就绪" : "autossh 未安装"}
          </span>
        </div>
        <div className="mt-2 font-mono text-[9px] text-[#5e7569]">127.0.0.1 · local only</div>
      </div>
    </aside>
  );
}

function EmptyState({ onCreate }: { onCreate: () => void }) {
  return (
    <div className="flex min-h-[calc(100vh-48px)] items-center justify-center">
      <div className="max-w-md text-center">
        <div className="mx-auto flex h-16 w-16 items-center justify-center rounded-3xl border border-[#dbe4dc] bg-white text-[#5d7c68] shadow-sm">
          <Layers3 size={28} />
        </div>
        <h2 className="mt-5 text-2xl font-semibold tracking-[-0.03em] text-[#1e3126]">
          建立第一条反向隧道
        </h2>
        <p className="mt-2 text-sm leading-6 text-[#728078]">
          配置 SSH 端点、远端监听端口与目标服务，AutoSSH Desk 会负责启动检查和进程管理。
        </p>
        <button
          type="button"
          onClick={onCreate}
          className="mt-6 inline-flex h-11 items-center gap-2 rounded-xl bg-[#244e37] px-5 text-sm font-semibold text-white"
        >
          <Plus size={16} /> 新增隧道
        </button>
      </div>
    </div>
  );
}

export default function App() {
  const queryClient = useQueryClient();
  const now = useClock();
  const [selectedId, setSelectedId] = useState<string | null>(() =>
    window.localStorage.getItem("autossh-desk.selectedTunnel") ??
      window.localStorage.getItem("wikitunnel.selectedTunnel"),
  );
  const [toast, setToast] = useState<ToastState | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [editingTunnel, setEditingTunnel] = useState<Tunnel | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [conflictOpen, setConflictOpen] = useState(false);
  const [logsFollow, setLogsFollow] = useState(true);
  const [notificationsEnabled, setNotificationsEnabled] = useState(
    () =>
      (window.localStorage.getItem("autossh-desk.notifications") ??
        window.localStorage.getItem("wikitunnel.notifications")) === "1",
  );
  const logRef = useRef<HTMLDivElement>(null);
  const previousStates = useRef<Record<string, TunnelState>>({});

  const notify = useCallback((message: string, tone: ToastState["tone"] = "info") => {
    setToast({ id: Date.now(), message, tone });
  }, []);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 4200);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const statusQuery = useQuery({
    queryKey: ["statuses"],
    queryFn: api.statuses,
    refetchInterval: 4000,
    refetchIntervalInBackground: true,
  });
  const healthQuery = useQuery({
    queryKey: ["health"],
    queryFn: api.health,
    refetchInterval: 30000,
  });

  const statuses = statusQuery.data ?? [];
  const selected = useMemo(
    () => statuses.find((item) => item.tunnel.id === selectedId) ?? null,
    [selectedId, statuses],
  );

  useEffect(() => {
    if (!statuses.length) return;
    if (!selectedId || !statuses.some((item) => item.tunnel.id === selectedId)) {
      const nextId = statuses[0].tunnel.id;
      setSelectedId(nextId);
      window.localStorage.setItem("autossh-desk.selectedTunnel", nextId);
    }
  }, [selectedId, statuses]);

  useEffect(() => {
    window.localStorage.setItem(
      "autossh-desk.notifications",
      notificationsEnabled ? "1" : "0",
    );
  }, [notificationsEnabled]);

  useEffect(() => {
    for (const item of statuses) {
      const previous = previousStates.current[item.tunnel.id];
      const next = item.runtime.state;
      if (
        previous &&
        previous !== next &&
        notificationsEnabled &&
        typeof Notification !== "undefined" &&
        Notification.permission === "granted" &&
        ["connected", "port_conflict", "ssh_unreachable", "target_unreachable", "error"].includes(
          next,
        )
      ) {
        new Notification(`${item.tunnel.name} · ${stateMeta[next].label}`, {
          body: item.runtime.last_error || stateMeta[next].description,
        });
      }
      previousStates.current[item.tunnel.id] = next;
    }
  }, [notificationsEnabled, statuses]);

  const toggleNotifications = async () => {
    if (notificationsEnabled) {
      setNotificationsEnabled(false);
      notify("状态通知已关闭", "info");
      return;
    }
    if (typeof Notification === "undefined") {
      notify("当前浏览器不支持系统通知", "error");
      return;
    }
    const permission = await Notification.requestPermission();
    if (permission === "granted") {
      setNotificationsEnabled(true);
      notify("状态通知已开启", "success");
    } else {
      notify("通知权限未授予", "error");
    }
  };

  const logsQuery = useQuery({
    queryKey: ["logs", selected?.tunnel.id],
    queryFn: () => api.logs(selected!.tunnel.id),
    enabled: Boolean(selected),
    refetchInterval: selected ? 2000 : false,
  });

  const handleEvent = useCallback(
    (event: TunnelEvent) => {
      if (event.type === "snapshot" && event.statuses) {
        queryClient.setQueryData(["statuses"], event.statuses);
        return;
      }
      if (event.type === "runtime" && event.runtime) {
        const runtime = event.runtime;
        queryClient.setQueryData<TunnelStatus[]>(["statuses"], (current) =>
          current?.map((item) =>
            item.tunnel.id === runtime.tunnel_id ? { ...item, runtime } : item,
          ),
        );
        return;
      }
      if (event.type === "log_cleared") {
        void queryClient.invalidateQueries({ queryKey: ["logs", event.tunnel_id] });
        return;
      }
      if (event.type === "system" && event.message) {
        notify(event.message, "info");
      }
    },
    [notify, queryClient],
  );

  const wsConnected = useLiveEvents({
    onEvent: handleEvent,
    onConnected: () => {
      void queryClient.invalidateQueries({ queryKey: ["statuses"] });
      void queryClient.invalidateQueries({ queryKey: ["logs"] });
    },
  });

  useEffect(() => {
    if (logsFollow && logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [logsFollow, logsQuery.data]);

  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["statuses"] }),
      queryClient.invalidateQueries({ queryKey: ["logs", selected?.tunnel.id] }),
    ]);
  };

  const runAction = async (name: string, action: () => Promise<string | void>) => {
    setBusy(name);
    try {
      const message = await action();
      if (message) notify(message, "success");
      await refresh();
    } catch (error) {
      notify(error instanceof Error ? error.message : "操作失败", "error");
    } finally {
      setBusy(null);
    }
  };

  const start = () => {
    if (!selected) return;
    void runAction("start", async () => (await api.start(selected.tunnel.id)).message);
  };
  const stop = () => {
    if (!selected) return;
    void runAction("stop", async () => {
      await api.stop(selected.tunnel.id);
      return "隧道停止请求已完成";
    });
  };
  const restart = () => {
    if (!selected) return;
    void runAction("restart", async () => (await api.restart(selected.tunnel.id)).message);
  };
  const test = () => {
    if (!selected) return;
    void runAction("test", async () => {
      await api.test(selected.tunnel.id);
      return "完整链路诊断已完成";
    });
  };
  const duplicate = () => {
    if (!selected) return;
    void runAction("duplicate", async () => {
      const created = await api.duplicate(selected.tunnel.id);
      setSelectedId(created.tunnel.id);
      window.localStorage.setItem("autossh-desk.selectedTunnel", created.tunnel.id);
      return "已复制配置，并分配了新的远端端口";
    });
  };
  const copyConnection = () => {
    if (!selected) return;
    void copyText(connectionText(selected.tunnel)).then(
      () => notify("服务连接参数 / SSH 命令已复制", "success"),
      () => notify("复制失败，请检查浏览器剪贴板权限", "error"),
    );
  };
  const copyDiagnostics = () => {
    if (!selected) return;
    const content = [
      `AutoSSH Desk diagnostic: ${selected.tunnel.name}`,
      `Generated: ${new Date().toISOString()}`,
      "",
      JSON.stringify({ tunnel: selected.tunnel, runtime: selected.runtime }, null, 2),
      "",
      "Recent logs:",
      (logsQuery.data ?? "").split("\n").slice(-200).join("\n"),
    ].join("\n");
    void copyText(content).then(
      () => notify("诊断信息已复制", "success"),
      () => notify("复制失败", "error"),
    );
  };
  const clearLogs = () => {
    if (!selected) return;
    void runAction("clearLogs", async () => {
      await api.clearLogs(selected.tunnel.id);
      return "当前隧道日志已清空";
    });
  };
  const saveTunnel = async (payload: TunnelFields) => {
    if (editingTunnel) {
      const updated = await api.update(editingTunnel.id, payload);
      await refresh();
      notify("隧道配置已保存", "success");
      setSelectedId(updated.tunnel.id);
    } else {
      const created = await api.create(payload);
      await refresh();
      setSelectedId(created.tunnel.id);
      window.localStorage.setItem("autossh-desk.selectedTunnel", created.tunnel.id);
      notify("隧道已创建", "success");
    }
  };
  const removeTunnel = async () => {
    if (!selected) return;
    await api.remove(selected.tunnel.id);
    await queryClient.invalidateQueries({ queryKey: ["statuses"] });
    setSelectedId(null);
    notify("隧道配置已删除", "success");
  };

  const openCreate = () => {
    setEditingTunnel(null);
    setFormOpen(true);
  };
  const openEdit = () => {
    if (!selected) return;
    if (selected.runtime.managed) {
      notify("请先停止该隧道，再修改配置", "error");
      return;
    }
    setEditingTunnel(selected.tunnel);
    setFormOpen(true);
  };

  const logs = logsQuery.data ?? "";
  const logLines = logs.split("\n").filter((line, index, list) => line || index === 0);
  const stableSeconds = selected?.runtime.stable_since
    ? elapsedSince(selected.runtime.stable_since, now)
    : 0;
  const connectedLabel = selected?.runtime.state === "connected" ? "已连接" : null;
  const isTransitioning = ["starting", "stopping"].includes(selected?.runtime.state ?? "");
  const selectedState = selected?.runtime.state ?? "stopped";
  const meta = stateMeta[selectedState];

  return (
    <div className="flex min-h-screen bg-[#edf1eb]">
      <Sidebar
        statuses={statuses}
        selectedId={selectedId}
        onSelect={(id) => {
          setSelectedId(id);
          window.localStorage.setItem("autossh-desk.selectedTunnel", id);
        }}
        onCreate={openCreate}
        autosshPath={healthQuery.data?.autossh}
      />

      <main className="min-w-0 flex-1">
        <header className="flex h-[70px] items-center justify-between border-b border-[#dde4dc] bg-[#f5f8f3]/85 px-8 backdrop-blur-xl">
          <div>
            <div className="text-[10px] font-semibold tracking-[0.18em] text-[#859188] uppercase">
              Reverse proxy manager
            </div>
            <div className="mt-1 text-sm font-semibold text-[#34483b]">
              {selected?.tunnel.name ?? "本机隧道控制台"}
            </div>
          </div>
          <div className="flex items-center gap-4">
            <button
              type="button"
              onClick={() => void toggleNotifications()}
              aria-label={notificationsEnabled ? "关闭状态通知" : "开启状态通知"}
              title={notificationsEnabled ? "关闭状态通知" : "开启状态通知"}
              className={`flex h-8 w-8 items-center justify-center rounded-lg border transition ${
                notificationsEnabled
                  ? "border-[#c9dfce] bg-[#edf6ef] text-[#3f7651]"
                  : "border-[#dce3da] bg-white text-[#849188] hover:bg-[#eef2ec]"
              }`}
            >
              {notificationsEnabled ? <Bell size={14} /> : <BellOff size={14} />}
            </button>
            <span className="flex items-center gap-1.5 text-[11px] font-medium text-[#718077]">
              {wsConnected ? (
                <Wifi size={14} className="text-[#46855c]" />
              ) : (
                <WifiOff size={14} className="text-[#a47f3d]" />
              )}
              {wsConnected ? "实时事件已连接" : "轮询模式"}
            </span>
            <span className="h-4 w-px bg-[#d6ddd5]" />
            <span className="font-mono text-[10px] text-[#86938a]">macOS · 127.0.0.1</span>
          </div>
        </header>

        {statusQuery.isError ? (
          <div className="mx-8 mt-5 flex items-center gap-3 rounded-2xl border border-[#eccaca] bg-[#fff7f7] px-4 py-3 text-sm text-[#9e3e3e]">
            <AlertCircle size={17} /> 无法连接本地后端：{statusQuery.error.message}
          </div>
        ) : null}

        {healthQuery.data && !healthQuery.data.autossh ? (
          <div className="mx-8 mt-5 flex items-center justify-between gap-4 rounded-2xl border border-[#ecd6a8] bg-[#fff9eb] px-4 py-3 text-sm text-[#7b5c20]">
            <div className="flex items-center gap-2">
              <AlertCircle size={17} /> 缺少 autossh。安装命令：<code className="font-semibold">brew install autossh</code>
            </div>
            <button
              type="button"
              onClick={() => {
                void copyText("brew install autossh").then(() =>
                  notify("安装命令已复制", "success"),
                );
              }}
              className="inline-flex items-center gap-1.5 rounded-lg border border-[#dfc68e] bg-white/60 px-3 py-1.5 text-xs font-medium"
            >
              <Copy size={12} /> 复制命令
            </button>
          </div>
        ) : null}

        {statusQuery.isLoading ? (
          <div className="flex h-[calc(100vh-70px)] items-center justify-center gap-3 text-sm text-[#6d7d73]">
            <LoaderCircle size={19} className="animate-spin" /> 正在读取隧道配置...
          </div>
        ) : !selected ? (
          <EmptyState onCreate={openCreate} />
        ) : (
          <div className="mx-auto max-w-[1500px] space-y-5 px-8 py-6">
            <section className="rounded-[26px] border border-[#dce4db] bg-[#fbfcf9] p-6 shadow-[0_10px_40px_rgba(31,55,40,0.04)]">
              <div className="flex items-start justify-between gap-6">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-3">
                    <StatusBadge state={selectedState} />
                    {selected.runtime.external_tunnel ? (
                      <span className="rounded-full border border-[#d9e2d9] bg-[#f1f5ef] px-2.5 py-1 text-[10px] font-semibold text-[#67766b]">
                        非受管监听
                      </span>
                    ) : null}
                    {selected.runtime.managed ? (
                      <span className="rounded-full border border-[#d9e2d9] bg-[#f1f5ef] px-2.5 py-1 text-[10px] font-semibold text-[#67766b]">
                        由 AutoSSH Desk 管理
                      </span>
                    ) : null}
                  </div>
                  <h1 className="mt-4 text-[28px] leading-tight font-semibold tracking-[-0.035em] text-[#172a20]">
                    {connectedLabel
                      ? `${connectedLabel} ${formatDuration(stableSeconds)}`
                      : meta.label}
                  </h1>
                  <p className="mt-2 max-w-2xl text-sm leading-6 text-[#718078]">
                    {selected.runtime.last_error && selectedState !== "connected"
                      ? selected.runtime.last_error
                      : selected.runtime.activity || meta.description}
                  </p>
                  {selected.tunnel.auto_reconnect && selected.runtime.next_retry_at ? (
                    <p className="mt-2 flex items-center gap-1.5 text-xs font-medium text-[#587462]">
                      <RefreshCw size={12} /> 自动保持连接 · 下次重试：
                      {formatTime(selected.runtime.next_retry_at)}，无需手动启动。
                    </p>
                  ) : null}
                </div>

                <div className="flex max-w-[520px] flex-wrap justify-end gap-2">
                  <button
                    type="button"
                    onClick={start}
                    disabled={Boolean(busy) || isTransitioning || selected.runtime.managed}
                    className="inline-flex h-10 items-center gap-2 rounded-xl bg-[#28563b] px-4 text-sm font-semibold text-white shadow-sm transition hover:bg-[#20462f] disabled:opacity-45"
                  >
                    {busy === "start" ? <LoaderCircle size={15} className="animate-spin" /> : <Play size={14} fill="currentColor" />}
                    启动
                  </button>
                  <button
                    type="button"
                    onClick={stop}
                    disabled={Boolean(busy) || isTransitioning || (selectedState === "stopped" && !selected.runtime.external_tunnel)}
                    className="inline-flex h-10 items-center gap-2 rounded-xl border border-[#d4ddd4] bg-white px-4 text-sm font-semibold text-[#435549] transition hover:bg-[#f2f5ef] disabled:opacity-45"
                  >
                    <Square size={13} fill="currentColor" /> 停止
                  </button>
                  <button
                    type="button"
                    onClick={restart}
                    disabled={Boolean(busy) || isTransitioning}
                    className="inline-flex h-10 items-center gap-2 rounded-xl border border-[#d4ddd4] bg-white px-4 text-sm font-semibold text-[#435549] transition hover:bg-[#f2f5ef] disabled:opacity-45"
                  >
                    <RotateCw size={14} className={busy === "restart" ? "animate-spin" : ""} /> 重启
                  </button>
                  <button
                    type="button"
                    onClick={test}
                    disabled={Boolean(busy)}
                    className="inline-flex h-10 items-center gap-2 rounded-xl border border-[#d4ddd4] bg-white px-4 text-sm font-semibold text-[#435549] transition hover:bg-[#f2f5ef] disabled:opacity-45"
                  >
                    <Activity size={14} className={busy === "test" ? "animate-pulse" : ""} /> 测试连接
                  </button>
                  <button
                    type="button"
                    onClick={openEdit}
                    disabled={Boolean(busy)}
                    className="inline-flex h-10 items-center gap-2 rounded-xl border border-[#d4ddd4] bg-white px-4 text-sm font-semibold text-[#435549] transition hover:bg-[#f2f5ef] disabled:opacity-45"
                  >
                    <Settings2 size={14} /> 设置
                  </button>
                </div>
              </div>

              <div className="mt-6 flex flex-wrap items-center gap-3 rounded-2xl border border-[#e5eae3] bg-[#f5f8f3] px-4 py-3.5">
                <div className="flex items-center gap-2 text-xs font-semibold text-[#33483b]">
                  <Server size={15} className="text-[#62816e]" /> Mac
                </div>
                <ArrowRight size={14} className="text-[#9eaba2]" />
                <div className="min-w-0 text-xs font-medium text-[#52655a]">
                  <span className="text-[#91a097]">SSH </span>
                  <span className="font-mono">{selected.tunnel.ssh_target}</span>
                </div>
                <ArrowRight size={14} className="text-[#9eaba2]" />
                <div className="text-xs font-medium text-[#52655a]">
                  <span className="text-[#91a097]">远端监听 </span>
                  <span className="font-mono">
                    {selected.tunnel.remote_bind_host}:{selected.tunnel.remote_port}
                  </span>
                </div>
                <ArrowRight size={14} className="text-[#9eaba2]" />
                <div className="min-w-0 flex-1 text-xs font-medium text-[#52655a]">
                  <span className="text-[#91a097]">目标服务 </span>
                  <span className="break-all font-mono">
                    {selected.tunnel.target_host}:{selected.tunnel.target_port}
                  </span>
                </div>
                <button
                  type="button"
                  onClick={copyConnection}
                  className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-[#d9e1d8] bg-white px-3 py-1.5 text-[11px] font-semibold text-[#587062] hover:bg-[#edf2eb]"
                >
                  <Copy size={12} /> 连接参数
                </button>
              </div>
            </section>

            <div className="grid grid-cols-[minmax(0,1.7fr)_minmax(310px,0.8fr)] gap-5">
              <section className="rounded-[24px] border border-[#dce4db] bg-[#fbfcf9] p-5 shadow-[0_8px_30px_rgba(31,55,40,0.035)]">
                <div className="mb-4 flex items-center justify-between">
                  <div>
                    <h2 className="text-base font-semibold tracking-[-0.01em] text-[#203429]">完整链路检查</h2>
                    <p className="mt-1 text-xs text-[#809087]">检查 DNS、目标 TCP、SSH 认证与远端监听；服务协议需另行验证</p>
                  </div>
                  <button
                    type="button"
                    onClick={test}
                    disabled={Boolean(busy)}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-[#dae2d9] bg-white px-3 py-2 text-xs font-semibold text-[#526a5b] hover:bg-[#f1f5ef] disabled:opacity-45"
                  >
                    <RefreshCw size={13} className={busy === "test" ? "animate-spin" : ""} />
                    重新检查
                  </button>
                </div>
                {selected.runtime.checks.length ? (
                  <div className="grid grid-cols-2 gap-3">
                    {selected.runtime.checks.map((check) => (
                      <CheckCard key={check.key} check={check} />
                    ))}
                  </div>
                ) : (
                  <div className="flex min-h-[230px] flex-col items-center justify-center rounded-2xl border border-dashed border-[#dce4db] bg-[#f8faf7] text-center">
                    <Activity size={26} className="text-[#809b89]" />
                    <div className="mt-3 text-sm font-semibold text-[#3e5446]">尚无诊断结果</div>
                    <p className="mt-1 max-w-xs text-xs leading-5 text-[#819087]">
                      启动隧道或点击“测试连接”后会依次执行完整检查。
                    </p>
                  </div>
                )}
              </section>

              <section className="rounded-[24px] border border-[#dce4db] bg-[#fbfcf9] p-5 shadow-[0_8px_30px_rgba(31,55,40,0.035)]">
                <div className="mb-4 flex items-center justify-between">
                  <div>
                    <h2 className="text-base font-semibold tracking-[-0.01em] text-[#203429]">运行信息</h2>
                    <p className="mt-1 text-xs text-[#809087]">进程、稳定时间和重试计划</p>
                  </div>
                  <ShieldCheck size={19} className="text-[#76917f]" />
                </div>
                <div className="grid grid-cols-2 gap-2.5">
                  <InfoCell
                    label="autossh PID"
                    value={selected.runtime.autossh_pid ?? "—"}
                    mono
                  />
                  <InfoCell label="ssh PID" value={selected.runtime.ssh_pid ?? "—"} mono />
                  <InfoCell label="启动时间" value={formatTime(selected.runtime.started_at)} />
                  <InfoCell
                    label="稳定运行"
                    value={selected.runtime.stable_since ? formatDuration(stableSeconds) : "—"}
                  />
                  <InfoCell label="重连次数" value={selected.runtime.reconnect_count} />
                  <InfoCell label="下次重试" value={formatTime(selected.runtime.next_retry_at)} />
                </div>
                <div className="mt-4 rounded-xl border border-[#e6ebe4] bg-[#f6f8f4] p-3.5">
                  <div className="text-[10px] font-semibold tracking-[0.08em] text-[#8b978f] uppercase">
                    最近活动
                  </div>
                  <div className="mt-1.5 text-xs leading-5 text-[#596a60]">
                    {selected.runtime.activity || "暂无活动"}
                  </div>
                </div>
                <div className="mt-3 flex gap-2">
                  <button
                    type="button"
                    onClick={duplicate}
                    disabled={Boolean(busy)}
                    className="flex h-9 flex-1 items-center justify-center gap-1.5 rounded-lg border border-[#dce3da] bg-white text-xs font-semibold text-[#52665a] hover:bg-[#f2f5ef] disabled:opacity-45"
                  >
                    <Layers3 size={13} /> 复制配置
                  </button>
                  <button
                    type="button"
                    onClick={() => setDeleteOpen(true)}
                    className="flex h-9 items-center justify-center gap-1.5 rounded-lg border border-[#ead8d8] bg-white px-3 text-xs font-semibold text-[#a04a4a] hover:bg-[#fff4f4]"
                  >
                    <Trash2 size={13} /> 删除
                  </button>
                </div>
              </section>
            </div>

            <RemoteSetupPanel tunnel={selected.tunnel} onNotify={notify} />

            <div className="grid grid-cols-[minmax(0,1.7fr)_minmax(310px,0.8fr)] gap-5">
              <section className="overflow-hidden rounded-[24px] border border-[#d9e1d9] bg-[#18241e] shadow-[0_12px_40px_rgba(20,35,27,0.12)]">
                <div className="flex items-center justify-between border-b border-white/[0.08] px-5 py-4">
                  <div className="flex items-center gap-2.5">
                    <Terminal size={16} className="text-[#a3c3ae]" />
                    <div>
                      <div className="text-sm font-semibold text-[#edf4ef]">实时日志</div>
                      <div className="mt-0.5 text-[10px] text-[#81978b]">
                        {logs.trim() ? `${logLines.length} 行 · 自动刷新` : "当前隧道暂无日志"}
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center gap-1.5">
                    <button
                      type="button"
                      onClick={copyDiagnostics}
                      className="inline-flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-[11px] font-medium text-[#a6b9ad] hover:bg-white/[0.06] hover:text-white"
                    >
                      <Clipboard size={12} /> 诊断
                    </button>
                    <button
                      type="button"
                      onClick={clearLogs}
                      disabled={Boolean(busy)}
                      className="inline-flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-[11px] font-medium text-[#a6b9ad] hover:bg-white/[0.06] hover:text-white disabled:opacity-45"
                    >
                      <Trash2 size={12} /> 清空
                    </button>
                  </div>
                </div>
                <div
                  ref={logRef}
                  onScroll={(event) => {
                    const node = event.currentTarget;
                    setLogsFollow(node.scrollHeight - node.scrollTop - node.clientHeight < 36);
                  }}
                  className="log-scroll h-[260px] overflow-auto px-5 py-4 font-mono text-[11px] leading-6"
                >
                  {logs.trim() ? (
                    logLines.map((line, index) => (
                      <div
                        key={`${index}-${line.slice(0, 24)}`}
                        className={
                          /error|failed|denied|refused|timeout/i.test(line)
                            ? "text-[#e7a0a0]"
                            : /warning|warn/i.test(line)
                              ? "text-[#dfc68e]"
                              : "text-[#adc3b6]"
                        }
                      >
                        <span className="mr-3 inline-block w-8 select-none text-right text-[#4f6659]">
                          {index + 1}
                        </span>
                        <span className="whitespace-pre-wrap">{line}</span>
                      </div>
                    ))
                  ) : (
                    <div className="flex h-full items-center justify-center text-[#60766a]">
                      启动隧道后，autossh / ssh 输出会显示在这里
                    </div>
                  )}
                </div>
              </section>

              <section className="rounded-[24px] border border-[#dce4db] bg-[#fbfcf9] p-5 shadow-[0_8px_30px_rgba(31,55,40,0.035)]">
                <div className="flex items-center gap-2">
                  <KeyRound size={16} className="text-[#708c7b]" />
                  <h2 className="text-base font-semibold text-[#203429]">SSH 与认证</h2>
                </div>
                <p className="mt-2 text-xs leading-5 text-[#7b8980]">
                  密码和私钥口令由 macOS Keychain 或 SSH Agent 提供，AutoSSH Desk 不读取认证内容。
                </p>
                <div className="mt-4 space-y-2.5">
                  <InfoCell label="SSH Profile" value={selected.tunnel.ssh_target} mono />
                  <InfoCell
                    label="私钥路径"
                    value={selected.tunnel.identity_file || "使用 ~/.ssh/config / Agent"}
                    mono={Boolean(selected.tunnel.identity_file)}
                  />
                  <InfoCell
                    label="ServerAlive"
                    value={`${selected.tunnel.server_alive_interval}s × ${selected.tunnel.server_alive_count_max}`}
                    mono
                  />
                </div>
                <div className="mt-4 grid grid-cols-2 gap-2">
                  <button
                    type="button"
                    onClick={copyConnection}
                    className="flex h-9 items-center justify-center gap-1.5 rounded-lg border border-[#dce3da] bg-white text-xs font-semibold text-[#52665a] hover:bg-[#f2f5ef]"
                  >
                    <Copy size={13} /> 复制参数
                  </button>
                  <button
                    type="button"
                    onClick={() => setConflictOpen(true)}
                    className="flex h-9 items-center justify-center gap-1.5 rounded-lg border border-[#ead8d8] bg-white text-xs font-semibold text-[#a04a4a] hover:bg-[#fff4f4]"
                  >
                    <ShieldAlert size={13} /> 清理端口
                  </button>
                </div>
                <div className="mt-3 flex items-center gap-2 rounded-xl border border-[#e6ebe4] bg-[#f6f8f4] px-3 py-2.5 text-[11px] text-[#718078]">
                  <Globe2 size={13} /> 远端监听
                  <span className="ml-auto font-mono text-[#3e5546]">
                    {selected.runtime.remote_listener.found
                      ? selectedState === "connected" && selected.runtime.managed
                        ? "受管 / 已建立"
                        : "已监听 / 待确认"
                      : "未监听 / 空闲"}
                  </span>
                </div>
              </section>
            </div>
          </div>
        )}
      </main>

      <TunnelFormDialog
        open={formOpen}
        tunnel={editingTunnel}
        onOpenChange={setFormOpen}
        onSubmit={saveTunnel}
      />
      <DeleteDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        status={selected}
        onConfirm={removeTunnel}
      />
      <ConflictDialog
        open={conflictOpen}
        onOpenChange={setConflictOpen}
        status={selected}
        onCleaned={(message) => {
          notify(message, "success");
          void refresh();
        }}
        onManualCommandCopied={() => notify("手工命令已复制", "success")}
      />

      {toast ? (
        <div
          role="status"
          className={`fixed right-6 bottom-6 z-[70] max-w-md rounded-2xl border px-4 py-3 text-sm shadow-[0_18px_60px_rgba(20,35,27,0.2)] backdrop-blur ${
            toast.tone === "success"
              ? "border-[#c9e2d0] bg-[#f3fbf5]/95 text-[#28633e]"
              : toast.tone === "error"
                ? "border-[#eccaca] bg-[#fff7f7]/95 text-[#983c3c]"
                : "border-[#d8e1d8] bg-[#f8fbf7]/95 text-[#4d6255]"
          }`}
        >
          <div className="flex items-start gap-2.5">
            {toast.tone === "success" ? (
              <Check size={16} className="mt-0.5" />
            ) : toast.tone === "error" ? (
              <AlertCircle size={16} className="mt-0.5" />
            ) : (
              <Info size={16} className="mt-0.5" />
            )}
            <span className="leading-5">{toast.message}</span>
          </div>
        </div>
      ) : null}
    </div>
  );
}

