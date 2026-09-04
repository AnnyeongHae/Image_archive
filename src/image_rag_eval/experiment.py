"""Private, bounded embedding experiment. No release or collection mutations."""
from __future__ import annotations

import hashlib
import html
import io
import itertools
import json
import math
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps

MODEL = "gemini-embedding-2"
DIMENSIONS = 3072
MAX_IMAGES = 20
MAX_QUERIES = 15
MAX_CALLS = 55
MAX_COST_USD = 0.10
IMAGE_RESERVE_USD = 0.00012
TEXT_RESERVE_USD = 8192 * 0.20 / 1_000_000
RELATION_LABELS = {"exact", "near_copy", "visual_family", "semantic_only", "unrelated"}
PRIVATE_RUNS = Path("data/private-research/image-rag-canary/runs")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: object) -> None:
    """Atomic replacement, only called inside an explicitly authorized private run."""
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(json_bytes(value))
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def safe_source(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root.resolve()):
        raise ValueError("source must stay within the archive")
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".avif"}:
        raise ValueError("only local raster images are accepted")
    if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("source unavailable or larger than 16 MiB")
    return path


def run_path(root: Path, run_id: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", run_id):
        raise ValueError("invalid run id")
    parent = (root / PRIVATE_RUNS).resolve()
    path = (parent / run_id).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_relative_to(parent):
        raise ValueError("run path escapes private archive")
    return path


def prepared_image(path: Path) -> bytes:
    """Same deterministic 768px RGB PNG for both arms; original stays immutable."""
    with Image.open(path) as source:
        if source.width * source.height > 80_000_000 or getattr(source, "n_frames", 1) != 1:
            raise ValueError("image exceeds decode limit or is animated")
        source = ImageOps.exif_transpose(source)
        if source.mode in {"RGBA", "LA"} or "transparency" in source.info:
            rgba = source.convert("RGBA")
            background = Image.new("RGBA", rgba.size, "white")
            background.alpha_composite(rgba)
            converted = background.convert("RGB")
        else:
            converted = source.convert("RGB")
        converted.thumbnail((768, 768), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        converted.save(output, format="PNG", optimize=True)
        return output.getvalue()


def bounded_text(value: str, byte_limit: int = 6000) -> str:
    return value.encode("utf-8")[:byte_limit].decode("utf-8", errors="ignore").strip()


def plan(image_count: int = MAX_IMAGES, query_count: int = MAX_QUERIES) -> dict:
    if not 1 <= image_count <= MAX_IMAGES or not 0 <= query_count <= MAX_QUERIES:
        raise ValueError("canary permits 1..20 images and 0..15 queries")
    return {
        "status": "dry_run", "network_calls": 0, "writes": 0,
        "model": MODEL, "arms": {"A": "image_only", "B": "same_image_plus_source_prompt"},
        "not_a_cross_provider_benchmark": True, "max_images": image_count,
        "max_queries": query_count, "max_inference_calls": image_count * 2 + query_count,
        "dimensions_requested": DIMENSIONS, "dimensions_evaluated_locally": [768, 1536, 3072],
        "reservation_upper_bound_usd": round(image_count * 2 * IMAGE_RESERVE_USD
            + (image_count + query_count) * TEXT_RESERVE_USD, 8),
        "billing_basis": "2026-09-03 standard pricing; full 8192-token text allowance per text-bearing call",
        "requires": ["explicit_paid_budget_approval", "sample_external_ai_approval", "human_relevance_labels_for_accuracy"],
        "qdrant_writes": 0, "canonical_writes": 0, "automatic_retry": False,
    }


def annotations_template(manifest: dict) -> dict:
    return {
        "schema_version": "1", "manifest_sha256": digest(json_bytes(manifest)),
        "reviewer": "", "reviewed_at": "",
        "items": [{"id": item["id"], "approved_for_external_ai": False} for item in manifest["items"]],
        "queries": [{"id": "q01", "text": "", "relevance": {}, "human_judged": False}],
        "pairs": [],
        "instructions": "Queries: grade EVERY sampled item 0=irrelevant,1=partial,2=good,3=ideal. Pair labels: exact/near_copy/visual_family/semantic_only/unrelated. Approval here permits private AI analysis only, never public release.",
    }


def validate_annotations(manifest: dict, annotations: dict, *, require_approval: bool) -> None:
    if annotations.get("manifest_sha256") != digest(json_bytes(manifest)):
        raise ValueError("annotations belong to another manifest")
    ids = {item["id"] for item in manifest["items"]}
    if len(ids) != len(manifest["items"]):
        raise ValueError("manifest has duplicate ids")
    approvals = annotations.get("items", [])
    if len(approvals) != len(ids) or {i.get("id") for i in approvals} != ids:
        raise ValueError("approval items must exactly match the manifest")
    if require_approval and (not annotations.get("reviewer") or not annotations.get("reviewed_at")
            or not all(i.get("approved_for_external_ai") is True for i in approvals)):
        raise ValueError("human approval for every external AI input is required")
    queries = annotations.get("queries", [])
    if not isinstance(queries, list) or len(queries) > MAX_QUERIES:
        raise ValueError("at most 15 queries")
    query_ids = set()
    for query in queries:
        query_id = query.get("id")
        if not isinstance(query_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,50}", query_id) or query_id in query_ids:
            raise ValueError("query ids must be unique safe identifiers")
        query_ids.add(query_id)
        if not isinstance(query.get("text"), str) or len(query["text"].encode("utf-8")) > 6000:
            raise ValueError("query must be text up to 6000 UTF-8 bytes")
        if require_approval and not query["text"].strip():
            raise ValueError("fill or remove blank queries before inference")
        labels = query.get("relevance", {})
        if not isinstance(labels, dict) or not set(labels).issubset(ids):
            raise ValueError("unknown relevance item")
        if any(isinstance(v, bool) or not isinstance(v, int) or v not in range(4) for v in labels.values()):
            raise ValueError("relevance must be integer 0..3")
        if query.get("human_judged") is True and (set(labels) != ids or not annotations.get("reviewer")):
            raise ValueError("human metrics require complete corpus judgments and reviewer")
    seen_pairs = set()
    for pair in annotations.get("pairs", []):
        left, right = pair.get("a"), pair.get("b")
        if left not in ids or right not in ids or left == right or pair.get("relation") not in RELATION_LABELS:
            raise ValueError("invalid human pair label")
        key = tuple(sorted((left, right)))
        if key in seen_pairs:
            raise ValueError("duplicate human pair label")
        seen_pairs.add(key)


def prepare(root: Path, run_id: str, limit: int = MAX_IMAGES) -> dict:
    from .dataset import build_manifest
    from .similarity import image_signals, prompt_signals, compare_pair, build_groups

    destination = run_path(root, run_id)
    if destination.exists():
        raise ValueError("run already exists; use a new run id, never overwrite preparation")
    manifest = build_manifest(root, limit)
    if not manifest.get("items"):
        raise ValueError("no eligible local images")
    # Finish all reads/validation first. Partial writes cannot alter canonical input.
    prepared = {}
    for item in manifest["items"]:
        path = safe_source(root, item["path"])
        actual = digest(path.read_bytes())
        if actual != item["sha256"]:
            raise ValueError("source digest mismatch")
        item["signals"] = image_signals(path)
        item["prompt_signals"] = prompt_signals(item.get("prompt", ""))
        item["embedding_prompt"] = bounded_text(item.get("prompt", ""))
        item["prompt_truncated"] = item["embedding_prompt"] != item.get("prompt", "").strip()
        data = prepared_image(path)
        item["prepared_sha256"] = digest(data)
        item["prepared_path"] = f"inputs/{digest(data)}.png"
        item["external_ai_approved"] = False
        prepared[item["prepared_path"]] = data
    pairs = [compare_pair(a, b) for a, b in itertools.combinations(manifest["items"], 2)]
    groups = build_groups(manifest["items"], pairs)
    manifest["experiment"] = plan(len(manifest["items"]))
    manifest["preprocessing"] = "EXIF transpose; alpha on white; RGB; max side 768; PNG; both arms identical pixels"
    manifest["created_at"] = now()
    destination.mkdir(parents=True)
    (destination / "inputs").mkdir()
    for relative, data in prepared.items():
        (destination / relative).write_bytes(data)
    write_json(destination / "manifest.json", manifest)
    write_json(destination / "annotations.template.json", annotations_template(manifest))
    offline = {"status": "offline_only", "pairs": pairs, "groups": groups,
        "embedding_calls": 0, "embedding_accuracy": None, "reason": "not_executed_no_human_gold"}
    write_json(destination / "offline.json", offline)
    (destination / "review.html").write_text(review_html(manifest, offline), encoding="utf-8")
    write_json(destination / "prepared.json", {"complete": True,
        "manifest_sha256": digest(json_bytes(manifest)), "at": now()})
    return {"status": "prepared_local_only", "run_id": run_id, "items": len(manifest["items"]),
        "pairs": len(pairs), "groups": len(groups), "embedding_calls": 0,
        "review_path": str(destination / "review.html")}


def review_html(manifest: dict, offline: dict) -> str:
    cards = []
    for item in manifest["items"]:
        ident = html.escape(item["id"], quote=True)
        cards.append(f'<article><img loading="lazy" src="{html.escape(item["prepared_path"], quote=True)}" alt="{html.escape(item.get("style_id", ident), quote=True)}"><h2>{html.escape(item.get("style_id", ident))}</h2><small>{ident}</small><p>{html.escape(item.get("review_status") or "unverified")}</p><label><input type="checkbox" data-id="{ident}"> 외부 AI 분석에 이 샘플 사용 승인</label><details><summary>프롬프트 확인</summary><pre>{html.escape(item.get("embedding_prompt", ""))}</pre></details></article>')
    template = json_bytes(annotations_template(manifest)).decode().replace("<", "\\u003c")
    lookup = {item["id"]: item for item in manifest["items"]}
    group_cards = []
    for group in offline["groups"]:
        previews = "".join(f'<figure><img loading="lazy" src="{html.escape(lookup[ident]["prepared_path"], quote=True)}" alt=""><figcaption>{html.escape(lookup[ident].get("style_id", ident))}</figcaption></figure>' for ident in group["member_ids"] if ident in lookup)
        group_cards.append(f'<article><h3>{html.escape(group["kind"])}</h3><p>{html.escape(group["status"])}</p><div class="grid">{previews}</div></article>')
    groups = "".join(group_cards)
    return '''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>이미지 RAG · 비공개 A/B 샘플 검토</title><style>body{font:16px system-ui;margin:24px;background:#10141c;color:#e6edf6}main{max-width:1320px;margin:auto}h1{font-size:26px}a{color:#9fc9ff}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:18px}article{background:#1b2431;padding:14px;border-radius:12px;overflow-wrap:anywhere}img{width:100%;height:240px;object-fit:contain;background:#0b1017}h2{font-size:17px}pre{white-space:pre-wrap;overflow-wrap:anywhere}button,input{font:inherit;padding:8px}button{margin:16px 0}input[type=checkbox]{width:20px;height:20px}label{display:block;margin:10px 0}.notice{border-left:4px solid #efc267;padding:12px;background:#27251d}</style><main><h1>이미지 RAG · 비공개 A/B 샘플 검토</h1><p class="notice">로컬 해시 분석만 수행했습니다. 임베딩 API 호출·공개 배포·삭제·병합은 하지 않았습니다. 체크는 외부 AI 입력 승인일 뿐 저작권/공개 승인이나 과금 승인이 아닙니다. 정확도 정답은 별도로 입력해야 합니다.</p><p>A: 이미지만 / B: 같은 이미지 + 원래 프롬프트. 동일 모델의 입력 비교이며 모델 간 성능 비교가 아닙니다.</p><label>검토자 <input id="reviewer" autocomplete="off"></label><button id="save">선택 결과 JSON 내려받기</button><p>내려받은 JSON의 queries에 실제 검색문을 쓰고 모든 샘플을 0..3으로 평가하세요. pairs에는 직접 확인한 이미지 관계만 입력합니다. 파일을 run 폴더의 annotations.json으로 저장한 후 사용합니다.</p><section class="grid">''' + "".join(cards) + '''</section><h2>로컬 관계 후보 — 자동 삭제/합치기 없음</h2>''' + groups + '''<script>const template=''' + template + ''';document.getElementById('save').onclick=()=>{const reviewer=document.getElementById('reviewer').value.trim();if(!reviewer){alert('검토자 이름을 입력하세요.');return;}const output=structuredClone(template);output.reviewer=reviewer;output.reviewed_at=new Date().toISOString();output.items=[...document.querySelectorAll('[data-id]')].map(x=>({id:x.dataset.id,approved_for_external_ai:x.checked}));const url=URL.createObjectURL(new Blob([JSON.stringify(output,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='annotations.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};</script></main></html>'''


@contextmanager
def run_lock(destination: Path):
    path = destination / ".execution.lock"
    # A crash leaves the lock for human inspection; never auto-break a stale lock.
    handle = path.open("x", encoding="utf-8")
    try:
        handle.write(now())
        handle.close()
        yield
    finally:
        handle.close()
        path.unlink(missing_ok=True)


class BudgetLedger:
    def __init__(self, path: Path, maximum_usd: float, maximum_calls: int = MAX_CALLS):
        if not math.isfinite(maximum_usd) or not 0 < maximum_usd <= MAX_COST_USD:
            raise ValueError("paid budget must be > 0 and <= 0.10 USD")
        self.path, self.maximum_usd, self.maximum_calls = path, maximum_usd, maximum_calls
        self.data = read_json(path) if path.exists() else {"attempts": [], "pricing_verified_date": "2026-09-03"}

    def reserve(self, key: str, amount: float) -> None:
        attempts = self.data["attempts"]
        if any(a["key"] == key for a in attempts):
            raise ValueError("an earlier attempt has no validated cache; manual investigation required, no retry")
        total = sum(a["reserved_usd"] for a in attempts)
        if len(attempts) >= self.maximum_calls or total + amount > self.maximum_usd + 1e-12:
            raise ValueError("hard inference reservation budget reached")
        attempts.append({"key": key, "reserved_usd": amount, "status": "reserved", "at": now()})
        write_json(self.path, self.data)  # Persist BEFORE the network request, including uncertain outcomes.

    def finish(self, key: str, status: str, usage: dict | None = None) -> None:
        attempt = next(a for a in self.data["attempts"] if a["key"] == key)
        attempt.update({"status": status, "usage": usage or {}})
        write_json(self.path, self.data)

    def precheck(self, requests: dict[str, float], cached_keys: set[str]) -> None:
        """Reject an unaffordable remaining experiment before its first paid call."""
        attempts = self.data["attempts"]
        attempted_keys = {a["key"] for a in attempts}
        if (set(requests) & attempted_keys) - cached_keys:
            raise ValueError("an earlier attempt has no validated cache; manual investigation required")
        pending = set(requests) - cached_keys
        total = sum(a["reserved_usd"] for a in attempts) + sum(requests[key] for key in pending)
        if total > self.maximum_usd + 1e-12 or len(attempts) + len(pending) > self.maximum_calls:
            raise ValueError("entire remaining canary exceeds approved reservation budget")


def unit_prefix(vector: list[float], size: int) -> list[float]:
    if len(vector) < size or any(isinstance(v, bool) or not isinstance(v, (int, float))
            or not math.isfinite(v) for v in vector):
        raise ValueError("invalid embedding vector")
    selected = vector[:size]
    norm = math.sqrt(sum(v * v for v in selected))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("zero embedding vector")
    return [v / norm for v in selected]


def execute(root: Path, run_id: str, annotations: dict, credentials: dict, *, allow_paid: bool,
            maximum_usd: float = 0, embedder=None, sleep=time.sleep) -> dict:
    from .providers import GeminiEmbedder

    if not allow_paid:
        raise ValueError("paid inference has not been explicitly approved")
    destination = run_path(root, run_id)
    manifest = read_json(destination / "manifest.json")
    completion = read_json(destination / "prepared.json")
    if completion.get("complete") is not True or completion.get("manifest_sha256") != digest(json_bytes(manifest)):
        raise ValueError("private sample preparation is incomplete or changed")
    specification = manifest.get("experiment", {})
    if specification.get("model") != MODEL or specification.get("dimensions_requested") != DIMENSIONS:
        raise ValueError("execution model or dimensions differ from reviewed experiment")
    validate_annotations(manifest, annotations, require_approval=True)
    if not 1 <= len(manifest["items"]) <= MAX_IMAGES:
        raise ValueError("invalid canary size")
    # Revalidate every input before ANY call. The review signature binds all prompts/hashes.
    images = {}
    for item in manifest["items"]:
        if digest(safe_source(root, item["path"]).read_bytes()) != item["sha256"]:
            raise ValueError("source changed after review")
        relative = Path(item["prepared_path"])
        path = (destination / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to((destination / "inputs").resolve()):
            raise ValueError("prepared input escapes run")
        data = path.read_bytes()
        if digest(data) != item["prepared_sha256"]:
            raise ValueError("prepared input changed after review")
        if len(item["embedding_prompt"].encode("utf-8")) > 6000:
            raise ValueError("prompt exceeds reviewed byte cap")
        images[item["id"]] = data
    # Serialize all canary runs from this archive, not only identical run IDs.
    # The monetary ledger remains run-scoped; this is not an account-wide cap.
    with run_lock(destination.parent), run_lock(destination):
        ledger = BudgetLedger(destination / "budget.json", maximum_usd)
        client = embedder or GeminiEmbedder(credentials.get("GEMINI_API_KEY", ""), dimensions=DIMENSIONS)
        cache_dir = destination / "vector-cache"
        cache_dir.mkdir(exist_ok=True)
        vectors = {"A": {}, "B": {}, "queries": {}}

        def request_key(data: bytes | None, text: str, task: str) -> str:
            return digest(json_bytes({"model": MODEL, "dimensions": DIMENSIONS,
                "image": digest(data) if data else None, "text": text, "task": task,
                "input_protocol": "embedding2-search-result-v1"}))

        requests = {}
        for item in manifest["items"]:
            for text in ("", item["embedding_prompt"]):
                key = request_key(images[item["id"]], text, "RETRIEVAL_DOCUMENT")
                requests[key] = IMAGE_RESERVE_USD + (TEXT_RESERVE_USD if text else 0)
        for query in annotations.get("queries", []):
            requests[request_key(None, query["text"], "RETRIEVAL_QUERY")] = TEXT_RESERVE_USD
        cached_keys = set()
        for key in requests:
            cache = cache_dir / f"{key}.json"
            if cache.exists():
                result = read_json(cache)
                if (result.get("key") != key or result.get("model") != MODEL
                        or len(result.get("vector", [])) != DIMENSIONS
                        or result.get("vector_sha256") != digest(json_bytes(result["vector"]))):
                    raise ValueError("cache identity or checksum mismatch")
                unit_prefix(result["vector"], DIMENSIONS)
                cached_keys.add(key)
        ledger.precheck(requests, cached_keys)

        def fetch(data: bytes | None, text: str, task: str) -> list[float]:
            key = request_key(data, text, task)
            cached = cache_dir / f"{key}.json"
            if cached.exists():
                result = read_json(cached)
                if result.get("key") != key or result.get("model") != MODEL or len(result.get("vector", [])) != DIMENSIONS:
                    raise ValueError("cache identity mismatch")
                return unit_prefix(result["vector"], DIMENSIONS)
            ledger.reserve(key, (IMAGE_RESERVE_USD if data else 0) + (TEXT_RESERVE_USD if text else 0))
            try:
                result = client.embed(image_bytes=data, mime_type="image/png" if data else None,
                    text=text, task_type=task)
                vector = unit_prefix(result["vector"], DIMENSIONS)
                write_json(cached, {"key": key, "model": MODEL, "vector": vector,
                    "vector_sha256": digest(json_bytes(vector)), "usage": result.get("usage", {})})
                ledger.finish(key, "completed", result.get("usage", {}))
            except Exception:
                ledger.finish(key, "failed_or_uncertain")
                raise
            sleep(1.0)  # Sequential bounded canary, no rate-limit chasing.
            return vector

        for item in manifest["items"]:
            vectors["A"][item["id"]] = fetch(images[item["id"]], "", "RETRIEVAL_DOCUMENT")
            vectors["B"][item["id"]] = fetch(images[item["id"]], item["embedding_prompt"], "RETRIEVAL_DOCUMENT")
        for query in annotations.get("queries", []):
            vectors["queries"][query["id"]] = fetch(None, query["text"], "RETRIEVAL_QUERY")
        write_json(destination / "vectors.json", vectors)
        write_json(destination / "executed-annotations.json", annotations)
        result = evaluate(manifest, annotations, vectors)
        result["budget"] = {"attempted_calls": len(ledger.data["attempts"]),
            "reserved_upper_bound_usd": sum(a["reserved_usd"] for a in ledger.data["attempts"]),
            "actual_invoice_cost_usd": None}
        write_json(destination / "evaluation.json", result)
        return result


def evaluate(manifest: dict, annotations: dict, vectors: dict) -> dict:
    from .similarity import compare_pair, cosine, rank, mmr, retrieval_metrics, build_visual_families

    validate_annotations(manifest, annotations, require_approval=False)
    evaluations = []
    for size in (768, 1536, 3072):
        for arm in ("A", "B"):
            corpus = {key: unit_prefix(value, size) for key, value in vectors[arm].items()}
            for query in annotations.get("queries", []):
                vector = unit_prefix(vectors["queries"][query["id"]], size)
                ranked = rank(vector, corpus, 5)
                diverse = mmr(vector, corpus, 5)
                metrics = {str(k): retrieval_metrics(ranked, query["relevance"], k)
                    for k in (1, 3, 5)} if query.get("human_judged") is True else None
                evaluations.append({"arm": arm, "dimensions": size, "query_id": query["id"],
                    "ranked": ranked, "diverse_mmr": diverse, "metrics": metrics,
                    "metrics_scope": "human_labeled_20_item_canary_only" if metrics else "not_human_judged"})
    pairs = []
    for a, b in itertools.combinations(manifest["items"], 2):
        pairs.append(compare_pair(a, b, image_cosine=cosine(vectors["A"][a["id"]], vectors["A"][b["id"]]),
            joint_cosine=cosine(vectors["B"][a["id"]], vectors["B"][b["id"]])))
    return {"status": "canary_embeddings_complete", "model": MODEL, "retrieval": evaluations,
        "pairs": pairs, "human_pair_labels": annotations.get("pairs", []),
        "visual_family_candidates": build_visual_families(vectors["A"]),
        "winner": None, "winner_reason": "small_canary_requires_human_review_and_separate_holdout",
        "automatic_merge": False, "qdrant_writes": 0, "public_release": False}
