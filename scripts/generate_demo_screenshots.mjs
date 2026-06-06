#!/usr/bin/env node

import fs from "node:fs/promises";
import fsSync from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const outputDir = path.join(root, "docs", "assets");
const runtimeDir = await fs.mkdtemp(path.join(os.tmpdir(), "grocery-cockpit-demo-"));
const configPath = path.join(runtimeDir, "config.json");
const dbPath = path.join(runtimeDir, "data", "grocery.sqlite");
let baseUrl = "";

function resolvePythonCommand() {
  const configured = process.env.GROCERY_PYTHON;
  if (configured) return { command: configured, args: [] };
  if (process.platform === "win32") {
    return { command: "py", args: ["-3.13"] };
  }
  for (const command of ["python3.13", "python3", "python"]) {
    const result = spawnSync(command, ["--version"], { stdio: "ignore" });
    if (result.status === 0) return { command, args: [] };
  }
  throw new Error("Python 3 was not found. Set GROCERY_PYTHON to its executable.");
}

function resolveBrowserExecutablePath() {
  if (process.env.GROCERY_CHROME_PATH) return process.env.GROCERY_CHROME_PATH;
  const candidates = [
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
  ];
  return candidates.find(candidate => fsSync.existsSync(candidate));
}

async function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      server.close(() => resolve(address.port));
    });
  });
}

function runPython(args) {
  const python = resolvePythonCommand();
  const result = spawnSync(python.command, [...python.args, path.join(root, "grocery_cockpit.py"), ...args], {
    cwd: root,
    encoding: "utf8",
  });
  if (result.status !== 0) {
    throw new Error(result.stderr || result.stdout || `Python exited with status ${result.status}`);
  }
}

async function waitForServer(baseUrl, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${baseUrl}/api/state`);
      if (response.ok) return;
    } catch {
      // Server is still starting.
    }
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  throw new Error(`Demo server did not become ready within ${timeoutMs}ms.`);
}

async function seedBasket(baseUrl) {
  const state = await fetch(`${baseUrl}/api/state`).then(response => response.json());
  const itemIds = state.items.slice(0, 3).map(card => card.item.id);
  for (const itemId of itemIds) {
    const response = await fetch(`${baseUrl}/api/basket`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ item_id: itemId, quantity: 1 }),
    });
    if (!response.ok) throw new Error(`Could not add demo item ${itemId} to the basket.`);
  }
}

function pngDimensions(bytes) {
  const signature = bytes.subarray(0, 8).toString("hex");
  if (signature !== "89504e470d0a1a0a") throw new Error("Generated file is not a PNG.");
  return {
    width: bytes.readUInt32BE(16),
    height: bytes.readUInt32BE(20),
  };
}

async function verifyScreenshot(filePath, expectedWidth, expectedHeight) {
  const bytes = await fs.readFile(filePath);
  const dimensions = pngDimensions(bytes);
  if (dimensions.width !== expectedWidth || dimensions.height !== expectedHeight) {
    throw new Error(
      `${path.basename(filePath)} is ${dimensions.width}x${dimensions.height}; expected ${expectedWidth}x${expectedHeight}.`,
    );
  }
  if (bytes.length < 20000) {
    throw new Error(`${path.basename(filePath)} is unexpectedly small and may be blank.`);
  }
  return { ...dimensions, bytes: bytes.length };
}

async function capture(page, fileName, viewport, beforeCapture) {
  await page.setViewportSize(viewport);
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.locator("#items .item-card").first().waitFor({ state: "visible" });
  await page.addStyleTag({
    content: "*, *::before, *::after { animation: none !important; transition: none !important; }",
  });
  if (beforeCapture) await beforeCapture(page);
  const filePath = path.join(outputDir, fileName);
  await page.screenshot({ path: filePath, fullPage: false });
  return verifyScreenshot(filePath, viewport.width, viewport.height);
}

await fs.mkdir(path.dirname(dbPath), { recursive: true });
await fs.mkdir(outputDir, { recursive: true });
await fs.writeFile(
  configPath,
  `${JSON.stringify({
    location: { label: "Demo home", pincode: "", city: "" },
    settings: {
      refresh_interval_minutes: 60,
      min_10d_avg_drop_percent: 20,
      min_30d_avg_drop_percent: 25,
      min_history_points: 3,
      include_delivery_fees: true,
      basket_alert_min_saving: 50,
      single_app_convenience_threshold_rupees: 50,
      single_app_convenience_threshold_percent: 5,
      alert_expiry_hours: 2,
    },
    access: { enabled: false, key: "" },
  }, null, 2)}\n`,
  "utf8",
);

let browser;
let server;
try {
  runPython(["--config", configPath, "--db", dbPath, "seed"]);
  const port = await freePort();
  baseUrl = `http://127.0.0.1:${port}`;
  const python = resolvePythonCommand();
  server = spawn(
    python.command,
    [
      ...python.args,
      path.join(root, "grocery_cockpit.py"),
      "--config",
      configPath,
      "--db",
      dbPath,
      "serve",
      "--host",
      "127.0.0.1",
      "--port",
      String(port),
    ],
    { cwd: root, stdio: ["ignore", "pipe", "pipe"] },
  );
  await waitForServer(baseUrl);
  await seedBasket(baseUrl);
  const executablePath = resolveBrowserExecutablePath();
  if (!executablePath) {
    throw new Error("Chrome, Edge, or Chromium was not found. Set GROCERY_CHROME_PATH to its executable.");
  }
  browser = await chromium.launch({
    headless: true,
    executablePath,
  });
  const page = await browser.newPage();
  const results = {};
  results["demo-desktop.png"] = await capture(page, "demo-desktop.png", { width: 1440, height: 1000 });
  results["demo-mobile.png"] = await capture(page, "demo-mobile.png", { width: 390, height: 844 });
  results["demo-basket.png"] = await capture(
    page,
    "demo-basket.png",
    { width: 1280, height: 900 },
    async currentPage => {
      await currentPage.locator("#basketOpenBtn").click();
      await currentPage.locator("#basketDialog").waitFor({ state: "visible" });
    },
  );
  for (const [name, result] of Object.entries(results)) {
    console.log(`${name}: ${result.width}x${result.height}, ${result.bytes} bytes`);
  }
} finally {
  if (browser) await browser.close().catch(() => {});
  if (server && !server.killed) server.kill();
  await fs.rm(runtimeDir, { recursive: true, force: true });
}
