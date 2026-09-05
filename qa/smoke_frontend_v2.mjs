/** Browser acceptance for a loopback gallery. Reads local projection only; no provider calls. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import crypto from 'node:crypto';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
function option(name) { const index = args.indexOf(name); return index < 0 ? null : args[index + 1]; }
const base = new URL(option('--url') || 'http://127.0.0.1:8965/');
const publicMode = args.includes('--public');
const loopback = base.protocol === 'http:' && base.hostname === '127.0.0.1';
const publicOrigin = publicMode && base.origin === 'https://photoposting.shop';
if ((!loopback && !publicOrigin) || base.username || base.password || base.pathname !== '/' || base.search || base.hash) throw new Error('approved_gallery_origin_required');
const output = path.resolve(root, option('--output') || 'data/private-research/platform-v2/frontend-v2-qa/latest');
const privateBase = path.join(root, 'data', 'private-research') + path.sep;
if (!output.startsWith(privateBase)) throw new Error('private_evidence_path_required');
fs.mkdirSync(output, {recursive: true});
const runtime = process.env.CODEX_BUNDLED_NODE_MODULES || 'C:\\Users\\user\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules';
const {chromium} = await import(pathToFileURL(path.join(runtime, 'playwright', 'index.mjs')).href);
const executablePath = ['C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe', 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'].find(fs.existsSync);
if (!executablePath) throw new Error('installed_browser_required');
const checks = [], errors = [], external = [];
const browser = await chromium.launch({executablePath, headless: true});
const context = await browser.newContext({viewport: {width: 1440, height: 1000}, permissions: ['clipboard-read', 'clipboard-write']});
await context.route('**/*', route => {
  const url = new URL(route.request().url());
  if (url.origin !== base.origin) { external.push(url.origin); return route.abort(); }
  return route.continue();
});
const page = await context.newPage();
page.on('pageerror', error => errors.push(error.name));
await page.addInitScript(() => {
  const nativeWrite = navigator.clipboard.writeText.bind(navigator.clipboard);
  Object.defineProperty(navigator.clipboard, 'writeText', {value: async text => {
    window.__galleryCopyArgument = text;
    return nativeWrite(text);
  }, configurable: true});
});
const json = async relative => { const response = await fetch(new URL(relative, base)); assert.equal(response.status, 200); return response.json(); };
const check = (name, condition = true) => { assert.ok(condition, name); checks.push(name); };
const cards = page.locator('#gallery > article[data-group-id]');
async function waitCards(count) { await page.waitForFunction(expected => document.querySelectorAll('#gallery > article[data-group-id]').length === expected, count); }
async function reset() { await page.locator('#reset-filters').click(); await waitCards(Math.min(24, catalog.groups.length)); }
const normalize = value => String(value ?? '').normalize('NFKC').toLocaleLowerCase('ko').replace(/\s+/gu, ' ').trim();
const categoryIds = member => member.category_ids?.length ? member.category_ids : ['unclassified'];
function facetCounts(groups, key) {
  const counts = new Map();
  const seen = new Set();
  for (const group of groups) {
    if (seen.has(group.id)) continue;
    seen.add(group.id);
    const labels = new Set(group.members.flatMap(member => member[key] || []).map(normalize).filter(Boolean));
    for (const label of labels) counts.set(label, (counts.get(label) || 0) + 1);
  }
  return counts;
}
async function facetOptions(target) {
  const result = {};
  for (const key of ['usage', 'style', 'background']) {
    result[key] = await target.locator(`#${key}-filter option`).evaluateAll(nodes => nodes.filter(node => node.value).map(node => ({value: node.value, label: node.textContent})));
  }
  return result;
}
async function chooseCategory(target, id) {
  const buttons = target.locator('#category-filters button[data-category-id]');
  const index = await buttons.evaluateAll((nodes, value) => nodes.findIndex(node => node.dataset.categoryId === value), id);
  assert.ok(index >= 0, `category chip exists: ${id}`);
  await buttons.nth(index).click();
}
async function waitGroupIds(target, ids) {
  await target.waitForFunction(expected => JSON.stringify([...document.querySelectorAll('#gallery > article[data-group-id]')].map(node => node.dataset.groupId)) === JSON.stringify(expected), ids);
}
async function waitDetailImage(target) {
  await target.locator('#detail-content .detail-main-image img').waitFor({state: 'visible'});
  await target.waitForFunction(() => {
    const image = document.querySelector('#detail-content .detail-main-image img');
    return image?.complete && image.naturalWidth > 0 && image.naturalHeight > 0;
  });
}
let catalog, first, multi, firstDetail;
try {
  catalog = await json('data/catalog.json');
  assert.ok(catalog.groups.length > 24, 'real populated preview required');
  first = catalog.groups[0];
  multi = catalog.groups.find(group => group.members.length > 1);
  assert.ok(multi, 'confirmed multi-member group required');
  firstDetail = await json(first.detail_path);
  const initialDetailRequests = [];
  page.on('request', request => { if (new URL(request.url()).pathname.startsWith('/data/groups/')) initialDetailRequests.push(request.url()); });
  await page.goto(base.href, {waitUntil: 'networkidle'});
  await waitCards(24);
  check('initial_24_representative_cards', await cards.count() === 24);
  check(publicMode ? 'public_mode_hides_private_preview_label' : 'private_preview_label', publicMode
    ? catalog.mode === 'public' && !(await page.locator('#preview-notice').isVisible())
    : await page.locator('#preview-notice').isVisible());
  check('no_initial_detail_shards', initialDetailRequests.length === 0);
  check('catalog_excludes_full_prompts', !JSON.stringify(catalog).includes('original_prompt'));
  const ids = await cards.evaluateAll(nodes => nodes.map(node => node.dataset.groupId));
  first = catalog.groups.find(group => group.id === ids[0]) || first;
  firstDetail = await json(first.detail_path);
  check('unique_top_level_groups', new Set(ids).size === ids.length);
  check('representative_stable', (await cards.evaluateAll(nodes => nodes.every(node => Boolean(node.dataset.itemId)))));
  check('copy_button_visible_without_details', await cards.first().locator('[data-action="copy"]').isVisible());
  check('variants_initially_closed', await page.locator('details.group-variants[open]').count() === 0);
  const categoryButtons = await page.locator('#category-filters button[data-category-id]').evaluateAll(nodes => nodes.map(node => ({id: node.dataset.categoryId, pressed: node.getAttribute('aria-pressed'), categoryClass: node.classList.contains('category-filter')})));
  check('category_navigation_present', categoryButtons.length > 1 && categoryButtons.every(node => node.categoryClass));
  check('category_navigation_unique_and_all_selected', new Set(categoryButtons.map(node => node.id)).size === categoryButtons.length && categoryButtons.filter(node => node.pressed === 'true').length === 1 && categoryButtons.find(node => node.id === '')?.pressed === 'true');
  check('facet_threshold_note_visible', await page.locator('#facet-note').isVisible() && (await page.locator('#facet-note').innerText()).includes('2개 이상'));
  const initialFacets = await facetOptions(page);
  for (const key of ['usage', 'style', 'background']) {
    const options = initialFacets[key];
    const counts = facetCounts(catalog.groups, key);
    const expected = [...counts].filter(([, count]) => count >= 2);
    check(`${key}_facet_normalized_unique`, new Set(options.map(option => normalize(option.value))).size === options.length && options.every(option => option.value === option.value.normalize('NFKC').replace(/\s+/gu, ' ').trim()));
    check(`${key}_facet_distinct_group_threshold_and_counts`, options.length === expected.length && options.every(option => counts.get(normalize(option.value)) >= 2 && option.label.endsWith(`(${counts.get(normalize(option.value)).toLocaleString('ko-KR')}그룹)`)));
    check(`${key}_facet_note_accessible`, await page.locator(`#${key}-filter`).getAttribute('aria-describedby') === 'facet-note');
  }
  await cards.first().locator('img').waitFor({state:'visible'});
  await page.waitForFunction(() => [...document.querySelectorAll('#gallery > article img')].filter(img => img.getBoundingClientRect().top < innerHeight).every(img => img.complete && img.naturalWidth > 0));
  check('visible_images_decoded');
  check('desktop_images_above_600px', await cards.first().evaluate(node => node.getBoundingClientRect().top < 600));
  await page.screenshot({path: path.join(output, 'desktop.png')});
  const original = firstDetail.members.find(member => member.id === first.representative_id).original_prompt;
  await cards.first().locator('[data-action="copy"]').click();
  await page.waitForFunction(() => document.querySelector('#toast')?.textContent?.includes('복사'));
  const clipboard = await page.evaluate(() => navigator.clipboard.readText());
  check('clipboard_write_argument_exact_original_prompt', await page.evaluate(() => window.__galleryCopyArgument) === original);
  // Windows CF_UNICODETEXT can normalize LF to CRLF. Do not change the source
  // prompt or claim byte-identical OS storage; verify the browser input exactly.
  check('clipboard_readback_content_preserved', clipboard.replace(/\r\n/g, '\n') === original.replace(/\r\n/g, '\n'));
  await cards.first().locator('[data-action="details"]').last().click();
  await page.locator('#detail-dialog').waitFor({state: 'visible'});
  await page.locator('#prompt-text').waitFor();
  check('detail_exact_original_prompt', await page.locator('#prompt-text').textContent() === original);
  check('detail_has_rights', (await page.locator('#detail-content').innerText()).includes('권리'));
  const shownTitle = await page.locator('#detail-title').textContent();
  check('detail_title_display_only_without_raw_urls', Boolean(shownTitle?.trim()) && !/https?:\/\//u.test(shownTitle) && !/\]\(/u.test(shownTitle));
  check('detail_title_matches_card_display', shownTitle === await cards.first().locator('.card-title').textContent());
  await waitDetailImage(page);
  check('detail_main_image_decoded_before_capture');
  await page.screenshot({path: path.join(output, 'detail.png')});
  await page.keyboard.press('Escape');
  check('escape_closes_dialog', !(await page.locator('#detail-dialog').isVisible()));
  check('dialog_restores_focus', await page.evaluate(() => document.activeElement?.matches('[data-action="details"]')));

  const member = multi.members.find(value => value.id !== multi.representative_id);
  await page.locator('#search').fill(member.style_id || member.id);
  await page.locator('#search-form').evaluate(form => form.requestSubmit());
  await page.waitForFunction(id => [...document.querySelectorAll('#gallery > article')].some(node => node.dataset.groupId === id), multi.id);
  // Avoid selector escaping opaque IDs: select the matching article by index from DOM data.
  const groupIndex = await cards.evaluateAll((nodes, id) => nodes.findIndex(node => node.dataset.groupId === id), multi.id);
  assert.ok(groupIndex >= 0);
  const multiCard = cards.nth(groupIndex);
  check('member_search_returns_representative', await multiCard.getAttribute('data-item-id') === multi.representative_id);
  check('facet_options_stable_after_search', JSON.stringify(await facetOptions(page)) === JSON.stringify(initialFacets));
  await multiCard.locator('.group-label-button').click();
  await page.locator('#detail-dialog').waitFor({state: 'visible'});
  const variantButtons = page.locator('#detail-content [data-action="open-member"]');
  check('variants_expand', await variantButtons.count() >= 0);
  await page.keyboard.press('Escape');
  await reset();

  for (const key of ['usage', 'style', 'background']) {
    const select = page.locator(`#${key}-filter`);
    const values = await select.locator('option').evaluateAll(nodes => nodes.map(node => node.value).filter(Boolean));
    assert.ok(values.length, `${key} facets present`);
    await select.selectOption(values[0]);
    const expectedGroups = catalog.groups.filter(group => group.members.some(value => (value[key] || []).some(label => normalize(label) === normalize(values[0]))));
    await waitCards(Math.min(24, expectedGroups.length));
    check(`${key}_filter`, await cards.count() === Math.min(24, expectedGroups.length));
    await reset();
  }
  const populatedCategory = categoryButtons.find(node => node.id);
  await chooseCategory(page, populatedCategory.id);
  const categoryGroups = catalog.groups.filter(group => group.members.some(member => categoryIds(member).includes(populatedCategory.id)));
  const categoryExpected = categoryGroups.slice(0, Math.min(24, categoryGroups.length));
  await waitGroupIds(page, categoryExpected.map(group => group.id));
  check('category_chip_filters_groups_and_preserves_representatives', await cards.evaluateAll((nodes, expected) => nodes.every((node, index) => node.dataset.itemId === expected[index]), categoryExpected.map(group => group.representative_id)));
  check('facet_options_stable_after_category', JSON.stringify(await facetOptions(page)) === JSON.stringify(initialFacets));
  await reset();
  check('reset_all_categories_facets_and_query', await page.locator('#search').inputValue() === '' && await page.locator('#sort').inputValue() === 'datetime' && await page.locator('#category-filters button[data-category-id=""]').getAttribute('aria-pressed') === 'true' && (await Promise.all(['usage', 'style', 'background'].map(key => page.locator(`#${key}-filter`).inputValue()))).every(value => value === ''));
  await page.locator('#search').fill('NO_MATCH_SYNTHETIC_7f2e9182');
  await page.locator('#search-form').evaluate(form => form.requestSubmit());
  await waitCards(0);
  check('empty_state_action', await page.locator('#gallery-status button').count() > 0);
  await page.locator('#gallery-status button').first().click();
  await waitCards(24);
  await page.locator('#load-more').click();
  await waitCards(48);
  check('load_more_48_unique', (await cards.evaluateAll(nodes => new Set(nodes.map(node => node.dataset.groupId)).size)) === 48);
  while (await page.locator('#load-more').isVisible()) await page.locator('#load-more').click();
  await waitCards(catalog.groups.length);
  check('all_representative_groups_load_once', await cards.evaluateAll(nodes => new Set(nodes.map(node => node.dataset.groupId)).size) === catalog.groups.length);
  check('final_load_more_focus_preserved', await page.evaluate(() => document.activeElement?.matches('[data-action="details"]')));
  await reset();

  await page.evaluate(() => {
    Object.defineProperty(navigator, 'clipboard', {value: {writeText: () => Promise.reject(new DOMException('Not allowed', 'NotAllowedError'))}, configurable: true});
  });
  await cards.first().locator('[data-action="copy"]').click();
  await page.locator('#copy-dialog').waitFor({state: 'visible'});
  check('clipboard_denied_manual_fallback', await page.locator('#manual-prompt').inputValue() === original);
  await page.locator('#select-prompt').click();
  check('manual_select_exact_length', await page.locator('#manual-prompt').evaluate(node => node.selectionEnd - node.selectionStart) === original.length);
  await page.keyboard.press('Escape');

  for (const width of [390, 320]) {
    await page.setViewportSize({width, height: 844});
    await page.evaluate(() => window.scrollTo(0, 0));
    check(`mobile_${width}_no_overflow`, await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    if (width === 390) await page.screenshot({path: path.join(output, 'mobile.png')});
    await cards.first().locator('[data-action="details"]').last().click();
    await page.locator('#prompt-text').waitFor();
    await waitDetailImage(page);
    const dimensions = await page.locator('#detail-dialog').evaluate(node => ({scroll: node.scrollWidth, client: node.clientWidth, offenders: [...node.querySelectorAll('*')].filter(child => child.getBoundingClientRect().right > node.getBoundingClientRect().right + 1).slice(0, 8).map(child => ({tag: child.tagName, className: child.className, width: child.getBoundingClientRect().width}))}));
    if (dimensions.scroll > dimensions.client) console.log(JSON.stringify({mobile_overflow: {width, ...dimensions}}));
    check(`mobile_${width}_dialog_no_overflow`, dimensions.scroll <= dimensions.client);
    await page.keyboard.press('Escape');
  }

  // Deterministic regression data: group A has category/style on different members.
  // It must not satisfy category A + "other" by joining evidence across members.
  // All fixture assets are already local; this page never changes the real catalog.
  const fixturePage = await context.newPage();
  fixturePage.on('pageerror', error => errors.push(error.name));
  const fixturePrompt = '**Exact source prompt**\nKeep [source](https://example.test/source) unchanged.\n';
  const fixtureMember = (id, categories, style) => ({...first.representative, id, style_id: id,
    title: '[Reference title](https://example.test/title) (@author)', category_ids: categories,
    categories: categories.map(id => id === 'fixture-a' ? 'Fixture A' : 'Fixture B'),
    usage: ['fixture-use'], style, background: ['fixture-background'], keywords: [], original_prompt: fixturePrompt});
  const fixtureGroup = (id, hex, members) => ({id, representative_id: members[0].id, representative: members[0], members, detail_path: `data/groups/${hex}.json`});
  const fixtureGroups = [
    fixtureGroup('fixture-group-one', 'fffffffffffff001', [
      fixtureMember('fixture-one', ['fixture-a'], [' SHARED ', 'ＥＤＩＴＯＲＩＡＬ', 'solo-only', 'SOLO-ONLY']),
      fixtureMember('fixture-variant', ['fixture-b'], ['other', 'shared']),
    ]),
    fixtureGroup('fixture-group-two', 'fffffffffffff002', [fixtureMember('fixture-two', ['fixture-a'], ['other', 'editorial'])]),
    fixtureGroup('fixture-group-three', 'fffffffffffff003', [fixtureMember('fixture-three', ['fixture-b'], ['shared', 'ＥＤＩＴＯＲＩＡＬ'])]),
  ];
  const fixtureCatalog = {...catalog, groups: fixtureGroups,
    browse_categories: [{id: 'fixture-a', label: 'Fixture A'}, {id: 'fixture-b', label: 'Fixture B'}],
    counts: {images: 4, groups: 3, variants: 1, excluded: 0, withheld: 0}};
  await fixturePage.route('**/data/catalog.json', route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(fixtureCatalog)}));
  await fixturePage.route('**/data/groups/*.json', route => {
    const fixture = fixtureGroups.find(group => '/' + group.detail_path === new URL(route.request().url()).pathname);
    return fixture ? route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(fixture)}) : route.abort();
  });
  await fixturePage.goto(base.href, {waitUntil: 'networkidle'});
  await waitGroupIds(fixturePage, fixtureGroups.map(group => group.id));
  const fixedOptions = await facetOptions(fixturePage);
  const fixtureStyles = fixedOptions.style.map(option => ({value: normalize(option.value), label: option.label}));
  check('fixture_facets_deduplicate_nfkc_and_count_distinct_groups', fixtureStyles.length === 3 && fixtureStyles.find(option => option.value === 'editorial')?.label.endsWith('(3그룹)') && fixtureStyles.find(option => option.value === 'shared')?.label.endsWith('(2그룹)') && fixtureStyles.find(option => option.value === 'other')?.label.endsWith('(2그룹)'));
  check('fixture_singleton_hidden_despite_repeated_member_labels', !fixtureStyles.some(option => option.value === 'solo-only'));
  const fixtureCategoryLabels = await fixturePage.locator('#category-filters button').allTextContents();
  check('fixture_category_counts_are_groups_not_members', fixtureCategoryLabels.includes('Fixture A2') && fixtureCategoryLabels.includes('Fixture B2'));
  await chooseCategory(fixturePage, 'fixture-a');
  await fixturePage.locator('#style-filter').selectOption('other');
  await waitGroupIds(fixturePage, ['fixture-group-two']);
  check('fixture_category_and_facet_require_same_member');
  await chooseCategory(fixturePage, 'fixture-b');
  await waitGroupIds(fixturePage, ['fixture-group-one']);
  check('fixture_category_change_keeps_human_representative', await fixturePage.locator('#gallery > article').getAttribute('data-item-id') === 'fixture-one');
  check('fixture_granular_options_stable_after_category_and_facet', JSON.stringify(await facetOptions(fixturePage)) === JSON.stringify(fixedOptions));
  await fixturePage.locator('#search').fill('fixture-one');
  await fixturePage.locator('#search-form').evaluate(form => form.requestSubmit());
  await waitGroupIds(fixturePage, []);
  check('fixture_query_category_and_facet_require_same_member');
  await fixturePage.locator('#usage-filter').selectOption('fixture-use');
  await fixturePage.locator('#background-filter').selectOption('fixture-background');
  await fixturePage.locator('#sort').selectOption('title');
  await fixturePage.locator('#reset-filters').click();
  await waitGroupIds(fixturePage, fixtureGroups.map(group => group.id));
  check('fixture_reset_clears_all_active_controls', await fixturePage.locator('#search').inputValue() === '' && await fixturePage.locator('#sort').inputValue() === 'datetime' && await fixturePage.locator('#category-filters button[data-category-id=""]').getAttribute('aria-pressed') === 'true' && (await Promise.all(['usage', 'style', 'background'].map(key => fixturePage.locator(`#${key}-filter`).inputValue()))).every(value => value === ''));
  await fixturePage.locator('#search').fill('solo-only');
  await fixturePage.locator('#search-form').evaluate(form => form.requestSubmit());
  await waitGroupIds(fixturePage, ['fixture-group-one']);
  check('fixture_hidden_singleton_still_searchable');
  await fixturePage.locator('#gallery > article [data-action="details"]').last().click();
  await fixturePage.locator('#prompt-text').waitFor();
  check('fixture_singleton_remains_in_details', (await fixturePage.locator('#detail-content .detail-meta').innerText()).includes('solo-only'));
  check('fixture_title_cleanup_only_changes_display', await fixturePage.locator('#detail-title').textContent() === 'Reference title (@author)' && fixtureGroups[0].members[0].title === '[Reference title](https://example.test/title) (@author)' && await fixturePage.locator('#prompt-text').textContent() === fixturePrompt);
  await waitDetailImage(fixturePage);
  check('fixture_detail_image_decodes');
  await fixturePage.close();

  // Failure state must be actionable; recover without a page reload.
  const failPage = await context.newPage();
  let failCatalog = true;
  await failPage.route('**/data/catalog.json', route => failCatalog ? route.fulfill({status: 503, body: 'unavailable'}) : route.continue());
  await failPage.goto(base.href, {waitUntil: 'networkidle'});
  await failPage.locator('#gallery-status button').waitFor();
  check('catalog_failure_retry_visible');
  failCatalog = false;
  await failPage.locator('#gallery-status button').first().click();
  await failPage.waitForFunction(() => document.querySelectorAll('#gallery > article').length === 24);
  check('catalog_retry_recovers');
  await failPage.close();
  const emptyPage = await context.newPage();
  await emptyPage.route('**/data/catalog.json', route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({schema_version:'image-gallery-2',mode:'public',groups:[],counts:{images:0,groups:0,variants:0,excluded:379}})}));
  await emptyPage.goto(base.href, {waitUntil:'networkidle'});
  check('public_empty_projection_state_fixture', await emptyPage.locator('#gallery-status').innerText().then(text => text.includes('아직 공개할 이미지가 없습니다')));
  check('public_empty_hides_private_preview_banner', !(await emptyPage.locator('#preview-notice').isVisible()));
  await emptyPage.close();
  check('no_external_requests', external.length === 0);
  check('no_page_errors', errors.length === 0);
} catch (error) {
  errors.push(error instanceof assert.AssertionError ? error.message : String(error?.message || error).slice(0, 500));
  await page.screenshot({path: path.join(output, 'failure.png')}).catch(() => {});
  process.exitCode = 1;
} finally {
  const report = {schema_version: 'frontend-v2-browser-check-1', status: process.exitCode ? 'failed' : 'passed',
    base_url: base.href, counts: catalog?.counts, checks, errors, external_origins: [...new Set(external)],
    model_calls: 0, provider_calls: 0,
    catalog_sha256: catalog ? crypto.createHash('sha256').update(JSON.stringify(catalog)).digest('hex') : null};
  fs.writeFileSync(path.join(output, 'browser-report.json'), JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report, null, 2));
  await context.close();
  await browser.close();
}
