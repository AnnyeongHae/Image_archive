import fs from "node:fs/promises";
import fsSync from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const qaDir = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(qaDir, "..");
const benchmarkPath = path.join(root, "data", "private-research", "media-benchmarks", "modern-current", "modern_format_benchmark.json");
const htmlPath = path.join(root, "data", "private-research", "media-benchmarks", "modern-current", "browser-smoke.html");
const reportPath = path.join(qaDir, "modern_format_browser_smoke.json");
const screenshotPath = path.join(qaDir, "modern_format_browser_smoke.png");
const bundledNodeModules = process.env.CODEX_BUNDLED_NODE_MODULES
  || "C:\\Users\\user\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules";
const playwrightUrl = pathToFileURL(path.join(bundledNodeModules, "playwright", "index.mjs")).href;
const { chromium } = await import(playwrightUrl);

const benchmark = JSON.parse(await fs.readFile(benchmarkPath, "utf8"));
const cards = benchmark.records.flatMap((record) => record.variants.map((variant) => ({
  styleId: record.reference_style_id,
  variant: variant.variant,
  bytes: variant.bytes,
  src: pathToFileURL(path.join(root, variant.sample_path)).href,
})));
const escaped = (value) => String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;");
const html = `<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Modern format browser smoke</title>
<style>
body{margin:24px;background:#f2f3f5;color:#15171a;font:14px system-ui,sans-serif}h1{font-size:24px}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}.card{background:#fff;border:1px solid #d8dce2;border-radius:12px;padding:12px}
.frame{height:260px;display:grid;place-items:center;background:repeating-conic-gradient(#eef0f3 0 25%,#fff 0 50%) 0/24px 24px;border-radius:8px;overflow:hidden}
img{width:100%;height:100%;object-fit:contain}.meta{display:flex;justify-content:space-between;gap:8px;margin-top:10px;font-weight:650}
</style><h1>AVIF · WebP · JPEG browser decode canary</h1><div class="grid">
${cards.map((card) => `<article class="card"><div class="frame"><img src="${escaped(card.src)}" alt="${escaped(`${card.styleId} ${card.variant}`)}"></div><div class="meta"><span>${escaped(card.styleId)}</span><span>${escaped(card.variant)}</span><span>${Number(card.bytes).toLocaleString()} B</span></div></article>`).join("\n")}
</div></html>`;
await fs.writeFile(htmlPath, html, "utf8");

const browserCandidates = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
];
const executablePath = browserCandidates.find((candidate) => fsSync.existsSync(candidate));
if (!executablePath) throw new Error("No supported local Chromium executable was found.");
const browser = await chromium.launch({ executablePath, headless: true, args: ["--no-sandbox", "--disable-gpu", "--allow-file-access-from-files"] });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errors = [];
page.on("pageerror", (error) => errors.push(String(error)));
await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "load" });
await page.waitForFunction(() => Array.from(document.images).every((image) => image.complete));
const images = await page.locator("img").evaluateAll((nodes) => nodes.map((image) => ({
  src: image.currentSrc,
  natural_width: image.naturalWidth,
  natural_height: image.naturalHeight,
})));
await page.screenshot({ path: screenshotPath, fullPage: true });
await browser.close();
const failures = [];
if (images.length !== cards.length) failures.push(`expected ${cards.length} images, got ${images.length}`);
if (images.some((image) => image.natural_width < 1 || image.natural_height < 1)) failures.push("one or more formats failed browser decode");
if (errors.length) failures.push(...errors);
const report = {
  schema_version: "modern-format-browser-smoke-1.0",
  generated_at: new Date().toISOString(),
  browser_executable: executablePath,
  requested: cards.length,
  decoded: images.filter((image) => image.natural_width > 0 && image.natural_height > 0).length,
  formats: [...new Set(cards.map((card) => card.variant))],
  failures,
  images,
};
await fs.writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
console.log(JSON.stringify({ report: reportPath, screenshot: screenshotPath, requested: report.requested, decoded: report.decoded, failures }, null, 2));
if (failures.length) process.exitCode = 1;
