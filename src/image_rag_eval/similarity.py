from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from itertools import combinations
from pathlib import Path
from typing import Any

import imagehash
import numpy as np
from PIL import Image, ImageOps


PHASH_THRESHOLD = 8
DHASH_THRESHOLD = 8
ASPECT_RATIO_DELTA_THRESHOLD = 0.05
COLOR_HISTOGRAM_L1_THRESHOLD = 0.25
UNIT_NORM_TOLERANCE = 1e-3
VISUAL_FAMILY_K_CAP = 10
MAX_DECODE_PIXELS = 80_000_000
METRICS_MAX_SIDE = 256


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalized_prompt(text: str | None) -> str:
    if not text:
        return ""
    compact = re.sub(r"\s+", " ", text).strip().casefold()
    return compact


def _char_ngrams(text: str, n: int = 3) -> set[str]:
    if not text:
        return set()
    if len(text) < n:
        return {text}
    return {text[i : i + n] for i in range(len(text) - n + 1)}


def _jaccard(left: set[str], right: set[str]) -> float | None:
    if not left and not right:
        return None
    union = left | right
    if not union:
        return None
    return len(left & right) / len(union)


def _as_unit_vector(vector: Any) -> np.ndarray:
    arr = np.asarray(vector, dtype=np.float64)
    if arr.ndim != 1 or arr.size == 0:
        raise ValueError("vector must be a non-empty 1D array")
    if not np.all(np.isfinite(arr)):
        raise ValueError("vector must contain only finite values")
    norm = float(np.linalg.norm(arr))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("vector norm must be positive and finite")
    if abs(norm - 1.0) > UNIT_NORM_TOLERANCE:
        raise ValueError("vector must be unit-normalized")
    return arr


def cosine(a: Any, b: Any) -> float:
    left = _as_unit_vector(a)
    right = _as_unit_vector(b)
    if left.shape != right.shape:
        raise ValueError("vectors must have the same dimensionality")
    return float(np.dot(left, right))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decoded_rgba_image(path: Path) -> Image.Image:
    with Image.open(path) as image:
        width, height = image.size
        if width <= 0 or height <= 0:
            raise ValueError("image dimensions must be positive")
        if width * height > MAX_DECODE_PIXELS:
            raise ValueError("image exceeds decode pixel guard")
        if int(getattr(image, "n_frames", 1)) != 1:
            raise ValueError("multi-frame images are unsupported")
        normalized = ImageOps.exif_transpose(image).convert("RGBA")
        return normalized.copy()


def _pixel_sha256(image: Image.Image) -> str:
    width, height = image.size
    payload = image.mode.encode("ascii") + width.to_bytes(4, "big") + height.to_bytes(4, "big") + image.tobytes()
    return _sha256_bytes(payload)


def _color_histogram(image: Image.Image, bins: int = 8) -> list[float]:
    arr = np.asarray(image, dtype=np.uint8)
    histogram: list[float] = []
    for channel in range(3):
        counts, _ = np.histogram(arr[:, :, channel], bins=bins, range=(0, 256))
        histogram.extend(counts.astype(np.float64).tolist())
    total = float(arr.shape[0] * arr.shape[1] * 3)
    if total <= 0:
        raise ValueError("image has no pixels")
    return [round(value / total, 8) for value in histogram]


def _low_information(image: Image.Image) -> bool:
    arr = np.asarray(image, dtype=np.uint8)
    gray = arr.mean(axis=2)
    grayscale_std = float(gray.std())
    unique_colors = int(np.unique(arr.reshape(-1, 3), axis=0).shape[0])
    return grayscale_std < 4.0 or unique_colors <= 4


