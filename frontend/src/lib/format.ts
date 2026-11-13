import type { TunnelFields, TunnelState } from "./api";
import { listenerConnectHost, shellQuote, sshCommand } from "./commands";

export const stateMeta: Record<
  TunnelState,
  { label: string; tone: "green" | "amber" | "red" | "gray"; description: string }
> = {
  stopped: { label: "未启动", tone: "gray", description: "隧道当前停止" },
  starting: { label: "正在启动", tone: "amber", description: "正在检查并建立连接" },
  connected: { label: "已连接", tone: "green", description: "受管 SSH 转发已建立；服务协议需另行验证" },
  target_unreachable: {
    label: "VPN / 目标不可达",
    tone: "red",
    description: "目标服务或 VPN 网络不可达",
  },
  ssh_unreachable: {
    label: "SSH 不可达",
    tone: "red",
    description: "远端主机的 SSH 端点不可达",
  },
  port_conflict: {
    label: "远端端口冲突",
    tone: "red",
    description: "远端监听端口被非受管会话占用，等待释放；不会自动复用未知监听",
  },
  reconnecting: {
    label: "正在自动重连",
    tone: "amber",
    description: "等待下一次重试",
  },
  error: { label: "错误", tone: "red", description: "连接或诊断失败，请查看错误详情" },
  stopping: { label: "停止中", tone: "gray", description: "正在安全终止进程" },
};

export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 60) return `${Math.floor(seconds)} 秒`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  if (hours < 24) return `${hours} 小时 ${remainingMinutes} 分`;
  const days = Math.floor(hours / 24);
  return `${days} 天 ${hours % 24} 小时`;
}

export function elapsedSince(value: string | null, now = Date.now()): number {
  if (!value) return 0;
  return Math.max(0, (now - new Date(value).getTime()) / 1000);
}

export function formatTime(value: string | null): string {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

export function copyText(text: string): Promise<void> {
  return navigator.clipboard.writeText(text);
}

export function connectionText(tunnel: TunnelFields): string {
  const listenerHost = listenerConnectHost(tunnel.remote_bind_host);
  const forward = `127.0.0.1:${tunnel.remote_port}:${listenerHost}:${tunnel.remote_port}`;
  return [
    `远端客户端入口：${listenerHost}:${tunnel.remote_port}`,
    `目标服务：${tunnel.target_host}:${tunnel.target_port}`,
    "使用目标服务对应的客户端和协议；SSH 隧道只转发 TCP。",
    "",
    "Mac 端可选本地转发命令（先确认本地监听端口空闲）：",
    sshCommand(tunnel, ["-N", "-L", shellQuote(forward)]),
    "",
    "目标主机由运行 autossh 的本机解析和连接。HTTPS 客户端应保留原域名进行 SNI 和证书验证。",
    "服务账号和密码未包含在复制内容中。",
  ].join("\n");
}

