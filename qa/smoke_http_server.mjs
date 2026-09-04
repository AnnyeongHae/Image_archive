import fsSync from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const qaDir = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(qaDir, "..");
const archive = path.join(root, "legacy", "current_archive");
const baseUrl = String(process.env.IMAGE_ARCHIVE_HTTP_BASE_URL || "http://127.0.0.1:8765").replace(/\/$/, "");

function loadJson(name) {
  return JSON.parse(fsSync.readFileSync(path.join(archive, name), "utf8"));
}

function listFrom(payload, keys) {
  if (Array.isArray(payload)) return payload;
  for (const key of keys) {
    if (Array.isArray(payload?.[key])) return payload[key];
  }
  return [];
}

const legacyRecords = listFrom(loadJson("gpt_image2_cases.json"), ["records", "items", "cases"]);
const externalRecords = listFrom(loadJson("external_prompt_records.json"), ["records", "items"]);
const manualRecords = listFrom(loadJson("manual_prompt_records.json"), ["records", "items"]);
const socialRecords = listFrom(loadJson("social_prompt_records.json"), ["records", "items"]);
const secretRecords = listFrom(loadJson("secret_code_records.json"), ["records", "items"]);
const bulTemplates = listFrom(loadJson("bul001_template_collection.json"), ["templates", "items"]);
const opennanaArchive = JSON.parse(fsSync.readFileSync(
  path.join(root, "data", "private-research", "opennana", "archive", "opennana_records.json"),
  "utf8",
));
const opennanaRecords = listFrom(opennanaArchive, ["records", "items"]);
const manualDashboardRecords = manualRecords.filter((record) => (
  String(record?.source_id || "") !== "secret-code-notion84"
  && String(record?.raw_metadata?.archive_group || "") !== "secret_codes"
));
const expectedRecordCount = legacyRecords.length
  + externalRecords.length
  + manualDashboardRecords.length
  + socialRecords.length
  + secretRecords.length
  + bulTemplates.length
  + opennanaRecords.length;

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

const browser = await chromium.launch({ executablePath, headless: true, args: ["--no-sandbox"] });
const pageErrors = [];
const consoleErrors = [];
const failedRequests = [];
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
page.on("pageerror", (error) => pageErrors.push(String(error)));
page.on("console", (message) => {
  if (message.type() === "error") consoleErrors.push(message.text());
});
page.on("requestfailed", (request) => failedRequests.push(`${request.url()} :: ${request.failure()?.errorText || "failed"}`));

await page.goto(`${baseUrl}/app/index.html`, { waitUntil: "load", timeout: 120000 });
await page.waitForSelector("#cardGrid .archive-card", { timeout: 120000 });
const appCardCount = await page.locator("#cardGrid .archive-card").count();
const archiveLink = await page.locator('a[href="../legacy/current_archive/index.html"]').getAttribute("href");

await page.goto(`${baseUrl}/legacy/current_archive/index.html`, { waitUntil: "load", timeout: 120000 });
await page.waitForSelector("#grid .reference-card", { timeout: 120000 });
await page.waitForFunction(
  (expected) => document.querySelector("#resultHeadline")?.textContent?.includes(String(expected)),
  expectedRecordCount,
  { timeout: 120000 },
);
const initialCardCount = await page.locator("#grid .reference-card").count();
const headline = (await page.locator("#resultHeadline").innerText()).trim();

const testedStyleIds = ["CASE-431", "TOOL-1120", "BUL-125", "VNT-001", "SOC-000001", "SOC-000002", "SCD-131"];
const searchResults = [];
for (const styleId of testedStyleIds) {
  await page.locator("#search").fill(styleId);
  await page.waitForFunction(
    (expected) => document.querySelector("#grid")?.textContent?.includes(expected),
    styleId,
    { timeout: 30000 },
  );
  const firstCard = page.locator("#grid .reference-card").first();
  const badgeStackText = (await firstCard.locator(".badge-stack").innerText()).trim();
  searchResults.push({
    style_id: styleId,
    card_count: await page.locator("#grid .reference-card").count(),
    visible: (await page.locator("#grid").innerText()).includes(styleId),
    source_badge: (await firstCard.locator(".source-origin").innerText()).trim(),
    license_badge: (await firstCard.locator(".license-observed, .license-review").innerText()).trim(),
    overlay_badges: badgeStackText.split(/\r?\n/).filter(Boolean),
  });
}

const duplicateSummaryResponse = await fetch(`${baseUrl}/api/duplicates/v1/summary`);
const duplicateSummary = duplicateSummaryResponse.ok ? await duplicateSummaryResponse.json() : null;
const duplicateGroupsResponse = await fetch(`${baseUrl}/api/duplicates/v1/groups?limit=3&offset=0&sort=members_desc`);
const duplicateGroups = duplicateGroupsResponse.ok ? await duplicateGroupsResponse.json() : null;

