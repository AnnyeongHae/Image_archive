"""Source-neutral private intake records and a literal upstream-gallery adapter.

This is not canonicalization, rights clearance, image approval or LLM analysis.
Fenced prompt interiors are retained exactly, including their final newline.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
from urllib.parse import quote, unquote, urlsplit

SCHEMA = "archive-local-intake-1"
ADAPTER = "freestylefly-gallery-fences-1"
SUPPORTED_REPOSITORY = "freestylefly/awesome-gpt-image-2"
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
SHA1 = re.compile(r"[a-f0-9]{40}\Z")
ANCHOR = re.compile(r'<a\s+(?:name|id)=["\'](case-\d+)["\']\s*>\s*</a>', re.I)
FENCE = re.compile(r"(?m)^```(?:text|plaintext)?[ \t]*\r?\n(.*?)^```[ \t]*(?:\r?\n|$)", re.S)
IMAGE = re.compile(r"!\[[^\]\r\n]*\]\(([^\s)]+)(?:[ \t]+[^)]*)?\)")
POLICY = {"rights_status": "unknown", "rights_tier": "P3", "portfolio_visibility": "admin_only",
          "image_approved": False, "metadata_human_approved": False, "release_eligible": False}


def encode(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def content_identity(record: dict) -> str:
    return sha256(encode({key: value for key, value in record.items() if key not in {"observed_at", "content_sha256"}}))


def validate_envelope(record: dict) -> dict:
    if not isinstance(record, dict) or record.get("schema_version") != SCHEMA:
        raise ValueError("invalid intake schema")
    for key in ("source_id", "source_item_id", "source_url", "title", "observed_at"):
        if not isinstance(record.get(key), str) or not record[key] or len(record[key]) > 4096:
            raise ValueError(f"invalid intake {key}")
    if record.get("rights") != POLICY:
        raise ValueError("intake cannot grant rights or approvals")
    prompt = record.get("original_prompt")
    if (not isinstance(prompt, dict) or set(prompt) != {"text", "sha256", "status"}
            or prompt.get("status") != "exact_source_fence" or not isinstance(prompt.get("text"), str)
            or not prompt["text"].strip() or len(prompt["text"].encode("utf-8")) > 1_000_000
            or sha256(prompt["text"].encode("utf-8")) != prompt.get("sha256")):
        raise ValueError("original prompt hash or literal-source contract mismatch")
    version = record.get("source_version")
    if not isinstance(version, dict) or not version:
        raise ValueError("source version is required")
    if not isinstance(record.get("media_refs"), list):
        raise ValueError("media references must be a list")
    for media in record["media_refs"]:
        if (not isinstance(media, dict) or media.get("binary_downloaded") is not False
                or not isinstance(media.get("url"), str) or urlsplit(media["url"]).scheme != "https"):
            raise ValueError("intake contains invalid media reference")
    if record.get("content_sha256") != content_identity(record):
        raise ValueError("intake content hash mismatch")
    return record


def supported_container(repository: str, path: str) -> bool:
    # gallery.md is an index in the reviewed upstream snapshot, not an item
    # container. Keep it deferred rather than failing an otherwise valid batch.
    return repository == SUPPORTED_REPOSITORY and bool(re.fullmatch(r"docs/gallery-part-\d+\.md", path))


def _media_refs(block: str, *, path: str, repository: str, commit: str, media_tree: dict) -> tuple[list, list]:
    refs, deferred = [], []
    for url in IMAGE.findall(block):
        # External links are evidence only and are never followed or handed to
        # the downloader as approved destinations.
        parsed = urlsplit(url)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            deferred.append({"reason": "external_or_query_image_not_fetched", "url_sha256": sha256(url.encode())})
            continue
        relative = unquote(parsed.path)
        target = posixpath.normpath(posixpath.join(posixpath.dirname(path), relative))
        if relative.startswith(("/", "\\")) or "\\" in target or target.startswith("../"):
            deferred.append({"reason": "unsafe_image_path", "url_sha256": sha256(url.encode())})
            continue
        row = media_tree.get(target)
        if not row or row.get("mode") not in {"100644", "100755"} or not SHA1.fullmatch(str(row.get("sha", ""))):
            deferred.append({"reason": "image_not_regular_allowlisted_blob", "url_sha256": sha256(url.encode())})
            continue
        ref = {"path": target, "git_blob_sha1": row["sha"],
               "url": f"https://raw.githubusercontent.com/{repository}/{commit}/{quote(target, safe='/')}",
               "binary_downloaded": False, "rights_status": "unknown"}
        if ref not in refs:
            refs.append(ref)
    return refs, deferred


def parse_gallery(raw: bytes, *, source: dict, path: str, commit: str, tree_sha: str,
                  blob_sha: str, media_tree: dict, observed_at: str) -> dict:
    repository = source["repository"]
    if not supported_container(repository, path):
        raise ValueError("unsupported container; leave unprocessed")
    if not all(SHA1.fullmatch(value) for value in (commit, tree_sha, blob_sha)):
        raise ValueError("pinned Git identities required")
    if hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest() != blob_sha:
        raise ValueError("source bytes do not match pinned Git blob")
    # decode, not read_text(): universal-newline conversion would alter prompts.
    body = raw.decode("utf-8-sig", errors="strict")
    anchors = list(ANCHOR.finditer(body))
    if not 1 <= len(anchors) <= 2000:
        raise ValueError("gallery anchor count outside supported bounds")
    if len({match.group(1) for match in anchors}) != len(anchors):
        raise ValueError("ambiguous duplicate gallery anchors")
    records, deferred = [], []
    for ordinal, anchor in enumerate(anchors):
        block = body[anchor.end():anchors[ordinal + 1].start() if ordinal + 1 < len(anchors) else len(body)]
        fences = list(FENCE.finditer(block))
        if len(fences) != 1 or not fences[0].group(1).strip():
            deferred.append({"source_item_id": f"{path}#{anchor.group(1)}", "reason": "one_nonblank_prompt_fence_required"})
            continue
        prompt = fences[0].group(1)
        heading = re.search(r"(?m)^###\s+([^\r\n]+)", block)
        outside_prompt = block[:fences[0].start()] + block[fences[0].end():]
        media, omitted = _media_refs(outside_prompt, path=path, repository=repository, commit=commit, media_tree=media_tree)
        record = {"schema_version": SCHEMA, "source_id": source["source_id"],
                  "source_item_id": f"{path}#{anchor.group(1)}",
                  "source_version": {"repository": repository, "repository_commit_sha": commit,
                     "repository_tree_sha": tree_sha, "git_blob_sha1": blob_sha, "adapter_version": ADAPTER},
                  "source_url": f"https://github.com/{repository}/blob/{commit}/{path}#{anchor.group(1)}",
                  "title": heading.group(1) if heading else anchor.group(1), "observed_at": observed_at,
                  "original_prompt": {"text": prompt, "sha256": sha256(prompt.encode("utf-8")), "status": "exact_source_fence"},
                  "media_refs": media, "deferred_media": omitted, "rights": dict(POLICY),
                  "source_container_sha256": sha256(raw)}
        record["content_sha256"] = content_identity(record)
        records.append(validate_envelope(record))
    return {"records": records, "deferred": deferred, "complete": not deferred,
            "source_container_sha256": sha256(raw), "adapter_version": ADAPTER}
