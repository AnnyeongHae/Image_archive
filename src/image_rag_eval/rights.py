"""Conservative rights notices, not a rights clearance or publication gate.

An explicitly human-verified ``image_rights_evidence`` may establish an image
license notice only when it binds this exact original image SHA, a separately
hashed evidence document and a public evidence URL. Repository licensing,
generic clearance booleans, uploader names and human similarity approvals do
not qualify. This pure normalizer never fetches or verifies a remote document.
"""
from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

SCHEMA = "image-rights-notice-1"
EVIDENCE_SCHEMA = "image-license-evidence-1"
SHA256 = re.compile(r"^[a-f0-9]{64}$")
RESTRICTED = {"blocked", "prohibited", "redistribution_prohibited", "rights_conflict", "takedown_requested",
              "restricted", "all_rights_reserved", "private_reference_only", "noncommercial_only"}
BAD_LICENSES = {"", "unknown", "unverified", "noassertion", "not_verified", "license_unverified", "none",
                "라이선스 미확인"}
BADGES = {"unverified": "권리 미확인", "restricted": "이용 제한", "verified": "개별 이미지 근거 확인"}
BASE_NOTICE = "출처 표기는 이용 허락을 대신하지 않습니다. 공개·상업 사용 승인은 별도입니다."
MIT_NOTICE = ("MIT가 적용되는 자료의 복제본 또는 상당 부분에는 원문의 저작권 고지와 허가 고지를 함께 유지해야 합니다. "
              "단순 출처 표기로 대체할 수 없습니다.")


def _dict(value):
    return value if isinstance(value, dict) else {}


def _text(value, limit=300):
    if not isinstance(value, str):
        return ""
    return " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", value).split())[:limit]


def safe_source_url(value):
    """Public HTTP(S) links only; discard every query and unsafe fragment.

    No credentials, signed query parameters, local/loopback destinations or
    DNS lookups. Query removal is deliberately conservative, not a claim that
    the resulting page has been fetched or that a source has granted rights.
    """
    if (not isinstance(value, str) or not value or len(value) > 4096 or "\\" in value
            or re.search(r"[\s\x00-\x1f\x7f]", value)):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (parsed.scheme not in {"http", "https"} or not host or parsed.username is not None or parsed.password is not None
                or "%" in host or host.casefold().rstrip(".") in {"localhost", "localhost.localdomain"}
                or host.casefold().rstrip(".").endswith((".localhost", ".local", ".internal"))):
            return None
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            # Browsers can interpret noncanonical dotted/octal/hex IPv4 hosts.
            if re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)(?:\.(?:0[xX][0-9a-fA-F]+|[0-9]+))*\.?", host):
                return None
            if "." not in host or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
                                           for part in host.encode("idna").decode("ascii").rstrip(".").split(".")):
                return None
        # Preserve the public hostname spelling; never print user-info/query.
        netloc = ("[" + host + "]") if ":" in host else host
        if port is not None:
            netloc += ":" + str(port)
        fragment = parsed.fragment if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", parsed.fragment) else ""
        return urlunsplit((parsed.scheme, netloc, parsed.path or "", "", fragment))
    except (ValueError, UnicodeError):
        return None


def _image_sha(record):
    for value in (record.get("source_sha256"), record.get("sha256")):
        if isinstance(value, str) and SHA256.fullmatch(value):
            return value
    media = _dict(record.get("media"))
    assets = media.get("assets") if isinstance(media.get("assets"), list) else []
    index = record.get("asset_index", 0)
    if type(index) is int and 0 <= index < len(assets):
        value = _dict(assets[index]).get("sha256")
        if isinstance(value, str) and SHA256.fullmatch(value):
            return value
    return None


def _individual_evidence(record):
    rights = _dict(record.get("rights"))
    evidence = _dict(record.get("image_rights_evidence") or rights.get("individual_image_evidence"))
    expected_sha = _image_sha(record)
    url = safe_source_url(evidence.get("evidence_url"))
    stamp = evidence.get("reviewed_at")
    try:
        reviewed = datetime.fromisoformat(stamp.replace("Z", "+00:00")) if isinstance(stamp, str) else None
    except ValueError:
        reviewed = None
    if (evidence.get("schema_version") != EVIDENCE_SCHEMA or evidence.get("scope") != "image"
            or evidence.get("verification_status") != "verified" or evidence.get("human_verified") is not True
            or not expected_sha or evidence.get("image_sha256") != expected_sha
            or not isinstance(evidence.get("evidence_sha256"), str) or not SHA256.fullmatch(evidence["evidence_sha256"])
            or not _text(evidence.get("reviewed_by")) or not reviewed or reviewed.tzinfo is None
            or _text(evidence.get("license_label"), 160).casefold() in BAD_LICENSES | {"unlicensed", "all rights reserved", "all_rights_reserved"}
            or not url
            or urlsplit(evidence["evidence_url"]).query):
        return None
    return evidence


