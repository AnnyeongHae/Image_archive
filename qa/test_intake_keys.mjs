import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { ensureIntakeKeys, verifyPair } from '../platform/v2/local/intake_keys.mjs';
import { sealBytes, unsealBytes } from '../src/github_sources/seal_intake.mjs';

async function temporary(run) {
  const root = await mkdtemp(path.join(tmpdir(), 'archive-intake-key-test-'));
  // Isolate the synthetic local-key tests from the runner's Actions marker.
  // The explicit Actions-denial test below still sets and verifies that guard.
  const actions = process.env.GITHUB_ACTIONS;
  delete process.env.GITHUB_ACTIONS;
  try { return await run(root); }
  finally {
    if (actions === undefined) delete process.env.GITHUB_ACTIONS;
    else process.env.GITHUB_ACTIONS = actions;
    await rm(root, { recursive: true, force: true }); // exact test-owned root
  }
}

test('dry-run creates no directories or keys', () => temporary(async (root) => {
  const result = await ensureIntakeKeys({ archiveRoot: root });
  assert.equal(result.status, 'dry_run_would_generate_rsa_oaep_sha256_3072');
  assert.equal(result.writes, 0);
  assert.deepEqual(await readdir(root), []);
}));

test('apply creates 3072-bit pair; repeat verifies without overwriting', () => temporary(async (root) => {
  const first = await ensureIntakeKeys({ archiveRoot: root, apply: true });
  assert.equal(first.status, 'generated_and_verified');
  assert.equal(first.acl, process.platform === 'win32' ? 'inherited_workspace_not_hardened' : 'posix_0600');
  const publicBytes = await readFile(first.public_key_path);
  const privateBytes = await readFile(first.private_key_path);
  const pub = JSON.parse(publicBytes), secret = JSON.parse(privateBytes);
  assert.equal(Buffer.from(pub.n, 'base64url').length, 384);
  assert.equal(pub.alg, 'RSA-OAEP-256');
  assert.equal('d' in pub, false);
  assert.match(first.recipient_key_sha256, /^[a-f0-9]{64}$/);
  assert.equal(JSON.stringify(first).includes(secret.d), false);
  const repeated = await ensureIntakeKeys({ archiveRoot: root, apply: true });
  assert.equal(repeated.status, 'verified_existing');
  assert.equal(repeated.writes, 0);
  assert.equal(first.recipient_key_sha256, repeated.recipient_key_sha256);
  assert.deepEqual(await readFile(first.private_key_path), privateBytes);
  assert.deepEqual(await readFile(first.public_key_path), publicBytes);
  const message = Buffer.from('sealed transport compatibility check');
  assert.deepEqual(await unsealBytes(await sealBytes(message, pub), secret), message);
}));

test('partial pair is preserved and blocks apply', () => temporary(async (root) => {
  await mkdir(path.join(root, 'config'));
  const publicPath = path.join(root, 'config/intake-recipient.public.jwk.json');
  await writeFile(publicPath, 'partial fixture');
  await assert.rejects(ensureIntakeKeys({ archiveRoot: root, apply: true }), { code: 'partial_keypair_preserved_manual_review_required' });
  assert.equal((await readFile(publicPath)).toString(), 'partial fixture');
  assert.deepEqual((await readdir(root)).sort(), ['config']);
}));

test('mismatched or malformed existing pair never gets repaired', () => temporary(async (root) => {
  const generated = await ensureIntakeKeys({ archiveRoot: root, apply: true });
  const existingPrivate = await readFile(generated.private_key_path);
  const pub = JSON.parse(await readFile(generated.public_key_path));
  pub.e = 'Aw';
  await writeFile(generated.public_key_path, JSON.stringify(pub));
  await assert.rejects(ensureIntakeKeys({ archiveRoot: root, apply: true }), { code: 'keypair_mismatch_or_invalid' });
  assert.deepEqual(await readFile(generated.private_key_path), existingPrivate);
  await assert.rejects(verifyPair({ kty: 'RSA' }, { kty: 'RSA' }), { code: 'keypair_mismatch_or_invalid' });
}));

test('key setup refuses the Actions environment', () => temporary(async (root) => {
  const previous = process.env.GITHUB_ACTIONS;
  process.env.GITHUB_ACTIONS = 'true';
  try { await assert.rejects(ensureIntakeKeys({ archiveRoot: root, apply: true }), { code: 'key_setup_forbidden_in_actions' }); }
  finally {
    if (previous === undefined) delete process.env.GITHUB_ACTIONS;
    else process.env.GITHUB_ACTIONS = previous;
  }
  assert.deepEqual(await readdir(root), []);
}));
