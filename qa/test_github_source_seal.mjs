import { test, before } from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { mkdtemp, writeFile, readFile, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { sealBytes, unsealBytes, validatePublicKey } from '../src/github_sources/seal_intake.mjs';

let publicKey, privateKey;
before(async () => {
  const pair = await webcrypto.subtle.generateKey({ name: 'RSA-OAEP', modulusLength: 2048,
    publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' }, true, ['encrypt', 'decrypt']);
  publicKey = await webcrypto.subtle.exportKey('jwk', pair.publicKey);
  privateKey = await webcrypto.subtle.exportKey('jwk', pair.privateKey);
});

test('exact plaintext roundtrip without visible prompt text', async () => {
  const bytes = Buffer.from('Original 한글 prompt\r\nwith spaces  \r\n');
  const envelope = await sealBytes(bytes, publicKey);
  assert.equal(JSON.stringify(envelope).includes('Original'), false);
  assert.deepEqual(await unsealBytes(envelope, privateKey), bytes);
  const another = await sealBytes(bytes, publicKey);
  assert.notEqual(envelope.ciphertext, another.ciphertext);
  assert.notEqual(envelope.iv, another.iv);
});

test('private material is rejected from the tracked public key', async () => {
  await assert.rejects(validatePublicKey(privateKey));
  await assert.rejects(validatePublicKey({ kty: 'oct', k: 'secret' }));
});

test('ciphertext and authenticated header tampering fail closed', async () => {
  const envelope = await sealBytes(Buffer.from('private prompt'), publicKey);
  await assert.rejects(unsealBytes({ ...envelope, plaintext_sha256: '0'.repeat(64) }, privateKey));
  await assert.rejects(unsealBytes({ ...envelope, ciphertext: envelope.ciphertext.slice(0, -2) + 'AA' }, privateKey));
  await assert.rejects(unsealBytes({ ...envelope, unexpected: 'field' }, privateKey));
});

test('wrong recipient cannot decrypt', async () => {
  const envelope = await sealBytes(Buffer.from('private prompt'), publicKey);
  await assert.rejects(unsealBytes(envelope, { ...privateKey, n: publicKey.n.slice(0, -2) + 'AA' }));
});

test('CLI streams plaintext and writes ciphertext only', async () => {
  const directory = await mkdtemp(path.join(tmpdir(), 'archive-seal-test-'));
  try {
    const keyPath = path.join(directory, 'public.json');
    const output = path.join(directory, 'intake.sealed.json');
    const script = fileURLToPath(new URL('../src/github_sources/seal_intake.mjs', import.meta.url));
    await writeFile(keyPath, JSON.stringify(publicKey));
    const plaintext = Buffer.from('{"private_prompt":"Exact CLI 한글"}');
    const result = spawnSync(process.execPath, [script, 'seal', '--public-key', keyPath, '--output', output], { input: plaintext });
    assert.equal(result.status, 0);
    assert.equal(result.stdout.includes('Exact'), false);
    assert.equal(result.stderr.length, 0);
    assert.deepEqual((await readdir(directory)).sort(), ['intake.sealed.json', 'public.json']);
    assert.deepEqual(await unsealBytes(JSON.parse(await readFile(output)), privateKey), plaintext);
    const again = spawnSync(process.execPath, [script, 'seal', '--public-key', keyPath, '--output', output], { input: plaintext });
    assert.equal(again.status, 2);
  } finally {
    await rm(directory, { recursive: true, force: true }); // exact test-owned temporary directory
  }
});

test('CLI refuses local unseal inside Actions before reading a key', () => {
  const script = fileURLToPath(new URL('../src/github_sources/seal_intake.mjs', import.meta.url));
  const result = spawnSync(process.execPath, [script, 'unseal', '--private-key', 'not-read.json',
    '--input', 'not-read.sealed.json', '--output', 'not-created.json'], { env: { ...process.env, GITHUB_ACTIONS: 'true' } });
  assert.equal(result.status, 2);
  assert.equal(result.stdout.length, 0);
  assert.equal(result.stderr.toString(), 'sealed_intake_operation_failed\n');
});
