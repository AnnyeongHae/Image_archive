import fsSync from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const qaDir = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(qaDir, "..");
const repo = path.resolve(root, "..");
const bundledNodeModules = process.env.CODEX_BUNDLED_NODE_MODULES
  || "C:\\Users\\user\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules";
const playwrightUrl = pathToFileURL(path.join(bundledNodeModules, "playwright", "index.mjs")).href;
const { chromium } = await import(playwrightUrl);
const browserCandidates = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
];
const executablePath = browserCandidates.find((candidate) => fsSync.existsSync(candidate));
if (!executablePath) throw new Error("No supported local Chromium executable was found.");

const entries = {
  new_root: path.join(root, "legacy", "current_archive", "index.html"),
  compatibility_path: path.join(repo, "Reports", "2026-08-25-01_상세페이지_프롬프트전수조사", "index.html"),
};
const browser = await chromium.launch({ executablePath, headless: true, args: ["--no-sandbox", "--allow-file-access-from-files"] });
const results = {};
for (const [name, entry] of Object.entries(entries)) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(String(error)));
  await page.goto(pathToFileURL(entry).href, { waitUntil: "load", timeout: 120000 });
  await page.waitForSelector("#grid .reference-card", { timeout: 120000 });
  results[name] = {
    title: await page.title(),
    card_count_initial: await page.locator("#grid .reference-card").count(),
    headline: await page.locator("#resultHeadline").innerText(),
    page_errors: pageErrors,
  };
  await page.close();
}
await browser.close();

const failures = [];
for (const [name, result] of Object.entries(results)) {
  if (result.card_count_initial < 1) failures.push(`${name}: no cards rendered`);
  if (result.headline.includes("불러오는 중")) failures.push(`${name}: loading state did not complete`);
  if (result.page_errors.length) failures.push(`${name}: ${result.page_errors.join(" | ")}`);
}
if (results.new_root.headline !== results.compatibility_path.headline) failures.push("legacy entry headlines differ");
const report = { schema_version: "1.0.0", status: failures.length ? "failed" : "passed", failures, results };
fsSync.writeFileSync(path.join(qaDir, "legacy_entry_smoke.json"), JSON.stringify(report, null, 2) + "\n", "utf8");
console.log(JSON.stringify(report, null, 2));
if (failures.length) process.exitCode = 1;
