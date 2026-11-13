import type { TunnelFields } from "./api";

export function shellQuote(value: string): string {
  return `'${value.replaceAll("'", "'\"'\"'")}'`;
}

export function listenerConnectHost(host: string): string {
  return host === "0.0.0.0" || host === "localhost" ? "127.0.0.1" : host;
}

export function sshCommand(
  tunnel: Pick<TunnelFields, "ssh_target" | "ssh_port" | "identity_file">,
  options: string[] = [],
): string {
  const args = ["ssh", ...options];
  if (tunnel.ssh_port) args.push("-p", String(tunnel.ssh_port));
  if (tunnel.identity_file) args.push("-i", shellQuote(tunnel.identity_file));
  args.push(shellQuote(tunnel.ssh_target));
  return args.join(" ");
}

export function suggestedLoopbackHost(remotePort: number): string {
  return `127.1.${Math.floor(remotePort / 256)}.${remotePort % 256}`;
}

export function remoteCommands(
  tunnel: Pick<TunnelFields, "remote_bind_host" | "remote_port" | "target_host" | "target_port">,
) {
  const serviceHost = suggestedLoopbackHost(tunnel.remote_port);
  const listenerHost = listenerConnectHost(tunnel.remote_bind_host);
  const listenerEndpoint = `${listenerHost}:${tunnel.remote_port}`;
  const serviceEndpoint = `${serviceHost}:${tunnel.target_port}`;
  const alreadyListening = tunnel.remote_port === tunnel.target_port && (
    tunnel.remote_bind_host === "0.0.0.0" || listenerHost === serviceHost
  );
  const hostname = tunnel.target_host.toLowerCase().replace(/\.$/, "");
  const literalAddress = /^[\d.]+$/.test(hostname) || hostname.includes(":");
  const reservedHostname = hostname === "localhost" || hostname.endsWith(".localhost")
    || hostname === "localhost.localdomain";
  const hostsNotice = literalAddress
    ? "目标是 IP 地址，/etc/hosts 不能重映射 IP。无需修改 hosts，请让远端客户端使用服务入口地址和端口。"
    : reservedHostname
      ? "目标是系统回环名称，不应改写 localhost 记录。无需修改 hosts，请让远端客户端使用服务入口地址和端口。"
      : null;

  return {
    serviceHost,
    serviceEndpoint,
    listenerEndpoint,
    portCheck: [
      "if command -v ss >/dev/null 2>&1; then",
      `  sudo ss -ltnp ${shellQuote(`sport = :${tunnel.target_port}`)}`,
      "else",
      `  sudo lsof -nP -iTCP:${tunnel.target_port} -sTCP:LISTEN`,
      "fi",
    ].join("\n"),
    macLoopback: `sudo ifconfig lo0 alias ${shellQuote(serviceHost)}`,
    socat: alreadyListening ? null : [
      "sudo socat",
      shellQuote(`TCP4-LISTEN:${tunnel.target_port},bind=${serviceHost},reuseaddr,fork`),
      shellQuote(`TCP4:${listenerEndpoint}`),
    ].join(" "),
    hostsEdit: [
      'sudo cp -p /etc/hosts "/etc/hosts.autossh-desk.$(date +%Y%m%d-%H%M%S).bak"',
      "sudoedit /etc/hosts",
    ].join("\n"),
    hostsEntry: hostsNotice ? null : `${serviceHost} ${tunnel.target_host}`,
    hostsNotice,
  };
}