def _metric_rgb_image(identity_image: Image.Image, max_side: int = METRICS_MAX_SIDE) -> Image.Image:
    white = Image.new("RGBA", identity_image.size, (255, 255, 255, 255))
    composited = Image.alpha_composite(white, identity_image).convert("RGB")
    width, height = composited.size
    longest = max(width, height)
    if longest <= max_side:
        return composited
    scale = max_side / float(longest)
    resized = composited.resize(
        (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
        Image.Resampling.LANCZOS,
    )
    return resized


def image_signals(path: Path) -> dict[str, Any]:
    identity_image = _decoded_rgba_image(path)
    original_width, original_height = identity_image.size
    metric_image = _metric_rgb_image(identity_image)
    return {
        "sha256": _file_sha256(path),
        "pixel_sha256": _pixel_sha256(identity_image),
        "phash": str(imagehash.phash(metric_image, hash_size=8, highfreq_factor=4)),
        "dhash": str(imagehash.dhash(metric_image, hash_size=8)),
        "width": int(original_width),
        "height": int(original_height),
        "color_histogram": _color_histogram(metric_image),
        "low_information": _low_information(metric_image),
        "metrics_max_side": METRICS_MAX_SIDE,
    }


def prompt_signals(text: str | None) -> dict[str, Any]:
    exact_text = text or ""
    normalized_text = _normalized_prompt(text)
    return {
        "exact_sha256": _sha256_bytes(exact_text.encode("utf-8")),
        "normalized_sha256": _sha256_bytes(normalized_text.encode("utf-8")),
        "normalized_text": normalized_text,
        "has_text": bool(normalized_text),
    }


def _hamming_hex(left: str | None, right: str | None) -> int | None:
    if not left or not right:
        return None
    return (int(left, 16) ^ int(right, 16)).bit_count()


def _aspect_ratio_delta(a: dict[str, Any], b: dict[str, Any]) -> float | None:
    aw, ah, bw, bh = a.get("width"), a.get("height"), b.get("width"), b.get("height")
    if not all(isinstance(v, int) and v > 0 for v in (aw, ah, bw, bh)):
        return None
    left = aw / ah
    right = bw / bh
    return abs(left - right) / max(left, right)


def _color_histogram_l1(a: dict[str, Any], b: dict[str, Any]) -> float | None:
    left = a.get("color_histogram")
    right = b.get("color_histogram")
    if not isinstance(left, list) or not isinstance(right, list) or len(left) != len(right):
        return None
    return float(sum(abs(float(l) - float(r)) for l, r in zip(left, right)))


def _validated_similarity(name: str, value: float | None, evidence_flags: list[str]) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value < -1.0 or value > 1.0:
        evidence_flags.append(f"invalid_{name}")
        return None
    return round(float(value), 6)


def compare_pair(
    a: dict[str, Any],
    b: dict[str, Any],
    image_cosine: float | None = None,
    joint_cosine: float | None = None,
) -> dict[str, Any]:
    left_signals = a.get("signals", {})
    right_signals = b.get("signals", {})
    left_prompt = prompt_signals(a.get("prompt"))
    right_prompt = prompt_signals(b.get("prompt"))
    evidence_flags = [
        "phash_distance_is_not_semantic_probability",
        "perceptual_thresholds_are_candidate_only",
    ]

    phash_distance = _hamming_hex(left_signals.get("phash"), right_signals.get("phash"))
    dhash_distance = _hamming_hex(left_signals.get("dhash"), right_signals.get("dhash"))
    aspect_ratio_delta = _aspect_ratio_delta(left_signals, right_signals)
    color_histogram_l1 = _color_histogram_l1(left_signals, right_signals)
    prompt_jaccard = _jaccard(
        _char_ngrams(left_prompt["normalized_text"]),
        _char_ngrams(right_prompt["normalized_text"]),
    )
    image_cosine = _validated_similarity("image_cosine", image_cosine, evidence_flags)
    joint_cosine = _validated_similarity("joint_cosine", joint_cosine, evidence_flags)

    exact_file = left_signals.get("sha256") and left_signals.get("sha256") == right_signals.get("sha256")
    exact_pixels = left_signals.get("pixel_sha256") and (
        left_signals.get("pixel_sha256") == right_signals.get("pixel_sha256")
    )
    prompt_exact = left_prompt["has_text"] and right_prompt["has_text"] and left_prompt["exact_sha256"] == right_prompt["exact_sha256"]
    prompt_normalized_match = (
        left_prompt["has_text"]
        and right_prompt["has_text"]
        and left_prompt["normalized_sha256"] == right_prompt["normalized_sha256"]
    )
    low_information_pair = bool(left_signals.get("low_information")) or bool(right_signals.get("low_information"))
    if low_information_pair:
        evidence_flags.append("low_information_blocks_near_copy")

    perceptual_gate = (
        not low_information_pair
        and phash_distance is not None
        and dhash_distance is not None
        and phash_distance <= PHASH_THRESHOLD
        and dhash_distance <= DHASH_THRESHOLD
        and aspect_ratio_delta is not None
        and aspect_ratio_delta <= ASPECT_RATIO_DELTA_THRESHOLD
        and color_histogram_l1 is not None
        and color_histogram_l1 <= COLOR_HISTOGRAM_L1_THRESHOLD
    )

    if exact_file:
        candidate_relation = "exact_file"
    elif exact_pixels:
        candidate_relation = "exact_pixels"
    elif perceptual_gate:
        candidate_relation = "near_copy_candidate"
    elif image_cosine is not None and image_cosine >= 0.9:
        candidate_relation = "semantic_related_candidate"
    elif joint_cosine is not None and joint_cosine >= 0.9:
        candidate_relation = "semantic_related_candidate"
    elif prompt_exact or prompt_normalized_match or (prompt_jaccard is not None and prompt_jaccard >= 0.8):
        candidate_relation = "manual_candidate"
    else:
        candidate_relation = "no_match_by_current_signals"

    return {
        "left_id": a.get("id"),
        "right_id": b.get("id"),
        "phash_hamming": phash_distance,
        "dhash_hamming": dhash_distance,
        "aspect_ratio_delta": None if aspect_ratio_delta is None else round(aspect_ratio_delta, 6),
        "color_histogram_l1": None if color_histogram_l1 is None else round(color_histogram_l1, 6),
        "image_cosine": image_cosine,
        "joint_cosine": joint_cosine,
        "prompt_exact": prompt_exact,
        "prompt_normalized_match": prompt_normalized_match,
        "normalized_prompt_char3gram_jaccard": None if prompt_jaccard is None else round(prompt_jaccard, 6),
        "candidate_relation": candidate_relation,
        "evidence_flags": sorted(set(evidence_flags)),
    }


def _group_id(kind: str, member_ids: list[str]) -> str:
    material = f"{kind}\0" + "\0".join(sorted(member_ids))
    return f"{kind}-{_sha256_bytes(material.encode('utf-8'))[:24]}"


def _item_id(item: dict[str, Any]) -> str:
    item_id = item.get("id")
    if not isinstance(item_id, str) or not item_id:
        raise ValueError("item id is required")
    return item_id


def _signal_group(items: list[dict[str, Any]], signal_key: str, kind: str, *, status: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for item in items:
        signal = item.get("signals", {}).get(signal_key)
        if isinstance(signal, str) and signal:
            grouped[signal].append(_item_id(item))
    results = []
    for signal, members in sorted(grouped.items()):
        if len(members) < 2:
            continue
        member_ids = sorted(members)
        results.append(
            {
                "group_id": _group_id(kind, member_ids),
                "kind": kind,
                "status": status,
                "member_ids": member_ids,
                "evidence": {signal_key: signal},
            }
        )
    return results


def _prompt_exact_groups(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for item in items:
        prompt_info = prompt_signals(item.get("prompt"))
        prompt_sha = prompt_info.get("exact_sha256")
        if prompt_sha and prompt_info.get("has_text"):
            grouped[prompt_sha].append(_item_id(item))
    results = []
    for prompt_sha, members in sorted(grouped.items()):
        if len(members) < 2:
            continue
        member_ids = sorted(members)
        results.append(
            {
                "group_id": _group_id("prompt_exact", member_ids),
                "kind": "prompt_exact",
                "status": "observed",
                "member_ids": member_ids,
                "evidence": {"prompt_exact_sha256": prompt_sha},
            }
        )
    return results


def _near_copy_cliques(pairs: list[dict[str, Any]]) -> list[list[str]]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    pair_index: dict[frozenset[str], dict[str, Any]] = {}
    for pair in pairs:
        if pair.get("candidate_relation") != "near_copy_candidate":
            continue
        left = pair.get("left_id")
        right = pair.get("right_id")
        if not isinstance(left, str) or not isinstance(right, str) or left == right:
            continue
        adjacency[left].add(right)
        adjacency[right].add(left)
        pair_index[frozenset((left, right))] = pair

    cliques: list[list[str]] = []

    def bronk(r: set[str], p: set[str], x: set[str]) -> None:
        if not p and not x:
            if len(r) >= 2:
                clique = sorted(r)
                if all(frozenset(edge) in pair_index for edge in combinations(clique, 2)):
                    cliques.append(clique)
            return
        pivot = next(iter(p | x)) if p or x else None
        candidates = p - (adjacency.get(pivot, set()) if pivot else set())
        for vertex in sorted(candidates):
            bronk(r | {vertex}, p & adjacency[vertex], x & adjacency[vertex])
            p.remove(vertex)
            x.add(vertex)

    bronk(set(), set(adjacency), set())
    unique = sorted({tuple(clique) for clique in cliques}, key=lambda c: (len(c), c))
    return [list(clique) for clique in unique]


def build_groups(items: list[dict[str, Any]], pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    groups.extend(_signal_group(items, "sha256", "exact_file", status="observed"))
    groups.extend(_signal_group(items, "pixel_sha256", "exact_pixels", status="observed"))
    groups.extend(_prompt_exact_groups(items))

    pair_lookup = {frozenset((pair["left_id"], pair["right_id"])): pair for pair in pairs if pair.get("left_id") and pair.get("right_id")}
    for member_ids in _near_copy_cliques(pairs):
        supporting_pairs = []
        for left, right in combinations(member_ids, 2):
            pair = pair_lookup[frozenset((left, right))]
            supporting_pairs.append(
                {
                    "left_id": left,
                    "right_id": right,
                    "phash_hamming": pair.get("phash_hamming"),
                    "dhash_hamming": pair.get("dhash_hamming"),
                }
            )
        groups.append(
            {
                "group_id": _group_id("near_copy_candidate", member_ids),
                "kind": "near_copy_candidate",
                "status": "needs_review",
                "member_ids": member_ids,
                "evidence": {"complete_link_pairs": supporting_pairs},
            }
        )

    groups.sort(key=lambda item: (item["kind"], item["group_id"]))
    return groups


def build_visual_families(
    vectors_by_id: dict[str, Any],
    k: int = 3,
    min_cosine: float = 0.85,
) -> list[dict[str, Any]]:
    if not 1 <= k <= VISUAL_FAMILY_K_CAP:
        raise ValueError(f"k must be between 1 and {VISUAL_FAMILY_K_CAP}")
    if not isinstance(min_cosine, (int, float)) or not math.isfinite(min_cosine) or min_cosine < -1.0 or min_cosine > 1.0:
        raise ValueError("min_cosine must be a finite similarity threshold between -1 and 1")

    normalized = {item_id: _as_unit_vector(vector) for item_id, vector in vectors_by_id.items()}
    if len(normalized) < 2:
        return []

    neighbors: dict[str, set[str]] = {}
    pair_scores: dict[frozenset[str], float] = {}
    for left_id, left_vec in normalized.items():
        scored: list[tuple[float, str]] = []
        for right_id, right_vec in normalized.items():
            if left_id == right_id:
                continue
            score = cosine(left_vec, right_vec)
            pair_scores[frozenset((left_id, right_id))] = score
            if score >= min_cosine:
                scored.append((score, right_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        neighbors[left_id] = {item_id for _, item_id in scored[:k]}

    candidate_pairs = []
    for left_id, right_id in combinations(sorted(normalized), 2):
        score = pair_scores.get(frozenset((left_id, right_id)))
        if score is None or score < min_cosine:
            continue
        if right_id in neighbors.get(left_id, set()) and left_id in neighbors.get(right_id, set()):
            candidate_pairs.append(
                {
                    "left_id": left_id,
                    "right_id": right_id,
                    "candidate_relation": "visual_family_candidate",
                    "image_cosine": round(score, 6),
                }
            )

    cliques = _near_copy_cliques(
        [
            {**pair, "candidate_relation": "near_copy_candidate"}
            for pair in candidate_pairs
        ]
    )
    families = []
    pair_lookup = {
        frozenset((pair["left_id"], pair["right_id"])): pair
        for pair in candidate_pairs
    }
    for member_ids in cliques:
        families.append(
            {
                "group_id": _group_id("visual_family_candidate", member_ids),
                "kind": "visual_family_candidate",
                "status": "needs_review",
                "soft_collection": True,
                "threshold_calibrated": False,
                "automatic_merge": False,
                "member_ids": member_ids,
                "evidence": {
                    "method": "image_only_embedding_mutual_knn_complete_link",
                    "k": k,
                    "min_cosine_hypothesis": round(float(min_cosine), 6),
                    "pair_cosines": [
                        {
                            "left_id": left,
                            "right_id": right,
                            "image_cosine": round(pair_lookup[frozenset((left, right))]["image_cosine"], 6),
                        }
                        for left, right in combinations(member_ids, 2)
                    ],
                },
            }
        )
    families.sort(key=lambda item: (item["kind"], item["group_id"]))
    return families


def rank(vector: Any, vectors_by_id: dict[str, Any], k: int) -> list[dict[str, float]]:
    query = _as_unit_vector(vector)
    if k < 1:
        raise ValueError("k must be >= 1")
    scored = []
    for item_id, candidate in sorted(vectors_by_id.items()):
        score = cosine(query, candidate)
        scored.append({"id": item_id, "score": round(score, 6)})
    scored.sort(key=lambda item: (-item["score"], item["id"]))
    return scored[:k]


def mmr(vector: Any, vectors_by_id: dict[str, Any], k: int, lambda_: float = 0.7) -> list[dict[str, float]]:
    query = _as_unit_vector(vector)
    if not 0.0 <= lambda_ <= 1.0:
        raise ValueError("lambda_ must be between 0 and 1")
    if k < 1:
        raise ValueError("k must be >= 1")
    all_vectors = {item_id: _as_unit_vector(candidate) for item_id, candidate in vectors_by_id.items()}
    candidates = dict(all_vectors)
    selected: list[str] = []
    while candidates and len(selected) < k:
        best_id = None
        best_score = None
        for item_id in sorted(candidates):
            relevance = cosine(query, candidates[item_id])
            diversity_penalty = max((cosine(candidates[item_id], all_vectors[chosen]) for chosen in selected), default=0.0)
            mmr_score = lambda_ * relevance - (1.0 - lambda_) * diversity_penalty
            if best_score is None or mmr_score > best_score or (mmr_score == best_score and item_id < best_id):
                best_id = item_id
                best_score = mmr_score
        assert best_id is not None and best_score is not None
        selected.append(best_id)
        candidates.pop(best_id)
    return [{"id": item_id, "score": round(cosine(query, all_vectors[item_id]), 6)} for item_id in selected]


def retrieval_metrics(ranked: list[dict[str, Any]], relevance: dict[str, int | None], k: int) -> dict[str, Any]:
    top_k = ranked[:k]
    unjudged = [item["id"] for item in top_k if item.get("id") not in relevance or relevance.get(item["id"]) is None]
    labels_complete = len(unjudged) == 0
    positives = [item_id for item_id, rel in relevance.items() if isinstance(rel, int) and rel > 0]
    if not labels_complete or not positives:
        return {
            "k": k,
            "labels_complete": labels_complete,
            "unjudged_ids": unjudged,
            "recall": None,
            "ndcg": None,
            "mrr": None,
            "hit": None,
        }

    gains = [int(relevance[item["id"]]) for item in top_k]
    hit = 1.0 if any(gain > 0 for gain in gains) else 0.0
    retrieved_positive = sum(1 for gain in gains if gain > 0)
    recall = retrieved_positive / len(positives)

    dcg = 0.0
    for index, gain in enumerate(gains, start=1):
        if gain > 0:
            dcg += (2**gain - 1) / math.log2(index + 1)
    ideal = sorted((int(rel) for rel in relevance.values() if isinstance(rel, int) and rel > 0), reverse=True)[:k]
    idcg = 0.0
    for index, gain in enumerate(ideal, start=1):
        idcg += (2**gain - 1) / math.log2(index + 1)
    mrr = 0.0
    for index, gain in enumerate(gains, start=1):
        if gain > 0:
            mrr = 1.0 / index
            break

    return {
        "k": k,
        "labels_complete": True,
        "unjudged_ids": [],
        "recall": round(recall, 6),
        "ndcg": round(dcg / idcg, 6) if idcg > 0 else None,
        "mrr": round(mrr, 6),
        "hit": round(hit, 6),
    }
