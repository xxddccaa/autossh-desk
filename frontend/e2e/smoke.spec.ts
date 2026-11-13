import { expect, test } from "@playwright/test";
import type { TunnelStatus } from "../src/lib/api";

test("renders the tunnel manager and opens configuration", async ({ page }) => {
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await page.goto("/");
  await expect(page.getByText("AutoSSH Desk", { exact: true })).toBeVisible();
  await expect(page.locator("h1").first()).toBeVisible();
  await expect(page.getByText("示例隧道").first()).toBeVisible();
  await expect(page.getByText("完整链路检查")).toBeVisible();
  await expect(page.getByText("实时日志")).toBeVisible();

  await page.getByRole("button", { name: "设置" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("编辑隧道")).toBeVisible();
  await expect(dialog.locator("input").nth(1)).toHaveValue("root@remote.example.com");
  await expect(dialog.locator("input").nth(5)).toHaveValue("15432");
  await dialog.getByRole("button", { name: "关闭" }).click();
  await expect(dialog).toBeHidden();

  expect(pageErrors).toEqual([]);
});

test("shows automatic retry while a remote port is occupied", async ({ page }) => {
  await page.routeWebSocket("**/api/ws", (socket) => socket.close());
  await page.route("**/api/tunnels", async (route) => {
    const response = await route.fetch();
    const statuses = (await response.json()) as TunnelStatus[];
    statuses[0].runtime = {
      ...statuses[0].runtime,
      state: "port_conflict",
      external_tunnel: false,
      last_error: "远端端口被非受管会话占用，TCP 可达不能确认目标",
      next_retry_at: new Date(Date.now() + 60_000).toISOString(),
      activity: "等待远端端口释放，将自动重试",
    };
    await route.fulfill({ response, json: statuses });
  });

  await page.goto("/");
  await expect(page.locator("h1")).toHaveText("远端端口冲突");
  await expect(page.getByText(/自动保持连接 · 下次重试/)).toBeVisible();
  await expect(page.getByText(/无需手动启动/)).toBeVisible();
  await expect(page.getByText("非受管监听", { exact: true })).toBeHidden();
});

test("requires explicit confirmation before cleaning a TCP-reachable listener", async ({ page }) => {
  let cleanCalls = 0;
  await page.route("**/conflict/preview", (route) => route.fulfill({
    json: {
      tunnel_id: "fixture-tunnel",
      fingerprint: "fixture-confirmation",
      can_clean: true,
      end_to_end_ok: true,
      remote_listener: {
        found: true, address: "127.0.0.1", port: 15432, pids: [456],
        processes: [{ pid: 456, ppid: 1, user: "tester", comm: "sshd", args: "sshd", is_sshd: true }],
        raw: "LISTEN 127.0.0.1:15432",
      },
      message: "TCP 可达不代表转发目标正确，清理将中断该会话的所有转发。",
      manual_command: null,
      expires_at: new Date(Date.now() + 120_000).toISOString(),
    },
  }));
  await page.route("**/conflict/clean", async (route) => {
    expect(route.request().postDataJSON()).toMatchObject({ confirm: true });
    cleanCalls += 1;
    await route.fulfill({ json: { message: "测试清理完成" } });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "清理端口" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("可达，目标未验证", { exact: true })).toBeVisible();
  const confirmButton = dialog.getByRole("button", { name: "核对完成，发送 SIGTERM" });
  await expect(confirmButton).toBeDisabled();
  await dialog.getByRole("checkbox", { name: "我已核对用途，确认终止该 sshd 会话的全部转发" }).check();
  await expect(confirmButton).toBeEnabled();
  await confirmButton.click();
  await expect(dialog).toBeHidden();
  expect(cleanCalls).toBe(1);
});

test("shows the remote port safety dialog", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "清理端口" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("检查远端端口占用")).toBeVisible();
  await expect(dialog.getByText("只处理当前隧道、当前监听端口的单一 sshd 会话。")).toBeVisible();
  await dialog.getByRole("button", { name: "取消" }).click();
});

