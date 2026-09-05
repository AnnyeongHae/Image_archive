import { PAGE_SIZE, filterGroups, sortGroups, facetValues, categoryValues, defaultFilters, displayTitle, safeResourcePath, validateCatalog, validateDetail } from './gallery-core.mjs';

const $ = id => document.getElementById(id);
const state = { catalog: null, filtered: [], visible: PAGE_SIZE, category: '', detailCache: new Map(), detailSequence: 0 };
const facets = ['usage', 'style', 'background'];
let searchTimer;
let toastTimer;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(text, className, onClick, action) {
  const node = el('button', className, text);
  node.type = 'button';
  if (action) node.dataset.action = action;
  if (onClick) node.addEventListener('click', onClick);
  return node;
}

function strings(values) { return Array.isArray(values) ? values.filter(value => typeof value === 'string' && value.trim()) : []; }
function displayId(member) { return member.style_id || member.id; }
function titleOf(member) { return displayTitle(member.title, displayId(member)); }
function number(value) { return Number(value).toLocaleString('ko-KR'); }

function safeExternalURL(value) {
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

function sourceLink(source) {
  const url = safeExternalURL(source?.url);
  if (!url) return el('span', '', source?.name || '출처 정보 없음');
  const link = el('a', '', `${source.name || '원문 출처'} ↗`);
  link.href = url;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  return link;
}

function picture(image, alt, { lazy = true, webp = true, className = '' } = {}) {
  const wrapper = el('picture', className);
  const fallback = () => {
    const missing = el('span', 'image-failure');
    missing.append(el('span', '', '▧'), el('span', '', '이미지를 불러오지 못했습니다'));
    missing.setAttribute('role', 'img');
    missing.setAttribute('aria-label', alt ? `${alt} · 이미지 표시 불가` : '이미지 표시 불가');
    wrapper.replaceWith(missing);
  };
  const src = safeResourcePath(image?.src);
  const webpSrc = webp && safeResourcePath(image?.webp);
  if (!src && !webpSrc) {
    wrapper.append(el('span', 'image-failure', '이미지 준비 중'));
    return wrapper;
  }
  if (webpSrc && src) {
    const source = el('source');
    source.type = 'image/webp';
    source.srcset = webpSrc;
    wrapper.append(source);
  }
  const img = el('img');
  img.alt = alt;
  img.loading = lazy ? 'lazy' : 'eager';
  img.decoding = 'async';
  if (Number(image.width) > 0) img.width = Number(image.width);
  if (Number(image.height) > 0) img.height = Number(image.height);
  img.addEventListener('error', () => {
    const source = wrapper.querySelector('source');
    if (source) { source.remove(); img.src = src; } else { fallback(); }
  });
  img.src = src || webpSrc;
  wrapper.append(img);
  return wrapper;
}

async function fetchJSON(path) {
  if (!safeResourcePath(path)) throw new Error('허용되지 않은 데이터 경로입니다.');
  const response = await fetch(path, { credentials: 'omit' });
  if (!response.ok) throw new Error(`데이터를 불러오지 못했습니다 (HTTP ${response.status}).`);
  return response.json();
}

async function getDetail(group) {
  if (state.detailCache.has(group.id)) return state.detailCache.get(group.id);
  const request = fetchJSON(group.detail_path).then(detail => validateDetail(detail, group));
  state.detailCache.set(group.id, request);
  try { return await request; } catch (error) { state.detailCache.delete(group.id); throw error; }
}

function announce(message) {
  clearTimeout(toastTimer);
  $('toast').textContent = message;
  $('toast').hidden = false;
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, 3500);
}

function showManualCopy(prompt) {
  $('manual-prompt').value = prompt;
  if (!$('copy-dialog').open) $('copy-dialog').showModal();
  $('manual-prompt').focus();
  $('manual-prompt').select();
}

