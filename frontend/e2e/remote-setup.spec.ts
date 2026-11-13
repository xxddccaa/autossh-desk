import { execFileSync } from "node:child_process";
import { expect, test, type Page } from "@playwright/test";
import type { TunnelFields, TunnelStatus } from "../src/lib/api";
import { remoteCommands, shellQuote, sshCommand, suggestedLoopbackHost } from "../src/lib/commands";
import { connectionText } from "../src/lib/format";

const service: TunnelFields = {
  name: "API 服务",
  ssh_target: "tunnel-host",
  ssh_port: null,
  remote_bind_host: "127.0.0.1",
  remote_port: 18443,
  target_host: "api.example.com",
  target_port: 443,
  identity_file: null,
  server_alive_interval: 30,
  server_alive_count_max: 3,
  connect_timeout: 10,
  auto_reconnect: true,
  auto_start: false,
};

async function mockServices(page: Page, managed = true) {
  await page.routeWebSocket("**/api/ws", (socket) => socket.close());
  await page.route("**/api/tunnels/*/logs", (route) => route.fulfill({ body: "" }));
  await page.route("**/api/tunnels", async (route) => {
    const response = await route.fetch();
    const initial = ((await response.json()) as TunnelStatus[])[0];
    const configs = [service, { ...service, name: "文件服务", remote_port: 18444, target_host: "files.example.com" }];
    const statuses = configs.map((config, index): TunnelStatus => ({
      tunnel: { ...initial.tunnel, ...config, id: `fixture-service-${index}` },
      runtime: {
        ...initial.runtime,
        tunnel_id: `fixture-service-${index}`,
        state: managed ? "connected" : "port_conflict",
        managed,
        autossh_pid: managed ? 111 : null,
        ssh_pid: managed ? 222 : null,
        started_at: new Date().toISOString(),
        stable_since: managed ? new Date().toISOString() : null,
        remote_listener: { found: true, address: "127.0.0.1", port: config.remote_port, pids: [456], processes: [], raw: "" },
        checks: [{
          key: "remote_listener", label: `远端 ${config.remote_port} 监听`, state: managed ? "ok" : "warn",
          detail: managed ? "受管 SSH 运行中，远端监听已建立；PID 456" : "端口已被监听；PID 456",
          latency_ms: null,
        }],
      },
    }));
    await route.fulfill({ response, json: statuses });
  });
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: async (text: string) => window.sessionStorage.setItem("copied-command", text) },
    });
  });
}

test("generates loopback-only commands and keeps same-port services separate", () => {
  const commands = remoteCommands(service);
  const other = remoteCommands({ ...service, remote_port: 18444 });
  expect(commands.socat).toContain(`TCP4-LISTEN:443,bind=${commands.serviceHost},reuseaddr,fork`);
  expect(commands.socat).toContain("TCP4:127.0.0.1:18443");
  expect(commands.socat).not.toContain(service.target_host);
  expect(commands.serviceHost).not.toBe(other.serviceHost);
  expect(commands.hostsEntry).toBe(`${commands.serviceHost} api.example.com`);
  expect(commands.hostsEdit).toContain("sudo cp -p /etc/hosts");
  expect(commands.hostsEdit).toContain("sudoedit /etc/hosts");
  expect(commands.hostsEdit).not.toContain("tee -a");
  for (const command of [commands.socat!, commands.hostsEdit, commands.macLoopback, commands.portCheck]) {
    execFileSync("/bin/sh", ["-n", "-c", command]);
  }
  for (const port of [1, 255, 256, 65535]) {
    const octets = suggestedLoopbackHost(port).split(".").map(Number);
    expect(octets[0]).toBe(127);
    expect(octets.every((octet) => octet >= 0 && octet <= 255)).toBe(true);
  }
});

