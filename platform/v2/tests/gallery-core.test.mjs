import test from 'node:test';
import assert from 'node:assert/strict';
import { PAGE_SIZE, MIN_FACET_GROUPS, normalizeText, safeResourcePath, collapseGroups, filterGroups, sortGroups, facetValues, categoryValues, memberCategoryIds, defaultFilters, displayTitle, validateCatalog, validateDetail } from '../frontend/gallery-core.mjs';

const member = (id, props = {}) => ({ id, style_id: id.toUpperCase(), title: id, usage: [], style: [], background: [], keywords: [], ...props });
const representative = member('bst-001', { title: '제품을 소개하는 형식', usage: ['상세페이지'], style: ['미니멀'] });
const variant = member('bst-002', { title: '음료의 변형', usage: ['포스터'], background: ['해변'], keywords: ['summer beverage'] });
const singleton = member('case-088', { title: '풍경 여행', usage: ['포스터'], style: ['사진'] });
const groups = [
  { id: 'g1', representative_id: representative.id, representative, members: [representative, variant], detail_path: 'data/groups/abc.json' },
  { id: 'g2', representative_id: singleton.id, representative: singleton, members: [singleton], detail_path: 'data/groups/def.json' },
];
const clone = value => JSON.parse(JSON.stringify(value));