async function copyExact(prompt, trigger) {
  if (!prompt.trim()) { announce('보존된 원문 프롬프트가 없습니다.'); return; }
  try {
    if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
    await navigator.clipboard.writeText(prompt);
    const oldText = trigger.textContent;
    trigger.textContent = '복사 완료 ✓';
    setTimeout(() => { if (trigger.isConnected) trigger.textContent = oldText; }, 2400);
    announce('원문 프롬프트를 그대로 복사했습니다.');
  } catch { showManualCopy(prompt); }
}

function inlineError(container, message, retry) {
  const previous = container.querySelector(':scope > .inline-error');
  if (previous) previous.remove();
  const notice = el('div', 'inline-error');
  notice.setAttribute('role', 'alert');
  notice.append(el('p', '', message));
  if (retry) notice.append(button('다시 시도', 'secondary-button', retry));
  container.append(notice);
}

function makeVariant(member, group, activeId, onChoose) {
  const variant = button('', 'variant-button', () => onChoose(member.id), 'open-member');
  variant.dataset.itemId = member.id;
  variant.setAttribute('aria-label', `${displayId(member)} · ${titleOf(member)} 상세 보기${member.id === group.representative_id ? ' · 대표 이미지' : ''}`);
  variant.setAttribute('aria-pressed', String(member.id === activeId));
  variant.append(picture(member.thumbnail, ''), el('span', '', displayId(member)));
  return variant;
}

function makeCard(group, index) {
  const member = group.representative;
  const card = el('article', 'image-card');
  card.dataset.groupId = group.id;
  card.dataset.itemId = member.id;
  const visual = el('div', 'card-image-wrap');
  const imageButton = button('', 'image-open', () => openDetail(group, member.id), 'details');
  imageButton.setAttribute('aria-label', `${titleOf(member)} · 이미지와 원문 보기`);
  imageButton.append(picture(member.thumbnail, titleOf(member), { lazy: index > 2 }));
  visual.append(imageButton);
  if (group.members.length > 1) {
    const groupButton = button(`대표 · ${number(group.members.length)}개 이미지`, 'group-label group-label-button', () => openDetail(group, member.id), 'details');
    groupButton.setAttribute('aria-label', `${displayId(member)} 그룹 대표 이미지 ${number(group.members.length)}개 보기`);
    visual.append(groupButton);
  }
  visual.append(el('span', 'card-index', displayId(member)));
  const body = el('div', 'card-body');
  body.append(el('p', 'card-kicker', displayId(member)), el('h3', 'card-title', titleOf(member)));
  const tags = el('div', 'card-tags');
  const usage = strings(member.usage);
  usage.slice(0, 2).forEach(tag => tags.append(el('span', 'tag', tag)));
  if (usage.length < 2) strings(member.style).slice(0, 2 - usage.length).forEach(tag => tags.append(el('span', 'tag neutral', tag)));
  body.append(tags);
  const actions = el('div', 'card-actions');
  const copy = button('프롬프트 복사', 'copy-button', async () => {
    copy.disabled = true;
    const oldError = body.querySelector(':scope > .inline-error');
    if (oldError) oldError.remove();
    try {
      const detail = await getDetail(group);
      await copyExact(detail.members.find(item => item.id === member.id).original_prompt, copy);
    } catch { inlineError(body, '원문을 불러오지 못했습니다. 복사 버튼을 다시 눌러 주세요.'); }
    finally { copy.disabled = false; }
  }, 'copy');
  copy.setAttribute('aria-label', `${displayId(member)} 원문 프롬프트 복사`);
  const details = button('상세 보기 ↗', 'card-detail', () => openDetail(group, member.id), 'details');
  details.setAttribute('aria-label', `${displayId(member)} 상세 보기`);
  actions.append(copy, details);
  body.append(actions);
  const rights = el('div', 'card-rights');
  if (member.source) rights.append(sourceLink(member.source));
  rights.append(el('span', '', member.rights?.badge || '출처·권리 상세 확인'));
  body.append(rights);
  card.append(visual, body);
  return card;
}

