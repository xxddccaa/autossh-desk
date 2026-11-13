import { Copy, Info, Terminal } from "lucide-react";
import type { TunnelFields } from "../lib/api";
import { remoteCommands, sshCommand } from "../lib/commands";
import { copyText } from "../lib/format";

type Notify = (message: string, tone: "success" | "error") => void;

function CommandBlock({
  title,
  command,
  onNotify,
}: {
  title: string;
  command: string;
  onNotify: Notify;
}) {
  return (
    <div className="overflow-hidden rounded-xl border border-[#dbe4dc] bg-[#f3f7f2]">
      <div className="flex items-center justify-between gap-3 border-b border-[#e1e8df] px-3 py-2">
        <span className="text-[11px] font-semibold text-[#5d7264]">{title}</span>
        <button
          type="button"
          aria-label={`复制${title}`}
          onClick={() => {
            void copyText(command).then(
              () => onNotify(`${title}已复制`, "success"),
              () => onNotify("复制失败，请检查浏览器剪贴板权限", "error"),
            );
          }}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-md px-2 py-1 text-[11px] font-medium text-[#476750] hover:bg-white"
        >
          <Copy size={12} /> 复制
        </button>
      </div>
      <pre className="overflow-x-auto px-3 py-3 font-mono text-[11px] leading-6 text-[#294c36]">
        <code>{command}</code>
      </pre>
    </div>
  );
}

export function RemoteSetupPanel({ tunnel, onNotify }: { tunnel: TunnelFields; onNotify: Notify }) {
  const commands = remoteCommands(tunnel);

  return (
    <section aria-labelledby="remote-setup-heading" className="rounded-[24px] border border-[#dce4db] bg-[#fbfcf9] p-5 shadow-[0_8px_30px_rgba(31,55,40,0.035)]">
      <div className="flex items-start gap-3">
        <Terminal size={19} className="mt-0.5 shrink-0 text-[#668570]" />
        <div className="min-w-0">
          <h2 id="remote-setup-heading" className="text-base font-semibold text-[#203429]">远端服务器操作</h2>
          <p className="mt-1 text-xs leading-5 text-[#718078]">
            先确保隧道已连接，再登录 SSH 远端主机 <code className="break-all">{tunnel.ssh_target}</code> 执行；不是在本机 Mac 执行。这里只生成建议指令，不会自动修改服务器。root 登录且未安装 sudo 时可省略 sudo，并用 vi /etc/hosts 代替 sudoedit。
          </p>
        </div>
      </div>

      <div className="mt-4">
        <CommandBlock title="登录远端主机" command={sshCommand(tunnel)} onNotify={onNotify} />
      </div>

      <div className="mt-4 flex gap-2 rounded-xl border border-[#dee7dc] bg-[#f2f7ee] px-3.5 py-3 text-xs leading-5 text-[#5e7463]">
        <Info size={15} className="mt-0.5 shrink-0" />
        <p>
          建议服务入口：<code className="font-semibold">{commands.serviceEndpoint}</code>，仅远端本机可访问。回环地址按隧道监听端口固定生成，避免多个同端口服务争用同一入口。以下命令适用于远端 Linux；已有地址规划时，请同步替换 socat 和 hosts 中的入口地址。
        </p>
      </div>

      <div className="mt-5 grid gap-5 xl:grid-cols-2">
        <div className="min-w-0 space-y-3">
          <h3 className="text-sm font-semibold text-[#304b3a]">1. socat：提供原服务端口</h3>
          <p className="text-xs leading-5 text-[#718078]">
            <code>{commands.serviceEndpoint}</code> → <code>{commands.listenerEndpoint}</code>。
            socat 连接 SSH 的远端监听，不直接连接目标域名。先确认已安装 socat，且该入口地址和端口未被其他服务占用。
          </p>
          <details className="rounded-xl border border-[#e2e8df] px-3 py-2 text-xs text-[#65796b]">
            <summary className="cursor-pointer font-medium">先检查入口端口占用</summary>
            <div className="mt-3">
              <CommandBlock title="入口端口检查命令" command={commands.portCheck} onNotify={onNotify} />
              <p className="mt-2 leading-5">核对监听地址和用途，不要直接终止其他进程；通配地址监听也可能占用此入口。</p>
            </div>
          </details>
          {commands.socat ? (
            <CommandBlock title="socat 命令" command={commands.socat} onNotify={onNotify} />
          ) : (
            <p className="rounded-xl bg-[#f0f7ee] p-3 text-xs leading-5 text-[#51725a]">
              SSH 隧道已覆盖该入口地址和服务端口，无需额外启动 socat；否则会造成监听冲突。
            </p>
          )}
          <p className="text-xs leading-5 text-[#7b8980]">
            socat 前台运行，Ctrl+C 停止；另开 SSH 终端修改 hosts。长期使用请交给远端服务管理器托管，AutoSSH Desk 不负责启动或重启远端 socat。
          </p>
          <details className="rounded-xl border border-[#e2e8df] px-3 py-2 text-xs text-[#65796b]">
            <summary className="cursor-pointer font-medium">远端是 macOS 时</summary>
            <div className="mt-3 space-y-2">
              <p className="leading-5">先为 lo0 添加建议回环地址，再运行 socat。Linux 通常不需要此步骤。</p>
              <CommandBlock title="macOS 回环别名命令" command={commands.macLoopback} onNotify={onNotify} />
            </div>
          </details>
        </div>

        <div className="min-w-0 space-y-3">
          <h3 className="text-sm font-semibold text-[#304b3a]">2. /etc/hosts：保留目标域名</h3>
          {commands.hostsEntry ? (
            <>
              <p className="text-xs leading-5 text-[#718078]">
                仅在远端备份并编辑 /etc/hosts。核对已有同名记录，合并或替换该域名的映射，保留其他域名和系统记录；不要重复追加。
              </p>
              <CommandBlock title="hosts 备份与编辑命令" command={commands.hostsEdit} onNotify={onNotify} />
              <CommandBlock title="hosts 映射" command={commands.hostsEntry} onNotify={onNotify} />
              <p className="text-xs leading-5 text-[#7b8980]">
                保存后，远端客户端使用原域名 <code className="break-all">{tunnel.target_host}</code> 和原端口 <code>{tunnel.target_port}</code>。
                HTTPS 应保留域名做 SNI 和证书验证，不要关闭证书校验。目标服务仍由运行 autossh 的本机解析并连接。
              </p>
            </>
          ) : (
            <p className="rounded-xl border border-[#e2e8df] bg-[#f6f8f4] p-3 text-xs leading-6 text-[#718078]">
              {commands.hostsNotice} 服务入口：<code>{commands.serviceEndpoint}</code>。
            </p>
          )}
          <p className="text-xs leading-5 text-[#7b8980]">
            这些操作仅建立访问路径，TCP 可达不等于服务协议验证通过。撤销时停止对应 socat，并仅移除本次添加的 hosts 映射。
          </p>
        </div>
      </div>
    </section>
  );
}
