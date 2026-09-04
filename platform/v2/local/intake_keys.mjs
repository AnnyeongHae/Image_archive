/** Local-only RSA recipient setup. Dry-run by default; never overwrite keys. */
import { webcrypto } from 'node:crypto';
import { open, mkdir, lstat, stat, readFile, realpath, chmod } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { validatePublicKey } from '../../../src/github_sources/seal_intake.mjs';

const { subtle } = webcrypto;
const PUBLIC = 'config/intake-recipient.public.jwk.json';
const PRIVATE = 'data/private-research/platform-v2/secrets/intake-recipient.private.jwk.json';
const DEFAULT_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const RSA = { name: 'RSA-OAEP', modulusLength: 3072,
  publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' };

export class KeySetupError extends Error {
  constructor(code) { super(code); this.code = code; }
}

async function inspectTarget(root, relative) {
  const target = path.resolve(root, relative);
  const relation = path.relative(root, target);
  if (!relation || relation.startsWith('..') || path.isAbsolute(relation)) throw new KeySetupError('unsafe_key_path');
  let current = root;
  const parts = relation.split(path.sep);
  for (let i = 0; i < parts.length; i++) {
    current = path.join(current, parts[i]);
    try {
      const info = await lstat(current);
      if (info.isSymbolicLink() || (i < parts.length - 1 && !info.isDirectory())
          || (i === parts.length - 1 && (!info.isFile() || info.nlink > 1))) throw new KeySetupError('unsafe_key_path');
    } catch (error) {
      if (error.code === 'ENOENT') return { target, exists: false };
      throw error;
    }
  }
  return { target, exists: true };
}

async function restrictPrivate(target) {
  // Windows uses the existing ignored local-secret-store ACL boundary. Do not
  // change managed workspace ACLs or represent chmod as an owner-only ACL.
  if (process.platform !== 'win32') await chmod(target, 0o600);
  return verifyPrivatePermissions(target);
}

async function verifyPrivatePermissions(target) {
  if (process.platform === 'win32') {
    return 'inherited_workspace_not_hardened';
  }
  const info = await stat(target);
  if ((info.mode & 0o077) !== 0 || (info.mode & 0o600) !== 0o600) throw new KeySetupError('private_permissions_not_owner_only');
  return 'posix_0600';
}

async function readJwk(target, maxBytes) {
  if ((await stat(target)).size > maxBytes) throw new KeySetupError('key_file_size_invalid');
  try { return JSON.parse(await readFile(target, 'utf8')); }
  catch { throw new KeySetupError('key_file_invalid'); }
}

export async function verifyPair(publicJwk, privateJwk) {
  try {
    const { key: publicKey, recipient_key_sha256 } = await validatePublicKey(publicJwk);
    if (!privateJwk || publicJwk.n !== privateJwk.n || publicJwk.e !== privateJwk.e
        || publicKey.algorithm.modulusLength !== RSA.modulusLength) throw new Error('mismatch');
    const privateKey = await subtle.importKey('jwk', privateJwk,
      { name: 'RSA-OAEP', hash: 'SHA-256' }, false, ['decrypt']);
    const challenge = webcrypto.getRandomValues(new Uint8Array(32));
    const encrypted = await subtle.encrypt('RSA-OAEP', publicKey, challenge);
    const decrypted = new Uint8Array(await subtle.decrypt('RSA-OAEP', privateKey, encrypted));
    if (decrypted.length !== challenge.length || decrypted.some((value, index) => value !== challenge[index])) throw new Error('mismatch');
    return recipient_key_sha256;
  } catch {
    throw new KeySetupError('keypair_mismatch_or_invalid');
  }
}

async function exclusiveWrite(target, bytes, { privateFile = false } = {}) {
  let handle;
  try {
    handle = await open(target, 'wx', privateFile ? 0o600 : 0o644);
    // POSIX open() creates it as 0600 before any secret bytes exist. Windows
    // inherits the existing workspace ACL; that caveat is explicit in receipts.
    if (privateFile) await restrictPrivate(target);
    await handle.writeFile(bytes);
    await handle.sync();
  } finally {
    if (handle) await handle.close();
  }
}

export async function ensureIntakeKeys({ archiveRoot = DEFAULT_ROOT, apply = false } = {}) {
  if (process.env.GITHUB_ACTIONS === 'true') throw new KeySetupError('key_setup_forbidden_in_actions');
  if (typeof apply !== 'boolean') throw new KeySetupError('explicit_apply_boolean_required');
  const root = await realpath(path.resolve(archiveRoot));
  const pub = await inspectTarget(root, PUBLIC);
  const secret = await inspectTarget(root, PRIVATE);
  const summary = { public_key_path: pub.target, private_key_path: secret.target };
  if (pub.exists !== secret.exists) throw new KeySetupError('partial_keypair_preserved_manual_review_required');
  if (pub.exists) {
    const recipient_key_sha256 = await verifyPair(await readJwk(pub.target, 16 * 1024), await readJwk(secret.target, 32 * 1024));
    const private_permissions = await verifyPrivatePermissions(secret.target);
    return { status: 'verified_existing', ...summary, recipient_key_sha256, private_permissions,
      acl: private_permissions, writes: 0 };
  }
  if (!apply) return { status: 'dry_run_would_generate_rsa_oaep_sha256_3072', ...summary,
    recipient_key_sha256: null, writes: 0 };

  const pair = await subtle.generateKey(RSA, true, ['encrypt', 'decrypt']);
  const publicJwk = await subtle.exportKey('jwk', pair.publicKey);
  const privateJwk = await subtle.exportKey('jwk', pair.privateKey);
  const recipient_key_sha256 = await verifyPair(publicJwk, privateJwk);
  const secretBytes = Buffer.from(JSON.stringify(privateJwk) + '\n');
  try {
    // Recheck after generation, which may have taken time. No repair, overwrite
    // or deletion is attempted if another worker created either target.
    if ((await inspectTarget(root, PUBLIC)).exists || (await inspectTarget(root, PRIVATE)).exists)
      throw new KeySetupError('concurrent_key_creation_preserved');
    await mkdir(path.dirname(pub.target), { recursive: true });
    await mkdir(path.dirname(secret.target), { recursive: true, mode: 0o700 });
    await inspectTarget(root, PUBLIC);
    await inspectTarget(root, PRIVATE);
    await exclusiveWrite(secret.target, secretBytes, { privateFile: true });
    await exclusiveWrite(pub.target, Buffer.from(JSON.stringify(publicJwk, null, 2) + '\n'));
  } finally {
    secretBytes.fill(0);
  }
  const verified = await ensureIntakeKeys({ archiveRoot: root, apply: false });
  if (verified.recipient_key_sha256 !== recipient_key_sha256) throw new KeySetupError('post_write_keypair_changed');
  return { ...verified, status: 'generated_and_verified', writes: 2 };
}

async function main(args) {
  if (args.length > 1 || (args.length === 1 && args[0] !== '--apply')) throw new KeySetupError('only_explicit_apply_supported');
  process.stdout.write(JSON.stringify(await ensureIntakeKeys({ apply: args[0] === '--apply' })) + '\n');
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).catch((error) => {
    process.stderr.write(JSON.stringify({ status: 'blocked', reason: error instanceof KeySetupError ? error.code : 'key_setup_failed_no_key_material_logged' }) + '\n');
    process.exitCode = 2;
  });
}