test('one card per group, repeated candidate does not become a second card', () => {
  assert.equal(PAGE_SIZE, 24);
  assert.equal(collapseGroups([groups[0], groups[0], groups[1]]).length, 2);
});
test('variant-only query returns the human-selected representative', () => {
  const results = filterGroups(groups, { query: 'BST-002 summer' });
  assert.equal(results.length, 1);
  assert.equal(results[0].representative_id, 'bst-001');
  assert.equal(results[0].representative.id, 'bst-001');
});
test('AND keyword search, normalization, style ID and empty query', () => {
  assert.equal(filterGroups(groups, { query: '풍경 여행' }).length, 1);
  assert.equal(filterGroups(groups, { query: '풍경 알수없음' }).length, 0);
  assert.equal(filterGroups(groups, { query: '  ＢＳＴ-００２  ' }).length, 1);
  assert.equal(filterGroups(groups, { query: ' ' }).length, 2);
  assert.equal(normalizeText(' ＡBC\n가 '), 'abc 가');
});
test('one matching member satisfies every facet and preserves representative', () => {
  const result = filterGroups(groups, { query: 'summer', usage: '포스터', background: '해변' });
  assert.equal(result.length, 1);
  assert.equal(result[0].representative_id, 'bst-001');
  assert.equal(filterGroups(groups, { background: '실내' }).length, 0);
});
test('different members cannot supply unrelated query or facet conditions', () => {
  assert.equal(filterGroups(groups, { usage: '포스터', style: '미니멀', background: '해변' }).length, 0);
  assert.equal(filterGroups(groups, { query: 'summer', style: '미니멀' }).length, 0);
  assert.equal(filterGroups(groups, { query: 'bst-001 summer' }).length, 0);
});
test('facet counts count groups, not repeated labels in variant images', () => {
  const input = clone(groups);
  input[0].representative.usage = ['포스터', '포스터'];
  input[0].members[0].usage = ['포스터'];
  assert.deepEqual(facetValues(input, 'usage'), [{ value: '포스터', count: 2 }]);
});
test('facet options require two distinct groups, not two variants of one group', () => {
  const input = clone(groups);
  input[0].members[0].style = ['미니멀'];
  input[0].members[1].style = ['미니멀'];
  assert.equal(MIN_FACET_GROUPS, 2);
  assert.deepEqual(facetValues(input, 'style'), []);
  input[1].members[0].style = ['미니멀'];
  assert.deepEqual(facetValues(input, 'style'), [{ value: '미니멀', count: 2 }]);
  assert.equal(filterGroups(groups, { query: '미니멀' }).length, 1, 'hidden singleton remains searchable');
});
test('facet counts normalize compatibility characters, case and whitespace', () => {
  const input = clone(groups);
  input[0].members[0].style = ['  ＰＨＯＴＯ　 Real  ', 'photo real'];
  input[0].members[1].style = ['PHOTO\nreal'];
  input[1].members[0].style = ['photo    REAL'];
  assert.deepEqual(facetValues(input, 'style'), [{ value: 'PHOTO Real', count: 2 }]);
  assert.equal(filterGroups(input, { style: 'PHOTO Real' }).length, 2);
  assert.equal(facetValues(input, 'style')[0].count, 2, 'variants are counted only once');
});
test('primary category and every other condition must cooccur in one member', () => {
  const input = clone(groups);
  input[0].members[0].category_ids = ['product-brand'];
  input[0].members[0].categories = ['상품·브랜드'];
  input[0].members[1].category_ids = ['content-publication'];
  input[0].members[1].categories = ['콘텐츠·출판'];
  assert.equal(filterGroups(input, { category: 'product-brand', query: 'summer' }).length, 0);
  assert.equal(filterGroups(input, { category: 'content-publication', style: '미니멀' }).length, 0);
  const result = filterGroups(input, { category: 'content-publication', query: 'summer', usage: '포스터', background: '해변' });
  assert.equal(result.length, 1);
  assert.equal(result[0].representative_id, 'bst-001');
  assert.equal(filterGroups(input, { query: '콘텐츠·출판' }).length, 1);
});
test('category counts use distinct groups and only actual categories', () => {
  const input = clone(groups);
  input[0].members[0].category_ids = ['product-brand'];
  input[0].members[1].category_ids = ['product-brand'];
  input[1].members[0].category_ids = ['product-brand'];
  const definitions = [{ id: 'product-brand', label: '상품·브랜드' }, { id: 'characters', label: '캐릭터' }];
  assert.deepEqual(categoryValues(input, definitions), [{ id: 'product-brand', label: '상품·브랜드', count: 2 }]);
});
test('old catalogs safely browse as unclassified and reset clears every condition', () => {
  assert.deepEqual(memberCategoryIds(representative), ['unclassified']);
  assert.deepEqual(categoryValues(groups), [{ id: 'unclassified', label: '미분류', count: 2 }]);
  assert.equal(filterGroups(groups, { category: 'unclassified' }).length, 2);
  assert.equal(filterGroups(groups, { category: 'not-in-catalog' }).length, 0);
  const selected = defaultFilters();
  selected.category = 'product-brand'; selected.query = 'summer'; selected.usage = '포스터';
  assert.deepEqual(defaultFilters(), { query: '', category: '', usage: '', style: '', background: '', sort: 'datetime' });
  assert.equal(filterGroups(groups, defaultFilters()).length, groups.length);
});
test('display title removes link syntax and raw URLs without editing the record', () => {
  const source = { title: '[제품 사진](https://example.test/long/path) — https://x.com/author/post', original_prompt: 'line one\nhttps://x.com/author/post\n' };
  const original = clone(source);
  assert.equal(displayTitle(source.title, 'ID-1'), '제품 사진');
  assert.equal(displayTitle('https://x.com/author/post', 'ID-1'), 'ID-1');
  assert.equal(displayTitle('제품 사진', 'ID-1'), '제품 사진');
  assert.deepEqual(source, original);
});
test('sort does not mutate input or choose a new representative', () => {
  const input = [groups[1], groups[0]];
  const sorted = sortGroups(input, 'variants');
  assert.equal(sorted[0].id, 'g1');
  assert.equal(input[0].id, 'g2');
  assert.equal(sorted[0].representative, representative);
  assert.equal(sortGroups(groups, 'id')[0].id, 'g1');
});
test('safe relative resources reject external URLs and traversal', () => {
  assert.equal(safeResourcePath('data/groups/abc.json'), 'data/groups/abc.json');
  assert.equal(safeResourcePath('assets/thumb-a.webp'), 'assets/thumb-a.webp');
  for (const bad of ['../secret', '/absolute', '//host/x', 'https://host/x', 'data/a/../x', 'data\\x', '%2e%2e/secret', 'data/%2e%2e/secret', 'data/%2fsecret', 'data/x?token=1', 'data/#x', 'data//x', 'data/%']) assert.equal(safeResourcePath(bad), null, bad);
});
test('catalog validates explicit mode and member identity boundaries', () => {
  const good = { schema_version: 'image-gallery-2', mode: 'private_local_preview', groups };
  assert.equal(validateCatalog(good), good);
  assert.throws(() => validateCatalog({ ...good, mode: 'approved' }));
  const wrongRep = clone(good); wrongRep.groups[0].representative_id = 'not-a-member';
  assert.throws(() => validateCatalog(wrongRep));
  const repeated = clone(good); repeated.groups[1].members.push(clone(variant));
  assert.throws(() => validateCatalog(repeated));
  const badPath = clone(good); badPath.groups[0].detail_path = '../private.json';
  assert.throws(() => validateCatalog(badPath));
});
test('detail preserves original prompt bytes and rejects crossed group data', () => {
  const prompt = '{\n  "title": "한글",\r\n  "spacing": "  "\n}\n';
  const detail = { id: 'g1', representative_id: 'bst-001', members: [ { ...representative, original_prompt: prompt }, { ...variant, original_prompt: '' } ] };
  assert.equal(validateDetail(detail, groups[0]).members[0].original_prompt, prompt);
  assert.throws(() => validateDetail({ ...detail, id: 'g2' }, groups[0]));
  const repeated = clone(detail); repeated.members[1].id = repeated.members[0].id;
  assert.throws(() => validateDetail(repeated, groups[0]));
});