def normalize_image_rights(record) -> dict:
    """Return a warning for missing/uncleared rights, never infer permission."""
    record = _dict(record)
    source, rights = _dict(record.get("source")), _dict(record.get("rights"))
    previous = _dict(record.get("rights_display"))
    license_data = _dict(record.get("license") or record.get("source_license"))
    raw = _dict(_dict(record.get("provenance")).get("raw_source"))
    raw_source = _dict(raw.get("source"))
    source_name = _text(source.get("name") or record.get("source_name") or raw.get("source_name")
                        or raw_source.get("source_label") or previous.get("source_name")) or "출처 미확인"
    source_url = next((url for value in (source.get("url"), record.get("source_url"), source.get("pinned_url"),
                         raw.get("source_url"), raw_source.get("source_url"), raw_source.get("gallery_url_pinned"),
                         source.get("repository"), previous.get("source_url")) if (url := safe_source_url(value))), None)
    # These are explicitly named creator fields, not source/uploader/title guesses.
    creator = _text(record.get("creator_name") or source.get("creator_name") or raw.get("creator_name")
                    or previous.get("creator_name")) or None
    label = _text(license_data.get("effective_spdx") or license_data.get("detected_spdx")
                  or license_data.get("reported_spdx") or record.get("repository_license_spdx")
                  or (record.get("source_license") if isinstance(record.get("source_license"), str) else "")
                  or record.get("license_label") or previous.get("license_label"), 160)
    if label.casefold() in BAD_LICENSES:
        label = "라이선스 미확인"
    scope_text = _text(license_data.get("scope") or record.get("license_scope") or previous.get("license_scope")).casefold()
    license_url = safe_source_url(license_data.get("evidence_url"))
    repository = ("repo" in scope_text or "repository" in _text(license_data.get("status")).casefold()
                  or bool(record.get("repository_license_spdx")) or bool(source.get("repository"))
                  or bool(license_url and re.search(r"https?://(?:www\.)?github\.com/[^/]+/[^/]+/(?:blob/[^/]+/)?(?:LICENSE|COPYING)(?:\.|$)", license_url, re.I)))
    scope = "repository_only" if repository else "image" if scope_text in {"image", "individual_image"} else "unknown"
    statuses = {_text(record.get("rights_status")).casefold(), _text(rights.get("status")).casefold(),
                _text(previous.get("status")).casefold()}
    restricted = bool(statuses & RESTRICTED) or rights.get("rights_tier") == "P4" or label.casefold() == "unlicensed"
    proof = _individual_evidence(record)
    verified = proof is not None and not restricted
    if verified:
        label, scope = _text(proof["license_label"], 160), "image"
        creator = _text(proof.get("creator_name")) or creator
    status = "restricted" if restricted else "verified" if verified else "unverified"
    if restricted:
        notice = "이용 제한 또는 권리 충돌이 기록되어 있습니다. 권리자 확인 전 재사용·재배포를 진행하지 마세요."
    elif verified:
        notice = "이 이미지에 연결된 사람 검토·라이선스 증빙이 기록되어 있습니다. 증빙의 허용 범위와 조건을 확인하세요."
    else:
        notice = "개별 이미지의 저작권자·이용 허락을 확인하지 못했습니다. 원출처에서 별도 확인이 필요합니다."
    if scope == "repository_only":
        notice += " 저장소 라이선스는 제3자 이미지·프롬프트의 이용 허락을 보장하지 않습니다."
    if label.casefold() == "mit":
        notice += " " + MIT_NOTICE
    notice += " " + BASE_NOTICE
    evidence_urls = sorted({url for value in (license_url, proof.get("evidence_url") if proof else None)
                            if (url := safe_source_url(value))})
    return {"schema_version": SCHEMA, "status": status, "badge": BADGES[status], "source_name": source_name,
            "source_url": source_url, "creator_name": creator, "license_label": label, "license_scope": scope,
            "attribution_text": "출처: " + source_name + (" · 제작자: " + creator if creator else " · 개별 이미지 제작자 미확인"),
            "notice_text": notice, "image_license_verified": verified, "release_eligible": False,
            "evidence_urls": evidence_urls}


