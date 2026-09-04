import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { performance } from "node:perf_hooks";

const require = createRequire(import.meta.url);
const scriptDir = dirname(fileURLToPath(import.meta.url));
const root = resolve(scriptDir, "..");
const canonicalPath = join(root, "data", "canonical", "featured_five.json");
const outputDir = join(root, "data", "private-research", "media-benchmarks", "modern-current");
const outputPath = join(outputDir, "modern_format_benchmark.json");

function loadSharp() {
  const explicit = process.env.IMAGE_ARCHIVE_SHARP_ROOT?.trim();
  if (explicit) return require(explicit);
  return require("sharp");
}

function sha256(buffer) {
  return createHash("sha256").update(buffer).digest("hex");
}

async function atomicWrite(path, content) {
  await mkdir(dirname(path), { recursive: true });
  const temporary = `${path}.tmp`;
  await writeFile(temporary, content);
  await rename(temporary, path);
}

function supported(format) {
  const entry = format ?? {};
  return Boolean(entry.output?.buffer || entry.output?.file);
}

async function encodeAndVerify(sharp, source, variant) {
  const started = performance.now();
  let pipeline = sharp(source, { failOn: "error" }).rotate();
  if (variant.name === "avif_q55") {
    pipeline = pipeline.avif({ quality: 55, effort: 4, chromaSubsampling: "4:4:4" });
  } else if (variant.name === "webp_q82") {
    pipeline = pipeline.webp({ quality: 82, effort: 6, smartSubsample: true });
  } else if (variant.name === "jpeg_q82") {
    pipeline = pipeline.jpeg({ quality: 82, progressive: true, mozjpeg: true, chromaSubsampling: "4:4:4" });
  } else {
    throw new Error(`unsupported variant: ${variant.name}`);
  }
  const encoded = await pipeline.toBuffer();
  const encodeMs = performance.now() - started;
  const decodeStarted = performance.now();
  const metadata = await sharp(encoded, { failOn: "error" }).metadata();
  const decodeMs = performance.now() - decodeStarted;
  return {
    variant: variant.name,
    mime_type: variant.mimeType,
    bytes: encoded.length,
    sha256: sha256(encoded),
    width: metadata.width ?? null,
    height: metadata.height ?? null,
    has_alpha: Boolean(metadata.hasAlpha),
    encode_ms: Number(encodeMs.toFixed(3)),
    decode_probe_ms: Number(decodeMs.toFixed(3)),
    payload: encoded,
  };
}

async function main() {
  const apply = process.argv.includes("--apply");
  const sharp = loadSharp();
  const formatSupport = {
    avif: supported(sharp.format.heif),
    webp: supported(sharp.format.webp),
    jpeg: supported(sharp.format.jpeg),
    jxl: supported(sharp.format.jxl),
  };
  const variants = [
    ...(formatSupport.avif ? [{ name: "avif_q55", mimeType: "image/avif", suffix: ".avif" }] : []),
    ...(formatSupport.webp ? [{ name: "webp_q82", mimeType: "image/webp", suffix: ".webp" }] : []),
    ...(formatSupport.jpeg ? [{ name: "jpeg_q82", mimeType: "image/jpeg", suffix: ".jpg" }] : []),
  ];
  if (!variants.length) throw new Error("no supported output encoders");

  const canonical = JSON.parse(await readFile(canonicalPath, "utf8"));
  const rows = [];
  const totals = Object.fromEntries(variants.map((variant) => [variant.name, { bytes: 0, encode_ms: 0, decode_probe_ms: 0 }]));
  let sourceTotal = 0;

  for (const item of canonical.items ?? []) {
    const sourcePath = join(root, item.image_path);
    const source = await readFile(sourcePath);
    const sourceMetadata = await sharp(source, { failOn: "error" }).metadata();
    sourceTotal += source.length;
    const encodedRows = [];
    for (const variant of variants) {
      const result = await encodeAndVerify(sharp, source, variant);
      const samplePath = join(outputDir, "samples", item.reference_style_id, `${variant.name}${variant.suffix}`);
      if (apply) await atomicWrite(samplePath, result.payload);
      totals[variant.name].bytes += result.bytes;
      totals[variant.name].encode_ms += result.encode_ms;
      totals[variant.name].decode_probe_ms += result.decode_probe_ms;
      encodedRows.push({
        ...result,
        payload: undefined,
        savings_bytes: source.length - result.bytes,
        savings_pct: Number((((source.length - result.bytes) / source.length) * 100).toFixed(2)),
        sample_path: apply ? samplePath.slice(root.length + 1).replaceAll("\\", "/") : null,
      });
    }
    rows.push({
      reference_style_id: item.reference_style_id,
      source_path: item.image_path,
      source_file: basename(sourcePath),
      source_bytes: source.length,
      source_width: sourceMetadata.width ?? null,
      source_height: sourceMetadata.height ?? null,
      source_has_alpha: Boolean(sourceMetadata.hasAlpha),
      variants: encodedRows,
    });
  }

  const aggregate = Object.fromEntries(Object.entries(totals).map(([name, value]) => [name, {
    total_bytes: value.bytes,
    savings_bytes: sourceTotal - value.bytes,
    savings_pct: Number((((sourceTotal - value.bytes) / sourceTotal) * 100).toFixed(2)),
    total_encode_ms: Number(value.encode_ms.toFixed(3)),
    total_decode_probe_ms: Number(value.decode_probe_ms.toFixed(3)),
  }]));
  const report = {
    schema_version: "modern-image-format-benchmark-1.0",
    generated_at: new Date().toISOString(),
    scope: "featured_five_only",
    quality_note: "Encoder quality values are not perceptually equivalent across codecs; byte comparisons require human visual review before a release decision.",
    runtime: {
      sharp: sharp.versions.sharp,
      vips: sharp.versions.vips,
      aom: sharp.versions.aom ?? null,
      heif: sharp.versions.heif ?? null,
      webp: sharp.versions.webp ?? null,
      mozjpeg: sharp.versions.mozjpeg ?? null,
    },
    format_support: formatSupport,
    source_total_bytes: sourceTotal,
    aggregate,
    records: rows,
  };
  const rendered = `${JSON.stringify(report, null, 2)}\n`;
  if (apply) {
    await mkdir(outputDir, { recursive: true });
    await atomicWrite(outputPath, rendered);
  }
  process.stdout.write(rendered);
}

main().catch(async (error) => {
  try {
    await rm(`${outputPath}.tmp`, { force: true });
  } catch {}
  process.stderr.write(`${error.stack ?? error}\n`);
  process.exitCode = 1;
});
