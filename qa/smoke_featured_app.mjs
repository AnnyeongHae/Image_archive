import fs from "node:fs/promises";
import fsSync from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const qaDir = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(qaDir, "..");
const reportPath = path.join(qaDir, "featured_ui_smoke.json");
const desktopPath = path.join(qaDir, "featured_desktop.png");
const mobilePath = path.join(qaDir, "featured_mobile.png");
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

const browser = await chromium.launch({
  executablePath,
  headless: true,
  args: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--allow-file-access-from-files"],
});

async function inspect(url, viewport, screenshotPath, exerciseSelection) {
  const page = await browser.newPage({ viewport });
  const pageErrors = [];
  const consoleErrors = [];
  page.on("pageerror", (error) => pageErrors.push(String(error)));
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  await page.goto(url, { waitUntil: "load" });
  await page.waitForSelector(".archive-card");
  let persistedSelection = null;
  if (exerciseSelection) {
    await page.locator('[data-style-id="VNT-001"] [data-action="select"]').click();
    await page.reload({ waitUntil: "load" });
    await page.waitForSelector(".archive-card");
    persistedSelection = await page.locator('[data-style-id="VNT-001"] [data-action="select"]').getAttribute("aria-pressed");
    await page.locator('[data-style-id="VNT-001"] summary').click();
  }
  const cards = page.locator(".archive-card");
  const cardCount = await cards.count();
  const styleIds = await page.locator(".style-id").allTextContents();
  for (let index = 0; index < cardCount; index += 1) {
    await cards.nth(index).scrollIntoViewIfNeeded();
    await page.waitForTimeout(250);
  }
  await page.waitForFunction(
    () => Array.from(document.querySelectorAll(".card-media")).every((image) => image.complete && image.naturalWidth > 0),
    { timeout: 4000 },
  ).catch(() => {});
  await page.waitForTimeout(900);
  const imageStates = await page.locator(".card-media").evaluateAll((images) => images.map((image) => ({
    src: image.getAttribute("src"),
    complete: image.complete,
    naturalWidth: image.naturalWidth,
    naturalHeight: image.naturalHeight,
    loading: image.getAttribute("loading"),
    decoding: image.getAttribute("decoding"),
    fetchpriority: image.getAttribute("fetchpriority"),
  })));
  await page.screenshot({ path: screenshotPath, fullPage: true });
  await page.close();
  return {
    card_count: cardCount,
    style_ids: styleIds,
    image_states: imageStates,
    persisted_selection: persistedSelection,
    page_errors: pageErrors,
    console_errors: consoleErrors,
  };
}

const appUrl = pathToFileURL(path.join(root, "app", "index.html")).href;
const distUrl = pathToFileURL(path.join(root, "dist", "index.html")).href;
const desktop = await inspect(appUrl, { width: 1440, height: 1000 }, desktopPath, true);
const mobile = await inspect(distUrl, { width: 390, height: 844 }, mobilePath, false);
await browser.close();

const expectedIds = ["TOOL-1120", "I2ADS-068", "VNT-001", "BST-001", "SOC-000002"];
const failures = [];
for (const [name, result] of [["desktop", desktop], ["mobile", mobile]]) {
  if (result.card_count !== 5) failures.push(`${name}: expected 5 cards, got ${result.card_count}`);
  if (JSON.stringify(result.style_ids) !== JSON.stringify(expectedIds)) failures.push(`${name}: Style ID order mismatch`);
  if (result.image_states.some((item) => !item.complete || item.naturalWidth < 1 || item.naturalHeight < 1)) {
    failures.push(`${name}: one or more images did not load`);
  }
  const [firstImage, ...remainingImages] = result.image_states;
  if (!firstImage || firstImage.loading !== "eager" || firstImage.fetchpriority !== "high") {
    failures.push(`${name}: first card must stay eager/high priority`);
  }
  if (remainingImages.some((item) => item.loading !== "lazy" || item.decoding !== "async")) {
    failures.push(`${name}: non-initial cards must stay lazy/async`);
  }
  if (result.page_errors.length) failures.push(`${name}: page errors ${result.page_errors.join(" | ")}`);
  if (result.console_errors.length) failures.push(`${name}: console errors ${result.console_errors.join(" | ")}`);
}
if (desktop.persisted_selection !== "true") failures.push("desktop: selection did not persist after reload");

const report = {
  schema_version: "1.0.0",
  status: failures.length ? "failed" : "passed",
  failures,
  desktop,
  mobile,
  screenshots: [
    path.relative(root, desktopPath).replaceAll("\\", "/"),
    path.relative(root, mobilePath).replaceAll("\\", "/"),
  ],
};
await fs.writeFile(reportPath, JSON.stringify(report, null, 2) + "\n", "utf8");
console.log(JSON.stringify({ status: report.status, failures, screenshots: report.screenshots }, null, 2));
if (failures.length) process.exitCode = 1;