test("uses reachable destinations for wildcard listeners and avoids self-conflicts", () => {
  expect(remoteCommands({ ...service, remote_bind_host: "0.0.0.0" }).socat).toContain("TCP4:127.0.0.1:18443");
  expect(remoteCommands({ ...service, remote_bind_host: "localhost" }).socat).toContain("TCP4:127.0.0.1:18443");
  expect(remoteCommands({ ...service, remote_bind_host: "0.0.0.0", remote_port: 443 }).socat).toBeNull();
  expect(remoteCommands({ ...service, remote_bind_host: suggestedLoopbackHost(443), remote_port: 443 }).socat).toBeNull();
});

test("does not recommend hosts entries for IP addresses or localhost", () => {
  for (const target of ["192.0.2.10", "localhost", "LOCALHOST.", "app.localhost", "localhost.localdomain"]) {
    const commands = remoteCommands({ ...service, target_host: target });
    expect(commands.hostsEntry).toBeNull();
    expect(commands.hostsNotice).toBeTruthy();
  }
});

test("quotes command arguments and copies protocol-neutral connection parameters", () => {
  const value = "example'$(printf unsafe)";
  expect(execFileSync("/bin/sh", ["-c", `printf '%s' ${shellQuote(value)}`], { encoding: "utf8" })).toBe(value);
  expect(sshCommand(service)).toBe("ssh 'tunnel-host'");
  expect(sshCommand({ ...service, ssh_port: 2222, identity_file: "/example/key with spaces" }))
    .toBe("ssh -p 2222 -i '/example/key with spaces' 'tunnel-host'");
  const copied = connectionText(service);
  expect(copied).toContain("目标服务：api.example.com:443");
  expect(copied).not.toMatch(/PostgreSQL|DBeaver|数据库/);
});

test("shows and copies commands for the selected tunnel without executing them", async ({ page }) => {
  let mutations = 0;
  page.on("request", (request) => {
    if (["POST", "PUT", "PATCH", "DELETE"].includes(request.method())) mutations += 1;
  });
  await mockServices(page);
  await page.goto("/");
  await expect(page.getByText("目标服务", { exact: true })).toBeVisible();
  await expect(page.getByText("目标数据库", { exact: true })).toHaveCount(0);
  const panel = page.getByRole("region", { name: "远端服务器操作" });
  await expect(panel.getByRole("heading", { name: "1. socat：提供原服务端口" })).toBeVisible();
  await expect(panel.getByRole("heading", { name: "2. /etc/hosts：保留目标域名" })).toBeVisible();
  await panel.getByRole("button", { name: "复制socat 命令", exact: true }).click();
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem("copied-command"))).toBe(remoteCommands(service).socat);
  await panel.getByRole("button", { name: "复制hosts 映射", exact: true }).click();
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem("copied-command"))).toBe(remoteCommands(service).hostsEntry);
  await page.getByRole("button", { name: /文件服务/ }).click();
  const other = remoteCommands({ ...service, remote_port: 18444, target_host: "files.example.com" });
  await panel.getByRole("button", { name: "复制socat 命令", exact: true }).click();
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem("copied-command"))).toBe(other.socat);
  await panel.getByRole("button", { name: "复制hosts 映射", exact: true }).click();
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem("copied-command"))).toBe(other.hostsEntry);
  expect(mutations).toBe(0);
});

test("renders a green managed listener and retains warnings for unmanaged listeners", async ({ page }) => {
  await mockServices(page);
  await page.goto("/");
  const listener = page.getByTestId("check-remote_listener");
  await expect(listener).toHaveAttribute("data-state", "ok");
  await expect(listener.locator("svg")).toHaveCSS("color", "rgb(47, 128, 80)");
  await expect(page.getByText("受管 / 已建立")).toBeVisible();
  await page.unroute("**/api/tunnels");
  await mockServices(page, false);
  await page.reload();
  await expect(listener).toHaveAttribute("data-state", "warn");
  await expect(listener.locator("svg")).toHaveCSS("color", "rgb(165, 119, 32)");
  await expect(page.getByText("已监听 / 待确认")).toBeVisible();
});