function metadataRow(label, values) {
  const row = el('div', 'meta-row');
  const valuesNode = el('div', 'meta-values');
  const labels = strings(values);
  if (labels.length) labels.forEach(value => valuesNode.append(el('span', 'tag neutral', value)));
  else valuesNode.append(el('span', '', '미분류'));
  row.append(el('span', 'meta-label', label), valuesNode);
  return row;
}

function renderDetail(group, detail, selectedId) {
  const member = detail.members.find(item => item.id === selectedId);
  if (!member) throw new Error('선택한 이미지를 찾을 수 없습니다.');
  const layout = el('div', 'detail-layout');
  layout.dataset.itemId = member.id;
  const visual = el('div', 'detail-visual');
  visual.append(picture(member.image || member.thumbnail, titleOf(member), { lazy: false, webp: false, className: 'detail-main-image' }));
  if (detail.members.length > 1) {
    const variants = el('div', 'variant-grid');
    variants.setAttribute('aria-label', '같은 그룹에서 이미지 선택');
    for (const item of detail.members) variants.append(makeVariant(item, group, member.id, id => {
      renderDetail(group, detail, id);
      const chosen = [...$('detail-content').querySelectorAll('[data-action="open-member"]')].find(node => node.dataset.itemId === id);
      chosen?.focus({ preventScroll: true });
    }));
    visual.append(variants);
  }
  const info = el('div', 'detail-info');
  info.append(el('p', 'card-kicker', `${displayId(member)}${member.id === group.representative_id ? ' · 그룹 대표' : ' · 그룹 내 변형'}`));
  const heading = el('h2', '', titleOf(member));
  heading.id = 'detail-title';
  info.append(heading);
  const metadata = el('div', 'detail-meta');
  metadata.append(metadataRow('분야', strings(member.categories).length ? member.categories : ['미분류']), metadataRow('활용', member.usage), metadataRow('스타일', member.style), metadataRow('배경', member.background));
  info.append(metadata);
  const metadataStatus = member.metadata_status === 'human_verified' ? '활용·스타일·배경: 사람이 검토한 메타데이터' : member.metadata_status === 'candidate' ? '활용·스타일·배경: AI 분석 제안 · 사람의 메타데이터 검수 전' : '활용·스타일·배경: 별도 메타데이터 없음';
  info.append(el('p', 'metadata-note', metadataStatus));
  const promptHeading = el('div', 'prompt-heading');
  const copy = button('원문 복사', 'copy-button', () => copyExact(member.original_prompt, copy), 'copy');
  copy.disabled = !member.original_prompt.trim();
  copy.setAttribute('aria-label', `${displayId(member)} 원문 프롬프트 복사`);
  promptHeading.append(el('h3', '', '원문 프롬프트'), copy);
  const prompt = el('pre', 'original-prompt', member.original_prompt || '보존된 원문 프롬프트가 없습니다.');
  prompt.id = 'prompt-text';
  prompt.tabIndex = 0;
  prompt.setAttribute('aria-label', '원문 프롬프트');
  info.append(promptHeading, prompt, el('p', 'prompt-note', '원문 내용은 바꾸지 않습니다. 클립보드의 줄바꿈 방식은 운영체제에 따라 달라질 수 있습니다.'));
  const rights = el('section', 'rights-panel');
  rights.append(el('strong', '', '참고용 · 권리 미확인'), el('p', '', '재사용 전 원출처의 이용 조건을 확인하세요.'));
  if (member.rights?.attribution) rights.append(el('p', '', `저작자 표시: ${member.rights.attribution}`));
  if (member.rights?.license) rights.append(el('p', '', `관측된 라이선스: ${member.rights.license}`));
  const source = el('p');
  source.append(sourceLink(member.source));
  rights.append(source);
  info.append(rights);
  layout.append(visual, info);
  $('detail-content').replaceChildren(layout);
}

