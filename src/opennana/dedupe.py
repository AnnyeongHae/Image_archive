from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    from .common import (
        DATA_ROOT,
        DEFAULT_CANONICAL,
        atomic_write_json,
        hamming_distance,
        iter_jsonl,
        read_json,
        sha256_text,
        simhash64,
        stable_json,
        template_text,
        token_set,
    )
except ImportError:
    from common import DATA_ROOT, DEFAULT_CANONICAL, atomic_write_json, hamming_distance, iter_jsonl, read_json, sha256_text, simhash64, stable_json, template_text, token_set


def descriptor(record: dict[str, Any], *, relation_source: str = "canonical") -> dict[str, Any]:
    return {
        "relation_source": relation_source,
        "catalog_key": record.get("catalog_key"),
        "record_id": record.get("record_id"),
        "style_id": record.get("style_id"),
        "upstream_id": record.get("upstream_id"),
        "title": record.get("title"),
        "source_url": record.get("source_url") or (record.get("source") or {}).get("url"),
    }


def canonical_prompt(record: dict[str, Any]) -> tuple[str, str | None]:
    prompt = record.get("prompt")
    if not isinstance(prompt, dict):
        return "", None
    text = prompt.get("text")
    if not isinstance(text, str) or not text.strip():
        return "", None
    return text, prompt.get("sha256") or sha256_text(text)


def canonical_opennana_identity(record: dict[str, Any]) -> tuple[str | None, str | None]:
    source = record.get("source") if isinstance(record.get("source"), dict) else {}
    provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
    raw = provenance.get("raw_source") if isinstance(provenance.get("raw_source"), dict) else {}
    provider = (
        source.get("provider")
        or source.get("type")
        or source.get("name")
        or raw.get("provider")
        or raw.get("source")
        or raw.get("source_id")
    )
    if "opennana" not in str(provider).casefold():
        return None, None
    upstream_id = source.get("upstream_id") or raw.get("upstream_id") or raw.get("id")
    content_hash = record.get("content_sha256") or raw.get("content_sha256")
    return str(upstream_id) if upstream_id is not None else None, str(content_hash) if content_hash else None


def jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def near_score(left: dict[str, Any], right_text: str, right_tokens: set[str], right_simhash: int) -> float | None:
    left_text = left["prompt_text"]
    if not left_text or not right_text:
        return None
    length_ratio = min(len(left_text), len(right_text)) / max(len(left_text), len(right_text))
    if length_ratio < 0.90:
        return None
    left_tokens = left["_tokens"]
    token_ratio = min(len(left_tokens), len(right_tokens)) / max(len(left_tokens), len(right_tokens), 1)
    if token_ratio < 0.88:
        return None
    distance = hamming_distance(left["_simhash"], right_simhash)
    # SimHash is only a blocker here; the high token overlap and length gates
    # remain the decisive constraints. Twelve bits tolerates one substituted
    # descriptive term without grouping loosely related prompts.
    if distance > 12:
        return None
    overlap = jaccard(left_tokens, right_tokens)
    if overlap < 0.88:
        return None
    return round((overlap * 0.75) + (length_ratio * 0.20) + ((64 - distance) / 64 * 0.05), 6)


def prepare(record: dict[str, Any]) -> dict[str, Any]:
    result = dict(record)
    result["_tokens"] = token_set(record["prompt_text"])
    result["_simhash"] = simhash64(record["prompt_text"])
    return result


