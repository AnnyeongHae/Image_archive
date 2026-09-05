"""Prepare a hash-bound public reference-display candidate, never publish it.

Image review and private-preview eligibility are not public permission. This
adapter consumes a separate item-level grant (including review_pending drafts)
and builds new public-shaped assets under ignored private storage. Every output
remains release_eligible=false: subsequent human authorization must bind the
unchanged candidate, grant, deployment target and cost scope externally.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import gzip
import importlib.util
import json
import os
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location("public_release_projection", Path(__file__).with_name("frontend_projection.py"))
projection = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(projection)
from image_rag_eval.rights import RESTRICTED
ROOT = projection.ROOT
GRANT_SCHEMA = "image-gallery-public-reference-grant-1"
CANDIDATE_SCHEMA = "image-gallery-public-reference-candidate-1"
# Short enough for Windows' default MAX_PATH even with content-addressed shards.
OUTPUT = "data/private-research/v2/p"
NOTICES_PATH = "deploy/cloudflare-public/public/THIRD_PARTY_NOTICES.txt"
NOTICES_SHA256 = "4fb21847428a99b1e1936461cc31b704a0e792d368087087a0f70f09be6b2b8c"
SCOPES = ("image_derivatives", "exact_original_prompts", "luna_metadata_candidates", "browse_categories",
          "human_reviewed_groups", "source_attribution", "rights_caution")
GRANT_KEYS = {"schema_version", "decision", "purpose", "snapshot_id", "snapshot_manifest_sha256", "approved_by",
              "approved_at", "decision_evidence", "items", "scopes", "commercial_rights_approved", "license_verified"}
ITEM_KEYS = {"item_id", "group_id", "representative_id", "prompt_sha256", "prepared_sha256"}
REFERENCE_NOTICE = ("참고·연구용으로 이미지와 원문 프롬프트를 소개합니다. 출처 표기는 재사용 허가를 뜻하지 않습니다. "
                    "개별 이미지·프롬프트의 저작권과 이용 조건은 확인되지 않았으며, 저장소에서 관측된 라이선스가 "
                    "개별 자료에 적용된다고 보장하지 않습니다. 재사용·상업 이용 전 원출처의 조건과 필요한 허가를 직접 확인하세요.")


class ReleaseError(ValueError):
    """Only fixed safe error codes; no source text or secret values."""


def _json(raw):
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ReleaseError("duplicate_json_key")
            obj[key] = value
        return obj
    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ReleaseError("nonfinite_json_number")))
    except (UnicodeError, json.JSONDecodeError):
        raise ReleaseError("invalid_grant_json") from None


def _private_input(path, root):
    path = Path(path)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError:
            raise ReleaseError("grant_must_be_local_private_file") from None
    return projection.local_path(root, path.as_posix(), private=True)


def validate_grant(grant, plan, root=ROOT):
    """Validate explicit reference-display scope, not commercial/license rights.

    A pending draft is preparation input only. Even an approved content grant
    does not authorize deployment of an as-yet-unseen artifact.
    """
    if (not isinstance(grant, dict) or set(grant) != GRANT_KEYS
            or grant.get("schema_version") != GRANT_SCHEMA
            or grant.get("purpose") != "public_reference_display"
            or grant.get("decision") not in {"review_pending", "approved"}
            or grant.get("commercial_rights_approved") is not False or grant.get("license_verified") is not False):
        raise ReleaseError("invalid_reference_grant_contract")
    scopes = grant.get("scopes")
    if (not isinstance(scopes, list) or len(scopes) != len(SCOPES)
            or any(not isinstance(v, str) for v in scopes) or set(scopes) != set(SCOPES)):
        raise ReleaseError("reference_grant_scope_mismatch")
    if (not projection._valid_hash(grant.get("snapshot_id"))
            or not projection._valid_hash(grant.get("snapshot_manifest_sha256"))
            or grant["snapshot_id"] != plan["manifest"]["snapshot_id"]
            or grant["snapshot_manifest_sha256"] != plan["manifest_sha256"]):
        raise ReleaseError("reference_grant_snapshot_mismatch")
    evidence_bindings = {}
    if grant["decision"] == "review_pending":
        if any(grant.get(key) is not None for key in ("approved_by", "approved_at", "decision_evidence")):
            raise ReleaseError("pending_grant_must_not_claim_approval")
    else:
        if not isinstance(grant.get("approved_by"), str) or not grant["approved_by"].strip():
            raise ReleaseError("approved_grant_missing_human_identity")
        try:
            stamp = datetime.fromisoformat(grant["approved_at"])
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError()
        except (TypeError, ValueError):
            raise ReleaseError("approved_grant_missing_timestamp") from None
        evidence = grant.get("decision_evidence")
        if (not isinstance(evidence, dict) or set(evidence) != {"path", "sha256"}
                or not projection._valid_hash(evidence.get("sha256"))):
            raise ReleaseError("approved_grant_missing_evidence")
        path = projection.local_path(root, evidence["path"], private=True)
        if not path.is_file() or projection.sha(path.read_bytes()) != evidence["sha256"]:
            raise ReleaseError("approved_grant_evidence_hash_mismatch")
        evidence_bindings[evidence["path"]] = evidence["sha256"]
    grants = grant.get("items")
    if not isinstance(grants, list) or not 0 < len(grants) <= len(plan["items"]):
        raise ReleaseError("invalid_grant_item_count")
    by_id = {row["item_id"]: row for row in plan["items"]}
    selected = {}
    for item in grants:
        if not isinstance(item, dict) or set(item) != ITEM_KEYS or not isinstance(item.get("item_id"), str):
            raise ReleaseError("invalid_grant_item_contract")
        ident = item["item_id"]
        if ident in selected or ident not in by_id:
            raise ReleaseError("unknown_or_duplicate_grant_item")
        source = by_id[ident]
        if any(projection.clean(source["rights_json"].get(key)).casefold() in RESTRICTED
               for key in ("status", "image_rights_status", "prompt_rights_status")):
            raise ReleaseError("explicit_rights_restriction_not_overridden")
        if any(not projection._valid_hash(item.get(key)) for key in ("prompt_sha256", "prepared_sha256")):
            raise ReleaseError("invalid_grant_item_hash")
        expected = {key: source[key] for key in ("item_id", "group_id", "representative_id")}
        expected.update({key: source["private_data"][key] for key in ("prompt_sha256", "prepared_sha256")})
        if item != expected:
            raise ReleaseError("grant_item_source_binding_mismatch")
        selected[ident] = source
    # Partial publication of a reviewed visual group changes its meaning. It
    # must be separately reviewed upstream, never repaired by choosing a new rep.
    groups = defaultdict(set)
    for row in plan["items"]:
        groups[row["group_id"]].add(row["item_id"])
    ids = set(selected)
    for members in groups.values():
        if ids & members and not members <= ids:
            raise ReleaseError("grant_requires_whole_reviewed_groups")
    return sorted(selected.values(), key=lambda r: r["item_id"]), evidence_bindings


def project_public_reference(items, input_count, media, taxonomy, browse_contract):
    """A new allowlisted public projection; no private bundle mode conversion."""
    groups = defaultdict(list)
    for row in items:
        groups[row["group_id"]].append(row)
    cards, details = [], {}
    for group_id, rows in sorted(groups.items()):
        representative_id = rows[0]["representative_id"]
        expanded = []
        for row in sorted(rows, key=lambda r: (r["item_id"] != representative_id, r["item_id"])):
            data, rights = row["private_data"], row["rights_json"]
            meta = projection.normalize_metadata(row["metadata_json"], taxonomy, browse_contract)
            source = {"name": projection.clean(rights.get("source_name") or data.get("source_name")),
                      "url": projection.safe_source_url(rights.get("source_url") or data.get("source_url"))}
            if not source["name"] or not source["url"]:
                raise ReleaseError("reference_display_requires_traceable_source")
            original_notice = projection.clean(rights.get("notice_text"), 1800)
            display_rights = {"badge": "참고용 · 권리 미확인", "notice": REFERENCE_NOTICE + ("\n" + original_notice if original_notice else ""),
                              "attribution": projection.clean(rights.get("attribution_text"), 500) or "출처: " + source["name"],
                              "license": projection.clean(rights.get("license_label"), 160) or "라이선스 미확인"}
            expanded.append({"id": row["item_id"], "style_id": projection.clean(data.get("style_id")),
                             "title": projection.clean(data.get("title"), 220) or projection.clean(data.get("style_id")) or row["item_id"],
                             **media[row["item_id"]], "original_prompt": row["original_prompt"], **meta,
                             "source": source, "rights": display_rights})
        detail = {"id": group_id, "representative_id": representative_id, "members": expanded}
        detail_path = "data/groups/" + projection.sha(projection.encoded(detail)) + ".json"
        details[detail_path] = detail
        rep = expanded[0]
        cards.append({"id": group_id, "representative_id": representative_id, "member_count": len(expanded),
                      "representative": {key: rep[key] for key in ("id", "style_id", "title", "thumbnail", "usage", "style", "background", "source", "category_ids", "categories", "category_source")}
                      | {"rights": {"badge": rep["rights"]["badge"]}},
                      "members": [{key: entry[key] for key in ("id", "style_id", "title", "usage", "style", "background", "keywords", "category_ids", "categories", "category_source")} for entry in expanded],
                      "detail_path": detail_path})
    cards.sort(key=lambda card: (card["representative"]["style_id"], card["id"]))
    withheld = input_count - len(items)
    return {"schema_version": projection.SCHEMA, "mode": "public", "status": "public_reference_display",
            "browse_taxonomy_version": browse_contract["version"],
            "browse_categories": [{"id": row["id"], "label": row["label"]} for row in [*browse_contract["categories"], browse_contract["unclassified"]]],
            "counts": {"images": len(items), "groups": len(cards), "variants": len(items) - len(cards), "excluded": withheld, "withheld": withheld},
            "groups": cards}, details


def support_files():
    def page(title, body):
        return ("<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                "<meta name=\"robots\" content=\"noindex,nofollow,noarchive\"><title>" + title + " · Photoposting</title>"
                "<link rel=\"stylesheet\" href=\"/gallery.css\"></head><body><main class=\"page-shell\"><h1>" + title + "</h1>" + body
                + "<p><a href=\"/\">갤러리로 돌아가기</a></p></main></body></html>\n").encode("utf-8")
    return {"notice.html": page("출처와 권리 안내", "<p>" + REFERENCE_NOTICE + "</p><p>각 이미지의 상세 화면에 원출처 링크와 관측된 라이선스를 표시합니다. "
                               "활용 아이디어·분류는 AI 분석 후보이며 사실성이나 이용 권리를 보증하지 않습니다. 원저작자·권리자의 요청이 있으면 "
                               "해당 자료를 확인하고 공개 범위를 재검토해야 합니다.</p><p><a href=\"/THIRD_PARTY_NOTICES.txt\">원출처 저작권·허가 고지 원문</a>은 "
                               "awesome-gpt-image-2 저장소(CASE 출처)의 보존된 고지입니다. 이 파일이 다른 출처의 자료까지 포함하거나 개별 이미지의 권리를 "
                               "확인한 것은 아닙니다. 다른 출처의 개별 이미지·프롬프트 권리는 계속 미확인입니다.</p>"),
            "privacy.html": page("개인정보와 외부 연결", "<p>이 공개 갤러리에는 계정 가입, 개인 메모 입력, 관리자 로그인 또는 개인용 RAG API 호출 기능이 없습니다. "
                                 "검색과 필터는 내려받은 카탈로그를 브라우저에서 처리하며, 이 갤러리 코드는 별도의 방문 분석 SDK를 로드하지 않습니다.</p>"
                                 "<p>호스팅 제공자는 서비스 제공·보안을 위해 요청 정보를 처리할 수 있습니다. 외부 출처 링크를 열면 해당 사이트의 개인정보·이용 정책이 적용됩니다. "
                                 "권한이 필요한 API나 모델 제공자에게 검색어·이미지를 보내지 않습니다.</p>"),
            "404.html": page("페이지를 찾을 수 없습니다", "<p>요청한 공개 자료나 경로가 없습니다.</p>"),
            "robots.txt": b"User-agent: *\nDisallow: /\n",
            "_headers": ("/*\n  Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'\n"
                         "  Referrer-Policy: no-referrer\n  X-Content-Type-Options: nosniff\n  X-Frame-Options: DENY\n  X-Robots-Tag: noindex, nofollow, noarchive\n"
                         "  Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()\n  Cache-Control: public, max-age=300, must-revalidate\n"
                         "/media/*\n  Cache-Control: public, max-age=31536000, immutable\n/data/groups/*\n  Cache-Control: public, max-age=31536000, immutable\n").encode("utf-8")}


def _write_candidate(target, files, grant_raw, receipt):
    complete = {"assets/" + name: raw for name, raw in files.items()}
    complete["grant.json"] = grant_raw
    complete["candidate.json"] = projection.encoded(receipt)
    if os.name == "nt" and any(len(str(target / name)) >= 260 for name in complete):
        raise ReleaseError("candidate_path_exceeds_windows_limit")
    if target.exists():
        actual = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
        if actual != set(complete) or any(projection.local_path(target, name).read_bytes() != raw for name, raw in complete.items()):
            raise ReleaseError("immutable_candidate_conflict")
        return
    target.mkdir(parents=True, exist_ok=False)
    # candidate.json is the final completion marker, outside the deployable assets.
    for name, raw in complete.items():
        path = projection.local_path(target, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())


def build_candidate(root=ROOT, *, snapshot=None, grant=None, grant_sha256=None, apply=False):
    root = Path(root).resolve()
    if grant is None:
        return {"status": "blocked", "blocked_reason": "explicit_item_reference_grant_required", "release_eligible": False,
                "permission_pending": True, "network_calls": 0, "model_calls": 0, "new_embedding_calls": 0}
    if not projection._valid_hash(grant_sha256):
        raise ReleaseError("expected_grant_sha256_required")
    grant_path = _private_input(grant, root)
    if not grant_path.is_file() or not 0 < grant_path.stat().st_size <= 2 * 1024 * 1024:
        raise ReleaseError("grant_missing_or_oversized")
    grant_raw = grant_path.read_bytes()
    if projection.sha(grant_raw) != grant_sha256:
        raise ReleaseError("grant_hash_mismatch")
    grant_document = _json(grant_raw)
    snapshot = Path(snapshot) if snapshot else root / projection.PINNED_SNAPSHOT
    if not snapshot.is_absolute():
        snapshot = root / snapshot
    plan = projection.read_snapshot(snapshot, root)
    items, evidence_bindings = validate_grant(grant_document, plan, root)
    notices_raw = projection.local_path(root, NOTICES_PATH).read_bytes()
    if projection.sha(notices_raw) != NOTICES_SHA256:
        raise ReleaseError("third_party_notice_source_hash_mismatch")
    notice_bindings = {NOTICES_PATH: NOTICES_SHA256}
    taxonomy, taxonomy_bindings = projection.load_taxonomy(root)
    browse, browse_bindings = projection.load_browse_contract(root)
    # Metadata integrity was pinned in the snapshot; validate approved rows before encoding media.
    normalized = [projection.normalize_metadata(r["metadata_json"], taxonomy, browse) for r in items]
    media, files, media_bindings, conversion = projection.prepare_media(items, root)
    catalog, details = project_public_reference(items, len(plan["items"]), media, taxonomy, browse)
    files["data/catalog.json"] = projection.encoded(catalog)
    files.update({name: projection.encoded(value) for name, value in details.items()})
    frontend_sources = {}
    for name in projection.SHELL_FILES:
        path = projection.local_path(root, projection.SHELL + "/" + name)
        raw = path.read_bytes()
        frontend_sources[projection.SHELL + "/" + name] = projection.sha(raw)
        if name == "index.html":
            text = raw.decode("utf-8")
            if text.count("</footer>") != 1:
                raise ReleaseError("frontend_footer_contract_drift")
            text = text.replace("</footer>", '<nav aria-label="이용 안내"><a href="/notice.html">출처·권리 안내</a> · <a href="/privacy.html">개인정보 안내</a></nav></footer>')
            raw = text.encode("utf-8")
        files[name] = raw
    files.update(support_files())
    files["THIRD_PARTY_NOTICES.txt"] = notices_raw
    projection._assert_no_private_text(files)
    code_sources = {"platform/v2/local/public_frontend_release.py": projection.sha(Path(__file__).read_bytes()),
                    "platform/v2/local/frontend_projection.py": projection.sha(Path(projection.__file__).read_bytes()),
                    "platform/v2/local/cloud_snapshot.py": projection.sha(Path(projection.__file__).with_name("cloud_snapshot.py").read_bytes()),
                    "src/image_rag_eval/rights.py": projection.sha((ROOT / "src/image_rag_eval/rights.py").read_bytes())}
    served_files = {name: projection.sha(raw) for name, raw in sorted(files.items())}
    identity = {"schema_version": CANDIDATE_SCHEMA, "snapshot_id": plan["manifest"]["snapshot_id"], "input_manifest_sha256": plan["manifest_sha256"],
                "input_files": plan["manifest"]["files"], "grant_sha256": grant_sha256, "scopes": list(SCOPES),
                "code_sources": code_sources, "frontend_sources": frontend_sources, "taxonomy_sources": taxonomy_bindings,
                "browse_taxonomy_sources": browse_bindings, "media_sources": media_bindings, "decision_evidence_sources": evidence_bindings,
                "third_party_notice_sources": notice_bindings,
                "conversion": conversion, "served_files": served_files}
    candidate_id = projection.sha(projection.encoded(identity))
    target = projection.local_path(root, OUTPUT + "/" + candidate_id, private=True)
    receipt = {"schema_version": CANDIDATE_SCHEMA, "candidate_id": candidate_id, "identity": identity, "mode": "public",
               "status": "prepare_only", "permission_pending": True, "release_eligible": False,
               "grant_decision": grant_document["decision"], "grant_sha256": grant_sha256,
               "publication_requires": "separate_hash_bound_human_approval_of_candidate_grant_target_and_cost_scope",
               "license_verified": False, "commercial_rights_approved": False,
               "source_snapshot_id": plan["manifest"]["snapshot_id"], "source_manifest_sha256": plan["manifest_sha256"],
               "counts": catalog["counts"], "served_files": served_files, "assets_directory": "assets",
               "metadata_status_counts": dict(Counter(r["metadata_status"] for r in normalized)),
               "catalog_bytes": len(files["data/catalog.json"]), "catalog_gzip_bytes_calculated": len(gzip.compress(files["data/catalog.json"], mtime=0)),
               "media_files": sum(name.startswith("media/") for name in files),
               "media_bytes": sum(len(raw) for name, raw in files.items() if name.startswith("media/")),
               "total_served_bytes": sum(map(len, files.values())), "network_calls": 0, "model_calls": 0, "new_embedding_calls": 0,
               "approval_writes": 0, "source_rights_modified": False, "originals_modified": False, "public_release": False}
    for relative, expected in {**taxonomy_bindings, **browse_bindings, **media_bindings, **frontend_sources, **evidence_bindings, **notice_bindings}.items():
        if projection.sha(projection.local_path(root, relative).read_bytes()) != expected:
            raise ReleaseError("source_changed_during_candidate_build")
    for name, expected in plan["manifest"]["files"].items():
        if projection.sha((plan["path"] / name).read_bytes()) != expected["sha256"]:
            raise ReleaseError("snapshot_changed_during_candidate_build")
    if (projection.sha((plan["path"] / "manifest.json").read_bytes()) != plan["manifest_sha256"]
            or projection.sha(grant_path.read_bytes()) != grant_sha256):
        raise ReleaseError("grant_or_snapshot_changed_during_candidate_build")
    if apply:
        _write_candidate(target, files, grant_raw, receipt)
    return {"status": "prepare_only" if apply else "dry_run", "candidate_id": candidate_id, "path": str(target),
            "assets_path": str(target / "assets"), "grant_decision": receipt["grant_decision"], "grant_sha256": grant_sha256,
            "counts": receipt["counts"], "catalog_bytes": receipt["catalog_bytes"], "catalog_gzip_bytes_calculated": receipt["catalog_gzip_bytes_calculated"],
            "media_bytes": receipt["media_bytes"], "media_files": receipt["media_files"], "total_served_bytes": receipt["total_served_bytes"],
            "release_eligible": False, "permission_pending": True, "network_calls": 0, "model_calls": 0, "new_embedding_calls": 0, "public_release": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-root", type=Path, default=ROOT)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--grant", type=Path)
    parser.add_argument("--grant-sha256")
    parser.add_argument("--apply", action="store_true", help="Write only an ignored immutable local candidate; never publish")
    args = parser.parse_args(argv)
    try:
        result = build_candidate(args.archive_root, snapshot=args.snapshot, grant=args.grant, grant_sha256=args.grant_sha256, apply=args.apply)
        print(projection.encoded(result).decode("utf-8"), end="")
        return 0 if result["status"] != "blocked" else 2
    except Exception as exc:
        code = str(exc) if isinstance(exc, (ReleaseError, projection.ProjectionError)) else "public_reference_candidate_failed"
        print(json.dumps({"status": "failed", "error_code": code, "release_eligible": False, "network_calls": 0, "model_calls": 0, "new_embedding_calls": 0}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
