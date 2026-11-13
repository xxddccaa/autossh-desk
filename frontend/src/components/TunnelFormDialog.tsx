import * as Switch from "@radix-ui/react-switch";
import { KeyRound, LoaderCircle, Network, ServerCog } from "lucide-react";
import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import type { Tunnel, TunnelFields } from "../lib/api";
import { Modal } from "./Modal";

interface Props {
  open: boolean;
  tunnel: Tunnel | null;
  onOpenChange: (open: boolean) => void;
  onSubmit: (payload: TunnelFields) => Promise<void>;
}

interface FormState {
  name: string;
  ssh_target: string;
  ssh_port: string;
  remote_bind_host: string;
  remote_port: string;
  target_host: string;
  target_port: string;
  identity_file: string;
  server_alive_interval: string;
  server_alive_count_max: string;
  connect_timeout: string;
  auto_reconnect: boolean;
  auto_start: boolean;
}

const emptyForm: FormState = {
  name: "新隧道",
  ssh_target: "root@remote.example.com",
  ssh_port: "",
  remote_bind_host: "127.0.0.1",
  remote_port: "15432",
  target_host: "service.example.com",
  target_port: "5432",
  identity_file: "",
  server_alive_interval: "30",
  server_alive_count_max: "3",
  connect_timeout: "10",
  auto_reconnect: true,
  auto_start: false,
};

function formFromTunnel(tunnel: Tunnel | null): FormState {
  if (!tunnel) return emptyForm;
  return {
    name: tunnel.name,
    ssh_target: tunnel.ssh_target,
    ssh_port: tunnel.ssh_port ? String(tunnel.ssh_port) : "",
    remote_bind_host: tunnel.remote_bind_host,
    remote_port: String(tunnel.remote_port),
    target_host: tunnel.target_host,
    target_port: String(tunnel.target_port),
    identity_file: tunnel.identity_file ?? "",
    server_alive_interval: String(tunnel.server_alive_interval),
    server_alive_count_max: String(tunnel.server_alive_count_max),
    connect_timeout: String(tunnel.connect_timeout),
    auto_reconnect: tunnel.auto_reconnect,
    auto_start: tunnel.auto_start,
  };
}

function Label({ children, hint }: { children: ReactNode; hint?: string }) {
  return (
    <label className="mb-2 flex items-center justify-between text-xs font-semibold text-[#33463b]">
      <span>{children}</span>
      {hint ? <span className="font-normal text-[#87938b]">{hint}</span> : null}
    </label>
  );
}

const inputClass =
  "h-11 w-full rounded-xl border border-[#dce3da] bg-white px-3.5 text-sm text-[#1e2d25] outline-none transition placeholder:text-[#a3ada6] focus:border-[#548064] focus:ring-3 focus:ring-[#548064]/10";

function FormSwitch({
  checked,
  onCheckedChange,
  label,
  description,
}: {
  checked: boolean;
  onCheckedChange: (value: boolean) => void;
  label: string;
  description: string;
}) {
  return (
    <div className="flex items-center justify-between rounded-xl border border-[#e0e5dc] bg-white px-4 py-3.5">
      <div>
        <div className="text-sm font-medium text-[#26382e]">{label}</div>
        <div className="mt-0.5 text-xs text-[#7b887f]">{description}</div>
      </div>
      <Switch.Root
        checked={checked}
        onCheckedChange={onCheckedChange}
        className="relative h-6 w-11 shrink-0 rounded-full bg-[#d5ddd5] transition data-[state=checked]:bg-[#3d7652]"
      >
        <Switch.Thumb className="block h-5 w-5 translate-x-0.5 rounded-full bg-white shadow-sm transition-transform data-[state=checked]:translate-x-[22px]" />
      </Switch.Root>
    </div>
  );
}

