/** Built-in WebCrypto sealed transport. No private key is required in Actions. */
import { webcrypto, createHash } from 'node:crypto';
import { readFile, writeFile, mkdir, stat, lstat, realpath } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const { subtle } = webcrypto;
const MAX_BYTES = 128 * 1024 * 1024;
const SCHEMA = 'archive-sealed-intake-1';
const ALGORITHM = 'RSA-OAEP-256+A256GCM';
const b64 = (bytes) => Buffer.from(bytes).toString('base64url');
const sha = (bytes) => createHash('sha256').update(bytes).digest('hex');
const json = (value) => Buffer.from(JSON.stringify(value), 'utf8');
const PRIVATE_PARTS = ['d', 'p', 'q', 'dp', 'dq', 'qi', 'oth'];

function publicIdentity(jwk) {
  if (!jwk || jwk.kty !== 'RSA' || !/^[A-Za-z0-9_-]+$/.test(jwk.n ?? '')
      || !/^[A-Za-z0-9_-]+$/.test(jwk.e ?? '') || Buffer.from(jwk.n, 'base64url').length < 256
      || (jwk.alg && jwk.alg !== 'RSA-OAEP-256')) throw new Error('invalid_recipient_key');
  return { e: jwk.e, kty: 'RSA', n: jwk.n };
}

export async function validatePublicKey(jwk) {
  if (PRIVATE_PARTS.some((key) => key in jwk)) throw new Error('private_key_forbidden_in_public_config');
  const identity = publicIdentity(jwk);
  const key = await subtle.importKey('jwk', { ...identity, alg: 'RSA-OAEP-256', ext: true },
    { name: 'RSA-OAEP', hash: 'SHA-256' }, false, ['encrypt']);
  return { key, recipient_key_sha256: sha(json(identity)) };
}

export async function sealBytes(plaintext, publicJwk) {
  const bytes = Buffer.from(plaintext);
  if (!bytes.length || bytes.length > MAX_BYTES) throw new Error('plaintext_size_out_of_bounds');
  const { key: recipient, recipient_key_sha256 } = await validatePublicKey(publicJwk);
  const aesBytes = webcrypto.getRandomValues(new Uint8Array(32));
  const iv = webcrypto.getRandomValues(new Uint8Array(12));
  const aes = await subtle.importKey('raw', aesBytes, 'AES-GCM', false, ['encrypt']);
  const header = { schema_version: SCHEMA, algorithm: ALGORITHM, recipient_key_sha256,
    plaintext_sha256: sha(bytes), plaintext_bytes: bytes.length };
  const ciphertext = await subtle.encrypt({ name: 'AES-GCM', iv, additionalData: json(header), tagLength: 128 }, aes, bytes);
  const wrapped = await subtle.encrypt({ name: 'RSA-OAEP' }, recipient, aesBytes);
  aesBytes.fill(0);
  return { ...header, iv: b64(iv), wrapped_key: b64(wrapped), ciphertext: b64(ciphertext),
    ciphertext_sha256: sha(Buffer.from(ciphertext)) };
}

export async function unsealBytes(envelope, privateJwk) {
  const expected = ['schema_version', 'algorithm', 'recipient_key_sha256', 'plaintext_sha256',
    'plaintext_bytes', 'iv', 'wrapped_key', 'ciphertext', 'ciphertext_sha256'].sort();
  if (!envelope || JSON.stringify(Object.keys(envelope).sort()) !== JSON.stringify(expected)
      || envelope.schema_version !== SCHEMA || envelope.algorithm !== ALGORITHM
      || !Number.isSafeInteger(envelope.plaintext_bytes) || envelope.plaintext_bytes < 1
      || envelope.plaintext_bytes > MAX_BYTES) throw new Error('invalid_sealed_envelope');
  const identity = publicIdentity(privateJwk);
  if (sha(json(identity)) !== envelope.recipient_key_sha256) throw new Error('recipient_identity_mismatch');
  const decoded = {};
  for (const key of ['iv', 'wrapped_key', 'ciphertext']) {
    if (typeof envelope[key] !== 'string' || !/^[A-Za-z0-9_-]+$/.test(envelope[key])) throw new Error('invalid_base64url');
    decoded[key] = Buffer.from(envelope[key], 'base64url');
    if (b64(decoded[key]) !== envelope[key]) throw new Error('noncanonical_base64url');
  }
  if (decoded.iv.length !== 12 || decoded.ciphertext.length !== envelope.plaintext_bytes + 16
      || sha(decoded.ciphertext) !== envelope.ciphertext_sha256) throw new Error('ciphertext_integrity_mismatch');
  const recipient = await subtle.importKey('jwk', privateJwk, { name: 'RSA-OAEP', hash: 'SHA-256' }, false, ['decrypt']);
  const raw = await subtle.decrypt('RSA-OAEP', recipient, decoded.wrapped_key);
  if (raw.byteLength !== 32) throw new Error('invalid_wrapped_key');
  const aes = await subtle.importKey('raw', raw, 'AES-GCM', false, ['decrypt']);
  const header = Object.fromEntries(['schema_version', 'algorithm', 'recipient_key_sha256', 'plaintext_sha256', 'plaintext_bytes']
    .map((key) => [key, envelope[key]]));
  const plaintext = Buffer.from(await subtle.decrypt({ name: 'AES-GCM', iv: decoded.iv,
    additionalData: json(header), tagLength: 128 }, aes, decoded.ciphertext));
  if (sha(plaintext) !== envelope.plaintext_sha256) throw new Error('plaintext_integrity_mismatch');
  return plaintext;
}