async function openDetail(group, memberId) {
  const sequence = ++state.detailSequence;
  const loading = el('div', 'detail-loading');
  const heading = el('h2', '', '이미지 상세');
  heading.id = 'detail-title';
  loading.append(heading, el('p', '', '이미지와 원문을 불러오고 있습니다.'));
  $('detail-content').replaceChildren(loading);
  if (!$('detail-dialog').open) $('detail-dialog').showModal();
  try {
    const detail = await getDetail(group);
    if (sequence !== state.detailSequence || !$('detail-dialog').open) return;
    renderDetail(group, detail, memberId);
  } catch {
    if (sequence !== state.detailSequence || !$('detail-dialog').open) return;
    const error = el('div', 'detail-loading');
    const errorHeading = el('h2', '', '상세 정보를 불러오지 못했습니다');
    errorHeading.id = 'detail-title';
    error.append(errorHeading, el('p', '', '연결 또는 데이터 파일을 확인한 뒤 다시 시도해 주세요.'), button('다시 시도', 'primary-button', () => openDetail(group, memberId)));
    $('detail-content').replaceChildren(error);
  }
}

function renderPage(append = false) {
  if (!state.catalog) return;
  const shown = Math.min(state.visible, state.filtered.length);
  const start = append ? $('gallery').children.length : 0;
  const cards = state.filtered.slice(start, shown).map((group, offset) => makeCard(group, start + offset));
  if (append) $('gallery').append(...cards); else $('gallery').replaceChildren(...cards);
  $('result-count').textContent = number(state.filtered.length);
  $('gallery').setAttribute('aria-busy', 'false');
  $('load-more').hidden = shown >= state.filtered.length;
  $('page-progress').textContent = state.filtered.length ? `${number(state.filtered.length)}개 대표 중 ${number(shown)}개 표시` : '';
  const memberCount = state.filtered.reduce((sum, group) => sum + group.members.length, 0);
  $('results-description').textContent = state.filtered.length ? `대표 ${number(state.filtered.length)}개 · 연결된 이미지 ${number(memberCount)}개. 같은 형식의 변형은 대표 아래에서 펼쳐볼 수 있습니다.` : '현재 조건에 맞는 대표 이미지가 없습니다.';
  $('gallery-status').replaceChildren();
  if (!state.filtered.length) {
    const emptyCatalog = !state.catalog.groups.length;
    $('gallery-status').append(el('h3', '', emptyCatalog ? '아직 공개할 이미지가 없습니다' : '조금 다른 단어로 찾아볼까요?'), el('p', '', emptyCatalog ? '공개·권리 검토를 통과한 이미지가 준비되면 이곳에 표시됩니다.' : '검색어를 짧게 바꾸거나, 선택한 분야·활용·스타일·배경을 해제해 보세요.'), button(emptyCatalog ? '목록 새로고침' : '검색·필터 초기화', 'primary-button', emptyCatalog ? loadCatalog : resetFilters));
  }
}

function applyFilters() {
  if (!state.catalog) return;
  const filters = { query: $('search').value, category: state.category };
  facets.forEach(key => { filters[key] = $(`${key}-filter`).value; });
  state.filtered = sortGroups(filterGroups(state.catalog.groups, filters), $('sort').value);
  state.visible = PAGE_SIZE;
  $('category-filters').querySelectorAll('[data-category-id]').forEach(node => node.setAttribute('aria-pressed', String(node.dataset.categoryId === filters.category)));
  renderPage();
}

function resetFilters() {
  clearTimeout(searchTimer);
  const defaults = defaultFilters();
  $('search').value = defaults.query;
  state.category = defaults.category;
  facets.forEach(key => { $(`${key}-filter`).value = defaults[key]; });
  $('sort').value = defaults.sort;
  applyFilters();
  $('search').focus();
}