export function TunnelFormDialog({ open, tunnel, onOpenChange, onSubmit }: Props) {
  const [form, setForm] = useState<FormState>(() => formFromTunnel(tunnel));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setForm(formFromTunnel(tunnel));
      setError(null);
    }
  }, [open, tunnel]);

  const update = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm((current) => ({ ...current, [key]: value }));
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await onSubmit({
        name: form.name.trim(),
        ssh_target: form.ssh_target.trim(),
        ssh_port: form.ssh_port ? Number(form.ssh_port) : null,
        remote_bind_host: form.remote_bind_host.trim(),
        remote_port: Number(form.remote_port),
        target_host: form.target_host.trim(),
        target_port: Number(form.target_port),
        identity_file: form.identity_file.trim() || null,
        server_alive_interval: Number(form.server_alive_interval),
        server_alive_count_max: Number(form.server_alive_count_max),
        connect_timeout: Number(form.connect_timeout),
        auto_reconnect: form.auto_reconnect,
        auto_start: form.auto_start,
      });
      onOpenChange(false);
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title={tunnel ? "编辑隧道" : "新增反向隧道"}
      description={
        tunnel
          ? "运行中的隧道需要先停止；SSH 密码或私钥口令不会写入配置。"
          : "创建一个由本机 autossh 管理的远端 TCP 转发。"
      }
      width="wide"
    >
      <form onSubmit={submit}>
        <div className="space-y-7 px-7 py-6">
          <section>
            <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-[#1d3025]">
              <ServerCog size={17} className="text-[#557964]" />
              基本信息
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div className="col-span-2">
                <Label>名称</Label>
                <input
                  required
                  maxLength={80}
                  className={inputClass}
                  value={form.name}
                  onChange={(event) => update("name", event.target.value)}
                  placeholder="例如：内部服务隧道"
                />
              </div>
              <div>
                <Label hint="Host alias 或 user@host">SSH 目标</Label>
                <input
                  required
                  className={inputClass}
                  value={form.ssh_target}
                  onChange={(event) => update("ssh_target", event.target.value)}
                  placeholder="remote-host 或 root@remote.example.com"
                />
              </div>
              <div>
                <Label hint="留空使用 ~/.ssh/config">SSH 端口</Label>
                <input
                  type="number"
                  min={1}
                  max={65535}
                  className={inputClass}
                  value={form.ssh_port}
                  onChange={(event) => update("ssh_port", event.target.value)}
                  placeholder="22"
                />
              </div>
              <div className="col-span-2">
                <Label hint="可留空，优先使用 SSH Agent / Keychain">私钥路径</Label>
                <div className="relative">
                  <KeyRound
                    size={15}
                    className="pointer-events-none absolute top-1/2 left-3.5 -translate-y-1/2 text-[#8c998f]"
                  />
                  <input
                    className={`${inputClass} pl-10`}
                    value={form.identity_file}
                    onChange={(event) => update("identity_file", event.target.value)}
                    placeholder="~/.ssh/id_ed25519"
                  />
                </div>
              </div>
            </div>
          </section>

          <section>
            <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-[#1d3025]">
              <Network size={17} className="text-[#557964]" />
              转发链路
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <Label>远端监听地址</Label>
                <input
                  required
                  className={inputClass}
                  value={form.remote_bind_host}
                  onChange={(event) => update("remote_bind_host", event.target.value)}
                />
              </div>
              <div>
                <Label>远端监听端口</Label>
                <input
                  required
                  type="number"
                  min={1}
                  max={65535}
                  className={inputClass}
                  value={form.remote_port}
                  onChange={(event) => update("remote_port", event.target.value)}
                />
              </div>
              <div>
                <Label>目标主机</Label>
                <input
                  required
                  className={inputClass}
                  value={form.target_host}
                  onChange={(event) => update("target_host", event.target.value)}
                />
              </div>
              <div>
                <Label>目标端口</Label>
                <input
                  required
                  type="number"
                  min={1}
                  max={65535}
                  className={inputClass}
                  value={form.target_port}
                  onChange={(event) => update("target_port", event.target.value)}
                />
              </div>
            </div>
          </section>

          <section>
            <div className="mb-4 text-sm font-semibold text-[#1d3025]">连接策略</div>
            <div className="grid grid-cols-3 gap-4">
              <div>
                <Label>存活间隔（秒）</Label>
                <input
                  required
                  type="number"
                  min={1}
                  max={3600}
                  className={inputClass}
                  value={form.server_alive_interval}
                  onChange={(event) => update("server_alive_interval", event.target.value)}
                />
              </div>
              <div>
                <Label>失败次数</Label>
                <input
                  required
                  type="number"
                  min={1}
                  max={10}
                  className={inputClass}
                  value={form.server_alive_count_max}
                  onChange={(event) => update("server_alive_count_max", event.target.value)}
                />
              </div>
              <div>
                <Label>连接超时（秒）</Label>
                <input
                  required
                  type="number"
                  min={1}
                  max={120}
                  className={inputClass}
                  value={form.connect_timeout}
                  onChange={(event) => update("connect_timeout", event.target.value)}
                />
              </div>
            </div>
            <div className="mt-4 grid grid-cols-2 gap-3">
              <FormSwitch
                checked={form.auto_reconnect}
                onCheckedChange={(value) => update("auto_reconnect", value)}
                label="自动重连"
                description="网络恢复、断线或端口释放后自动连接"
              />
              <FormSwitch
                checked={form.auto_start}
                onCheckedChange={(value) => update("auto_start", value)}
                label="随应用启动"
                description="打开 AutoSSH Desk 时立即尝试连接"
              />
            </div>
          </section>

          {error ? (
            <div className="rounded-xl border border-[#efcaca] bg-[#fff4f4] px-4 py-3 text-sm text-[#a63b3b]">
              {error}
            </div>
          ) : null}
        </div>

        <div className="flex items-center justify-between border-t border-[#e5e9e2] bg-[#f7f9f5] px-7 py-4">
          <p className="max-w-md text-xs leading-5 text-[#7e8b82]">
            应用直接调用 <code>/usr/bin/ssh</code> 与 Homebrew autossh；不会读取或保存认证内容。
          </p>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => onOpenChange(false)}
              className="h-10 rounded-xl border border-[#d7ded5] bg-white px-4 text-sm font-medium text-[#506058] transition hover:bg-[#f0f3ed]"
            >
              取消
            </button>
            <button
              type="submit"
              disabled={saving}
              className="inline-flex h-10 items-center gap-2 rounded-xl bg-[#244e37] px-5 text-sm font-medium text-white shadow-sm transition hover:bg-[#1c412d] disabled:opacity-60"
            >
              {saving ? <LoaderCircle size={15} className="animate-spin" /> : null}
              {tunnel ? "保存修改" : "创建隧道"}
            </button>
          </div>
        </div>
      </form>
    </Modal>
  );
}