await page.goto(`${baseUrl}/legacy/current_archive/duplicate-review.html`, { waitUntil: "load", timeout: 120000 });
await page.waitForSelector("#groupList .group-row", { timeout: 120000 });
await page.waitForFunction(() => document.querySelector("#detailContent")?.hidden === false, { timeout: 120000 });
const duplicateUi = {
  api_badge: (await page.locator("#apiBadge").innerText()).trim(),
  summary_cards: await page.locator("#summaryGrid .summary-card").count(),
  visible_groups: await page.locator("#groupList .group-row").count(),
  detail_title: (await page.locator("#detailTitle").innerText()).trim(),
  comparison_cards: await page.locator("#comparisonGrid .comparison-card").count(),
};
const duplicateLiveScreenshot = path.join(qaDir, "duplicate_review_live.png");
await page.screenshot({ path: duplicateLiveScreenshot, fullPage: true });

await browser.close();

const failures = [];
if (expectedRecordCount !== 18_815) failures.push(`expected record calculation drifted: ${expectedRecordCount}`);
if (appCardCount !== 5) failures.push(`featured app cards: ${appCardCount}`);
if (archiveLink !== "../legacy/current_archive/index.html") failures.push(`archive link: ${archiveLink}`);
if (headline !== `${expectedRecordCount}개 스타일 일치`) failures.push(`headline: ${headline}`);
if (initialCardCount !== 50) failures.push(`initial full-archive cards: ${initialCardCount}`);
if (!duplicateSummaryResponse.ok || !duplicateSummary) failures.push(`duplicate summary API: ${duplicateSummaryResponse.status}`);
if (!duplicateGroupsResponse.ok || !duplicateGroups) failures.push(`duplicate groups API: ${duplicateGroupsResponse.status}`);
if (duplicateSummary?.canonical?.record_count !== expectedRecordCount) failures.push(`duplicate canonical count: ${duplicateSummary?.canonical?.record_count}`);
if (duplicateSummary?.counts?.groups_total !== 772) failures.push(`duplicate group count: ${duplicateSummary?.counts?.groups_total}`);
if (duplicateGroups?.groups?.length !== 3) failures.push(`duplicate API page size: ${duplicateGroups?.groups?.length}`);
if (duplicateUi.api_badge !== "API 연결됨") failures.push(`duplicate API badge: ${duplicateUi.api_badge}`);
if (duplicateUi.summary_cards !== 4) failures.push(`duplicate summary cards: ${duplicateUi.summary_cards}`);
if (duplicateUi.visible_groups !== 20) failures.push(`duplicate visible groups: ${duplicateUi.visible_groups}`);
if (!duplicateUi.detail_title) failures.push("duplicate detail title missing");
if (duplicateUi.comparison_cards < 2) failures.push(`duplicate comparison cards: ${duplicateUi.comparison_cards}`);
for (const result of searchResults) {
  if (!result.visible || result.card_count < 1) failures.push(`${result.style_id}: not found`);
  if (!result.source_badge.startsWith("출처 · ")) failures.push(`${result.style_id}: source badge missing`);
  if (!result.license_badge) failures.push(`${result.style_id}: license badge missing`);
  if (/Candidate|A direct/.test(result.overlay_badges.join(" "))) failures.push(`${result.style_id}: legacy candidate badge remains`);
}
if (pageErrors.length) failures.push(...pageErrors.map((value) => `pageerror: ${value}`));
if (consoleErrors.length) failures.push(...consoleErrors.map((value) => `console: ${value}`));
if (failedRequests.length) failures.push(...failedRequests.map((value) => `request: ${value}`));

const report = {
  schema_version: "1.0.0",
  status: failures.length ? "failed" : "passed",
  base_url: baseUrl,
  expected_record_count: expectedRecordCount,
  record_components: {
    legacy: legacyRecords.length,
    external: externalRecords.length,
    bul001: bulTemplates.length,
    manual: manualDashboardRecords.length,
    social: socialRecords.length,
    secret_codes: secretRecords.length,
    opennana: opennanaRecords.length,
  },
  featured_app: { card_count: appCardCount, archive_link: archiveLink },
  full_archive: { headline, initial_card_count: initialCardCount, search_results: searchResults },
  duplicate_review: {
    summary_status: duplicateSummaryResponse.status,
    groups_status: duplicateGroupsResponse.status,
    canonical_record_count: duplicateSummary?.canonical?.record_count ?? null,
    groups_total: duplicateSummary?.counts?.groups_total ?? null,
    api_page_size: duplicateGroups?.groups?.length ?? null,
    ui: duplicateUi,
    screenshot: path.relative(root, duplicateLiveScreenshot).replaceAll("\\", "/"),
  },
  page_errors: pageErrors,
  console_errors: consoleErrors,
  failed_requests: failedRequests,
  failures,
};
fsSync.writeFileSync(path.join(qaDir, "http_server_smoke.json"), JSON.stringify(report, null, 2) + "\n", "utf8");
console.log(JSON.stringify(report, null, 2));
if (failures.length) process.exitCode = 1;
