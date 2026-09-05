/** Pure gallery projections. A search hit never replaces the human-selected representative. */
export const PAGE_SIZE = 24;
export const UNCLASSIFIED_CATEGORY = 'unclassified';
export const MIN_FACET_GROUPS = 2;

export function defaultFilters() {
  return { query: '', category: '', usage: '', style: '', background: '', sort: 'datetime' };
}

export function normalizeText(value) {
  return String(value ?? '').normalize('NFKC').toLocaleLowerCase('ko').replace(/\s+/gu, ' ').trim();
}

export function safeResourcePath(value) {
  if (typeof value !== 'string' || !value || value.startsWith('/') || /[\\?#:]/u.test(value)) return null;
  let decoded;
  try { decoded = decodeURIComponent(value); } catch { return null; }
  if (decoded !== value && /[\\/:?#]/u.test(decoded)) return null;
  if (decoded.split('/').some(part => !part || part === '.' || part === '..')) return null;
  return value;
}

export function collapseGroups(groups = []) {
  const seen = new Set();
  return groups.filter(group => {
    if (seen.has(group.id)) return false;
    seen.add(group.id);
    return true;
  });
}

function labels(member, key) {
  return Array.isArray(member?.[key]) ? member[key].filter(value => typeof value === 'string') : [];
}

/** Only presentation changes; original title and prompt records are never rewritten. */
export function displayTitle(value, fallback = '') {
  const title = String(value ?? '').trim();
  const cleaned = title.replace(/!?\[([^\]\r\n]+)\]\(\s*https?:\/\/[^)\s]+\s*\)/gu, '$1')
    .replace(/<?https?:\/\/[^\s<>]+>?/gu, '')
    .replace(/\s+/gu, ' ').replace(/^[\s|·:—-]+|[\s|·:—-]+$/gu, '').trim();
  return cleaned || fallback;
}

export function memberCategoryIds(member) {
  const ids = [...new Set(labels(member, 'category_ids').map(value => value.trim()).filter(Boolean))];
  return ids.length ? ids : [UNCLASSIFIED_CATEGORY];
}

export function categoryValues(groups, definitions = []) {
  const counts = new Map();
  for (const group of collapseGroups(groups)) {
    const ids = new Set((group.members ?? []).flatMap(memberCategoryIds));
    for (const id of ids) counts.set(id, (counts.get(id) ?? 0) + 1);
  }
  const known = new Map();
  for (const item of definitions) {
    if (typeof item?.id === 'string' && typeof item.label === 'string' && item.id.trim() && item.label.trim()) known.set(item.id.trim(), item.label.trim());
  }
  if (counts.get(UNCLASSIFIED_CATEGORY)) known.set(UNCLASSIFIED_CATEGORY, '미분류');
  return [...known].filter(([id]) => (counts.get(id) ?? 0) > 0).map(([id, label]) => ({ id, label, count: counts.get(id) }));
}

export function groupSearchText(group) {
  return normalizeText([group.representative, ...(group.members ?? [])].flatMap(member => [
    member?.id, member?.style_id, member?.title,
    ...labels(member, 'usage'), ...labels(member, 'style'), ...labels(member, 'background'), ...labels(member, 'keywords'), ...labels(member, 'categories'),
  ]).join(' '));
}

function memberSearchText(member) {
  return normalizeText([member?.id, member?.style_id, member?.title,
    ...labels(member, 'usage'), ...labels(member, 'style'), ...labels(member, 'background'), ...labels(member, 'keywords'), ...labels(member, 'categories'),
  ].join(' '));
}

export function filterGroups(groups, filters = {}) {
  const tokens = normalizeText(filters.query).split(' ').filter(Boolean);
  return collapseGroups(groups).filter(group => (group.members ?? []).some(member => {
    if (filters.category && !memberCategoryIds(member).includes(filters.category)) return false;
    const searchable = memberSearchText(member);
    if (!tokens.every(token => searchable.includes(token))) return false;
    return ['usage', 'style', 'background'].every(key => !filters[key] ||
      labels(member, key).some(label => normalizeText(label) === normalizeText(filters[key])));
  }));
}

export function sortGroups(groups, order = 'recommended') {
  const result = [...groups];
  if (order === 'datetime' || order === 'recommended') result.sort((a, b) => {
    const at = Date.parse(a.datetime || a.representative?.datetime || '') || 0;
    const bt = Date.parse(b.datetime || b.representative?.datetime || '') || 0;
    return bt - at || a.id.localeCompare(b.id);
  });
  if (order === 'title') result.sort((a, b) => String(a.representative.title).localeCompare(String(b.representative.title), 'ko') || a.id.localeCompare(b.id));
  if (order === 'variants') result.sort((a, b) => b.members.length - a.members.length || a.id.localeCompare(b.id));
  if (order === 'id') result.sort((a, b) => String(a.representative.style_id || a.representative.id).localeCompare(String(b.representative.style_id || b.representative.id), 'ko', { numeric: true }));
  return result;
}

export function facetValues(groups, key, minGroups = MIN_FACET_GROUPS) {
  const counts = new Map();
  const display = new Map();
  for (const group of collapseGroups(groups)) {
    const values = new Set();
    for (const raw of (group.members ?? []).flatMap(member => labels(member, key))) {
      const normalized = normalizeText(raw);
      if (!normalized) continue;
      if (!display.has(normalized)) display.set(normalized, raw.normalize('NFKC').replace(/\s+/gu, ' ').trim());
      values.add(normalized);
    }
    for (const normalized of values) counts.set(normalized, (counts.get(normalized) ?? 0) + 1);
  }
  return [...counts].filter(([, count]) => count >= minGroups).map(([normalized, count]) => ({ value: display.get(normalized), count })).sort((a, b) => b.count - a.count || a.value.localeCompare(b.value, 'ko'));
}

export function validateCatalog(catalog) {
  if (catalog?.schema_version !== 'image-gallery-2' || !['private_local_preview', 'public'].includes(catalog.mode) || !Array.isArray(catalog.groups)) throw new Error('지원하지 않는 갤러리 데이터입니다.');
  const groups = new Set();
  const members = new Set();
  for (const group of catalog.groups) {
    if (!group?.id || groups.has(group.id) || !group.representative || !Array.isArray(group.members) || !group.members.length ||
      group.representative.id !== group.representative_id || !group.members.some(member => member.id === group.representative_id) ||
      !safeResourcePath(group.detail_path) || !group.detail_path.startsWith('data/groups/')) throw new Error('그룹 데이터의 대표 또는 경로를 확인할 수 없습니다.');
    groups.add(group.id);
    for (const member of group.members) {
      if (!member?.id || members.has(member.id)) throw new Error('중복된 이미지 연결이 있어 목록을 열지 않았습니다.');
      members.add(member.id);
    }
  }
  return catalog;
}

export function validateDetail(detail, group) {
  if (detail?.id !== group.id || detail.representative_id !== group.representative_id || !Array.isArray(detail.members) || detail.members.length !== group.members.length) throw new Error('상세 데이터가 현재 그룹과 일치하지 않습니다.');
  const expected = new Set(group.members.map(member => member.id));
  for (const member of detail.members) {
    if (!expected.delete(member.id) || typeof member.original_prompt !== 'string') throw new Error('이미지 또는 원문 데이터를 확인할 수 없습니다.');
  }
  return detail;
}