async function boundedRead(filename, maximum = MAX_BYTES * 2) {
  if ((await stat(filename)).size > maximum) throw new Error('file_size_out_of_bounds');
  return readFile(filename);
}

async function rejectPrivateSymlinks(privateRoot, candidate) {
  let current = candidate;
  while (current !== path.dirname(path.dirname(privateRoot))) {
    try {
      if ((await lstat(current)).isSymbolicLink()) throw new Error('private_symlink_forbidden');
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
    if (current === path.dirname(current)) throw new Error('private_path_required');
    current = path.dirname(current);
  }
}

async function main(args) {
  const [mode, ...rest] = args;
  const options = {};
  for (let i = 0; i < rest.length; i += 2) {
    if (!rest[i]?.startsWith('--') || !rest[i + 1] || options[rest[i]]) throw new Error('invalid_arguments');
    options[rest[i]] = rest[i + 1];
  }
  if (mode === 'verify') {
    const validated = await validatePublicKey(JSON.parse(await boundedRead(options['--public-key'], 16 * 1024)));
    process.stdout.write(JSON.stringify({ ok: true, recipient_key_sha256: validated.recipient_key_sha256 }) + '\n');
    return;
  }
  if (mode === 'seal') {
    const key = JSON.parse(await boundedRead(options['--public-key'], 16 * 1024));
    await validatePublicKey(key); // fail before reading prompt-bearing stdin
    const chunks = [];
    let count = 0;
    for await (const chunk of process.stdin) {
      count += chunk.length;
      if (count > MAX_BYTES) throw new Error('plaintext_size_out_of_bounds');
      chunks.push(chunk);
    }
    const envelope = await sealBytes(Buffer.concat(chunks), key);
    await mkdir(path.dirname(options['--output']), { recursive: true });
    await writeFile(options['--output'], json(envelope), { flag: 'wx', mode: 0o600 });
    process.stdout.write(JSON.stringify({ ok: true, recipient_key_sha256: envelope.recipient_key_sha256,
      ciphertext_sha256: envelope.ciphertext_sha256, plaintext_sha256: envelope.plaintext_sha256 }) + '\n');
    return;
  }
  if (mode === 'unseal') {
    if (process.env.GITHUB_ACTIONS === 'true') throw new Error('unseal_forbidden_in_actions');
    const archiveRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
    const privateRoot = path.join(archiveRoot, 'data', 'private-research');
    const output = path.resolve(options['--output']);
    const keyPath = path.resolve(options['--private-key']);
    for (const candidate of [output, keyPath]) {
      const relative = path.relative(privateRoot, candidate);
      if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) throw new Error('private_path_required');
      await rejectPrivateSymlinks(privateRoot, candidate);
    }
    if (!path.relative(await realpath(privateRoot), await realpath(keyPath))
        || path.relative(await realpath(privateRoot), await realpath(keyPath)).startsWith('..')) throw new Error('private_path_required');
    const envelope = JSON.parse(await boundedRead(options['--input']));
    const key = JSON.parse(await boundedRead(keyPath, 32 * 1024));
    const plaintext = await unsealBytes(envelope, key);
    await mkdir(path.dirname(output), { recursive: true });
    await writeFile(output, plaintext, { flag: 'wx', mode: 0o600 });
    process.stdout.write(JSON.stringify({ ok: true, plaintext_sha256: sha(plaintext) }) + '\n');
    return;
  }
  throw new Error('mode_must_be_verify_seal_or_unseal');
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).catch(() => {
    // Never echo exception objects, input contents, keys or response bodies.
    process.stderr.write('sealed_intake_operation_failed\n');
    process.exitCode = 2;
  });
}