def classify_bundle(bundle: dict[str, Any], canonical_path: Path) -> dict[str, Any]:
    incoming = [prepare(record) for record in bundle.get("records", [])]
    exact_matches: dict[str, list[dict[str, Any]]] = defaultdict(list)
    source_matches: dict[str, list[tuple[str | None, dict[str, Any]]]] = defaultdict(list)
    remix_matches: dict[str, list[dict[str, Any]]] = defaultdict(list)
    near_matches: dict[str, list[dict[str, Any]]] = defaultdict(list)
    canonical_scanned = 0

    if canonical_path.exists():
        for canonical in iter_jsonl(canonical_path):
            canonical_scanned += 1
            c_text, c_sha = canonical_prompt(canonical)
            c_descriptor = descriptor(canonical)
            if c_sha:
                for item in incoming:
                    if item["prompt_sha256"] == c_sha:
                        exact_matches[item["upstream_id"]].append(c_descriptor)
            c_source_id, c_content_hash = canonical_opennana_identity(canonical)
            if c_source_id:
                source_matches[c_source_id].append((c_content_hash, c_descriptor))
            if not c_text:
                continue
            potentially_near = [
                item for item in incoming
                if min(len(item["prompt_text"]), len(c_text)) / max(len(item["prompt_text"]), len(c_text), 1) >= 0.75
            ]
            if not potentially_near:
                continue
            c_template_sha = sha256_text(template_text(c_text))
            c_tokens = token_set(c_text)
            c_simhash = simhash64(c_text)
            for item in potentially_near:
                if item["prompt_sha256"] == c_sha:
                    continue
                if item.get("prompt_template_sha256") and item["prompt_template_sha256"] == c_template_sha:
                    remix_matches[item["upstream_id"]].append(c_descriptor)
                    continue
                score = near_score(item, c_text, c_tokens, c_simhash)
                if score is not None:
                    near_matches[item["upstream_id"]].append({**c_descriptor, "similarity": score})

    results: list[dict[str, Any]] = []
    earlier: list[dict[str, Any]] = []
    for item in incoming:
        upstream_id = item["upstream_id"]
        same_source = source_matches.get(upstream_id, [])
        exact = list(exact_matches.get(upstream_id, []))
        remix = list(remix_matches.get(upstream_id, []))
        near = list(near_matches.get(upstream_id, []))
        for previous in earlier:
            previous_descriptor = descriptor(previous, relation_source="incoming_batch")
            if previous["prompt_sha256"] == item["prompt_sha256"]:
                exact.append(previous_descriptor)
            elif previous.get("prompt_template_sha256") == item.get("prompt_template_sha256"):
                remix.append(previous_descriptor)
            else:
                score = near_score(item, previous["prompt_text"], previous["_tokens"], previous["_simhash"])
                if score is not None:
                    near.append({**previous_descriptor, "similarity": score})

        if same_source:
            unchanged = [match for content_hash, match in same_source if content_hash == item["content_sha256"]]
            if unchanged:
                classification = "same_source_unchanged"
                matches = unchanged
                auto_collapsed = True
            else:
                classification = "same_source_update"
                matches = [match for _, match in same_source]
                auto_collapsed = False
        elif exact:
            classification = "exact_duplicate"
            matches = exact
            auto_collapsed = True
        elif remix:
            classification = "remix_family"
            matches = remix
            auto_collapsed = False
        elif near:
            classification = "near_duplicate"
            matches = sorted(near, key=lambda match: (-float(match.get("similarity", 0)), str(match)))
            auto_collapsed = False
        else:
            classification = "new"
            matches = []
            auto_collapsed = False

        clean_item = {key: value for key, value in item.items() if not key.startswith("_")}
        clean_item["dedupe"] = {
            "classification": classification,
            "auto_collapsed": auto_collapsed,
            "auto_merged": False,
            "matches": matches[:5],
            "method": "exact_sha256_then_conservative_template_or_token_simhash_candidates",
        }
        clean_item["workflow_status"] = "duplicate_collapsed" if auto_collapsed else "dedupe_classified"
        results.append(clean_item)
        earlier.append(item)

    counts = Counter(record["dedupe"]["classification"] for record in results)
    return {
        "schema_version": "opennana-dedupe-bundle-1.0",
        "run_id": bundle["run_id"],
        "observed_at": bundle["observed_at"],
        "source_normalized": f"staging/normalized-{bundle['run_id']}.json",
        "canonical_compared": str(canonical_path),
        "summary": {
            "input_records": len(results),
            "canonical_records_scanned": canonical_scanned,
            "classification_counts": dict(sorted(counts.items())),
            "auto_collapsed": sum(1 for record in results if record["dedupe"]["auto_collapsed"]),
            "auto_merged": 0,
        },
        "records": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify OpenNana duplicate candidates without auto-merging (dry-run by default).")
    parser.add_argument("--input", type=Path, default=DATA_ROOT / "staging" / "normalized-sample-canary-v1.json")
    parser.add_argument("--canonical", type=Path, default=DEFAULT_CANONICAL)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    classified = classify_bundle(read_json(args.input), args.canonical)
    output = args.output or DATA_ROOT / "staging" / f"dedupe-{classified['run_id']}.json"
    if args.apply:
        atomic_write_json(output, classified)
        print(stable_json({"written": str(output), "summary": classified["summary"]}), end="")
    else:
        print(stable_json({"writes": False, "would_write": str(output), "summary": classified["summary"]}), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
