// Offline browser smoke: no provider calls and no real system clipboard writes.
import fs from 'node:fs/promises';
import fsSync from 'node:fs';
import path from 'node:path';
import {pathToFileURL, fileURLToPath} from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const [reviewArg, outputArg] = process.argv.slice(2);
if (!reviewArg || !outputArg) throw new Error('Usage: review.html private-output-directory');
const reviewPath = path.resolve(root, reviewArg);
const output = path.resolve(root, outputArg);
for (const target of [reviewPath, output]) {
  const rel = path.relative(path.join(root, 'data/private-research'), target);
  if (rel.startsWith('..') || path.isAbsolute(rel)) throw new Error('Private artifact path required');
}
const modules = process.env.CODEX_BUNDLED_NODE_MODULES || 'C:/Users/user/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules';
const {chromium} = await import(pathToFileURL(path.join(modules, 'playwright/index.mjs')).href);
const executablePath = [
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
].find(p => fsSync.existsSync(p));
if (!executablePath) throw new Error('Existing Chromium browser required; no installation attempted');
const browser = await chromium.launch({executablePath, headless: true, args: ['--allow-file-access-from-files']});
const errors = [];
const externalRequests = [];
const checks = [];
const check = (name, pass) => { checks.push({name, pass: Boolean(pass)}); };
try {
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => {if (m.type() === 'error') errors.push(m.text());});
  await page.route(/^https?:/, route => {externalRequests.push(route.request().url()); return route.abort();});
  await page.addInitScript(() => {
    window.__copiedPrompt = null;
    Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {
      writeText: async text => {window.__copiedPrompt = text;},
    }});
  });
  await page.goto(pathToFileURL(reviewPath).href, {waitUntil: 'load'});
  const options = await page.locator('#saved-query option').count();
  check('eleven_saved_queries', options === 11);
  for (let i = 0; i < options; i++) {
    await page.selectOption('#saved-query', String(i));
    const panel = page.locator('[data-query-panel]:visible');
    check(`query_${i}_one_panel`, await panel.count() === 1);
    const cards = panel.locator('.result-card');
    const reps = await cards.evaluateAll(nodes => nodes.map(n => n.dataset.representativeId));
    check(`query_${i}_five_distinct_representatives`, reps.length === 5 && new Set(reps).size === 5);
    check(`query_${i}_members_initially_collapsed`, await panel.locator('.group-members[open]').count() === 0);
    const images = panel.locator('.result-card > figure img');
    for (let j = 0; j < await images.count(); j++) {
      await images.nth(j).scrollIntoViewIfNeeded();
      await images.nth(j).evaluate(img => img.decode());
    }
    check(`query_${i}_representative_images_load`, await images.evaluateAll(imgs => imgs.length === 5 && imgs.every(img => img.naturalWidth > 0)));
  }
  await page.selectOption('#saved-query', '0');
  const firstPanel = page.locator('[data-query-panel]:visible');
  const prompt = firstPanel.locator('.result-card > .prompt-details').first();
  await prompt.locator('summary').click();
  const original = await prompt.locator('textarea').inputValue();
  await prompt.locator('button[data-copy]').click();
  check('copy_handler_preserves_original_without_system_clipboard', Boolean(original) && await page.evaluate(() => window.__copiedPrompt) === original);
  await prompt.locator('summary').click();
  const group = firstPanel.locator('.group-members').first();
  check('first_query_has_expandable_group', await group.count() === 1);
  if (await group.count()) {
    await group.locator('summary').first().click();
    check('group_expand_shows_multiple_members', await group.locator('.member:visible').count() >= 2);
    await group.locator('summary').first().click();
  }
  await fs.mkdir(output, {recursive: true});
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({path: path.join(output, 'desktop.png'), fullPage: false});
  await page.setViewportSize({width: 390, height: 844});
  await page.evaluate(() => window.scrollTo(0, 0));
  check('mobile_no_horizontal_overflow', await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.screenshot({path: path.join(output, 'mobile.png'), fullPage: false});
  check('no_browser_errors', errors.length === 0);
  check('no_external_requests', externalRequests.length === 0);
} finally {
  await browser.close();
}
const result = {schema_version: 'private-text-search-browser-smoke-1', status: checks.every(c => c.pass) ? 'passed' : 'failed',
  review: path.relative(root, reviewPath).replaceAll('\\', '/'), checks, errors, external_requests: externalRequests,
  real_clipboard_written: false, api_calls: 0};
await fs.writeFile(path.join(output, 'browser-smoke.json'), JSON.stringify(result, null, 2) + '\n', {flag: 'wx'});
process.stdout.write(JSON.stringify(result, null, 2) + '\n');
if (result.status !== 'passed') process.exitCode = 1;