function fillFilters() {
  const names = { usage: '활용', style: '스타일', background: '배경' };
  for (const key of facets) {
    const select = $(`${key}-filter`);
    const initial = el('option', '', `모든 ${names[key]}`);
    initial.value = '';
    select.replaceChildren(initial);
    for (const { value, count } of facetValues(state.catalog.groups, key)) {
      const option = el('option', '', `${value} (${number(count)}그룹)`);
      option.value = value;
      select.append(option);
    }
  }
  $('category-filters').replaceChildren();
  const categories = [{ id: '', label: '전체', count: state.catalog.groups.length }, ...categoryValues(state.catalog.groups, state.catalog.browse_categories)];
  for (const { id, label, count } of categories) {
    const category = button('', 'quick-filter category-filter', () => { state.category = id; applyFilters(); });
    category.dataset.categoryId = id;
    category.setAttribute('aria-pressed', String(state.category === id));
    category.setAttribute('aria-label', `${label} · ${number(count)}개 그룹`);
    category.append(el('span', '', label), el('span', 'category-count', number(count)));
    $('category-filters').append(category);
  }
}

async function loadCatalog() {
  state.catalog = null;
  state.detailCache.clear();
  $('gallery-status').replaceChildren();
  $('gallery').setAttribute('aria-busy', 'true');
  $('gallery').replaceChildren(...Array.from({ length: 6 }, () => { const placeholder = el('div', 'loading-card'); placeholder.setAttribute('aria-hidden', 'true'); return placeholder; }));
  $('results-description').textContent = '이미지 목록을 불러오고 있습니다.';
  $('load-more').hidden = true;
  try {
    const catalog = validateCatalog(await fetchJSON('data/catalog.json'));
    if (catalog.mode === 'private_local_preview' && !['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname)) throw new Error('비공개 미리보기는 로컬 주소에서만 열 수 있습니다.');
    state.catalog = catalog;
    state.category = '';
    $('preview-notice').hidden = catalog.mode !== 'private_local_preview';
    $('group-count').textContent = number(catalog.groups.length);
    $('image-count').textContent = number(catalog.groups.reduce((sum, group) => sum + group.members.length, 0));
    fillFilters();
    applyFilters();
  } catch (error) {
    $('gallery').replaceChildren();
    $('gallery').setAttribute('aria-busy', 'false');
    $('results-description').textContent = '목록을 열 수 없습니다. 자동으로 재시도하지 않습니다.';
    $('gallery-status').append(el('h3', '', '라이브러리를 불러오지 못했습니다'), el('p', '', location.protocol === 'file:' ? '파일을 직접 여는 대신 프로젝트의 로컬 미리보기 서버를 이용해 주세요.' : error.message || '연결 상태와 데이터 파일을 확인해 주세요.'), button('다시 시도', 'primary-button', loadCatalog));
  }
}

$('search-form').addEventListener('submit', event => { event.preventDefault(); clearTimeout(searchTimer); applyFilters(); });
$('search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(applyFilters, 150); });
facets.forEach(key => $(`${key}-filter`).addEventListener('change', applyFilters));
$('sort').addEventListener('change', applyFilters);
$('reset-filters').addEventListener('click', resetFilters);
$('load-more').addEventListener('click', () => {
  const firstNewIndex = $('gallery').children.length;
  const restoreFocus = document.activeElement === $('load-more');
  state.visible += PAGE_SIZE;
  renderPage(true);
  if (restoreFocus && $('load-more').hidden) $('gallery').children[firstNewIndex]?.querySelector('[data-action="details"]')?.focus();
});
$('close-dialog').addEventListener('click', () => $('detail-dialog').close());
$('detail-dialog').addEventListener('close', () => { state.detailSequence += 1; });
$('close-copy').addEventListener('click', () => $('copy-dialog').close());
$('select-prompt').addEventListener('click', () => { $('manual-prompt').focus(); $('manual-prompt').select(); });
loadCatalog();