def _file_sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_rights_catalog(root: Path, spec: dict) -> dict[str, dict]:
    """Overlay notices without modifying frozen items or their human decisions.

    Thin review items are enriched only through hash-bound source manifests
    (id + style + original SHA), then a hash-bound canonical catalog key. A
    missing/drifted enrichment source falls back to explicit unknown notices.
    """
    from .experiment import digest, json_bytes, run_path
    root = Path(root).resolve()
    items = spec.get("items", [])
    if not isinstance(items, list) or any(not isinstance(i, dict) or not isinstance(i.get("id"), str) for i in items):
        raise ValueError("rights catalog requires image items with ids")
    if len({i["id"] for i in items}) != len(items):
        raise ValueError("rights catalog requires unique image ids")
    base = {item["id"]: copy.deepcopy(item) for item in items}
    if not spec.get("run_id"):
        return {ident: normalize_image_rights(item) for ident, item in base.items()}
    source = (run_path(root, spec["run_id"]) / "group-workflow-v1/source-bindings.json").resolve()
    if not source.is_relative_to(root):
        raise ValueError("rights source bindings escape archive root")
    if not source.is_file():
        return {ident: normalize_image_rights(item) for ident, item in base.items()}
    binding = json.loads(source.read_text(encoding="utf-8"))
    receipt_path = source.parent / "build-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.is_file() else {}
    def semantic_sha(value):
        return digest(json_bytes(value))
    unhashed = {key: value for key, value in spec.items() if key != "spec_sha256"}
    if (binding.get("review_spec_sha256") != spec.get("spec_sha256")
            or semantic_sha(unhashed) != spec.get("spec_sha256")
            or receipt.get("run_id") != spec.get("run_id") or receipt.get("status") != "ready"
            or receipt.get("spec_sha256") != spec.get("spec_sha256")
            or receipt.get("binding_sha256") != semantic_sha(binding)):
        raise ValueError("rights frozen source identity mismatch")
    files = {}
    for row in binding.get("files", []):
        if (not isinstance(row, dict) or not isinstance(row.get("path"), str)
                or not isinstance(row.get("sha256"), str) or not SHA256.fullmatch(row["sha256"])
                or row["path"] in files):
            raise ValueError("invalid rights source file binding")
        files[row["path"]] = row["sha256"]
    mapped = {}
    pattern = r"data/private-research/image-rag-canary/runs/[A-Za-z0-9][A-Za-z0-9_.-]{0,79}/manifest\.json"
    for relative, expected in files.items():
        if not re.fullmatch(pattern, relative):
            continue
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file() or _file_sha(path) != expected:
            continue
        manifest = json.loads(path.read_text(encoding="utf-8"))
        for row in manifest.get("items", []):
            target = base.get(row.get("id"))
            expected_sha = target.get("source_sha256", target.get("sha256")) if target else None
            if (target is not None and isinstance(expected_sha, str) and SHA256.fullmatch(expected_sha)
                    and isinstance(target.get("style_id"), str) and target["style_id"]
                    and row.get("style_id") == target["style_id"] and row.get("sha256") == expected_sha):
                ident = target["id"]
                if ident in mapped and mapped[ident].get("catalog_key") != row.get("catalog_key"):
                    raise ValueError("ambiguous rights source catalog mapping")
                mapped[ident] = row
    for ident, row in mapped.items():
        base[ident] = {**copy.deepcopy(row), **base[ident]}
    relative = "data/canonical/archive_records.jsonl"
    canonical = (root / relative).resolve()
    if (relative in files and canonical.is_relative_to(root) and canonical.is_file()
            and _file_sha(canonical) == files[relative]):
        wanted = {row.get("catalog_key") for row in mapped.values() if row.get("catalog_key")}
        records = {}
        with canonical.open(encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                key = row.get("catalog_key")
                if key in wanted:
                    if key in records:
                        raise ValueError("ambiguous canonical rights record")
                    records[key] = row
        for ident, row in mapped.items():
            canonical_record = records.get(row.get("catalog_key"))
            if canonical_record and canonical_record.get("style_id") == base[ident].get("style_id"):
                base[ident] = {**copy.deepcopy(canonical_record), **base[ident]}
    return {ident: normalize_image_rights(item) for ident, item in base.items()}
