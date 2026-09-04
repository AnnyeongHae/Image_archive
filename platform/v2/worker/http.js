export class ApiError extends Error {
  constructor(code, status = 503) { super(code); this.code = code; this.status = status; }
}

export const PRIVATE_HEADERS = {
  "Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
  "X-Robots-Tag": "noindex, nofollow, noarchive",
  "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
};

export function json(value, status = 200) {
  return Response.json(value, { status, headers: PRIVATE_HEADERS });
}

export async function readJsonBounded(response, maxBytes = 1048576) {
  const declared = response.headers.get("content-length");
  if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > maxBytes)) {
    await response.body?.cancel();
    throw new ApiError("response_too_large", 502);
  }
  if (!response.body) throw new ApiError("empty_response", 502);
  const reader = response.body.getReader(); const chunks = []; let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > maxBytes) throw new ApiError("response_too_large", 502);
      chunks.push(value);
    }
  } catch (error) { await reader.cancel().catch(() => {}); throw error; }
  finally { reader.releaseLock(); }
  const data = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { data.set(chunk, offset); offset += chunk.byteLength; }
  try { return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(data)); }
  catch { throw new ApiError("invalid_json_response", 502); }
}

export async function fetchJson(url, init = {}, { timeout = 8000, maxBytes = 1048576 } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    // workerd supports follow/manual, not error. Never follow a redirect with
    // provider credentials: manual returns 3xx, rejected by the !ok gate below.
    const response = await fetch(url, { ...init, signal: controller.signal, redirect: "manual" });
    if (!response.ok) { await response.body?.cancel(); throw new ApiError("upstream_unavailable", 503); }
    return await readJsonBounded(response, maxBytes);
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError("upstream_unavailable", 503);
  } finally { clearTimeout(timer); }
}

export function finiteVector(value, dimension) {
  if (!Array.isArray(value) || value.length !== dimension || !value.every(Number.isFinite)
      || !value.some(number => number !== 0)) throw new ApiError("vector_contract_mismatch", 503);
  return value;
}

export async function sha256(value) {
  return [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)))]
    .map(byte => byte.toString(16).padStart(2, "0")).join("");
}
