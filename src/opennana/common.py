from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Iterable


MODULE_DIR = Path(__file__).resolve().parent
ARCHIVE_ROOT = MODULE_DIR.parents[1]
DATA_ROOT = ARCHIVE_ROOT / "data" / "private-research" / "opennana"
DEFAULT_CANONICAL = ARCHIVE_ROOT / "data" / "canonical" / "archive_records.jsonl"
LEGACY_ROOT = ARCHIVE_ROOT / "legacy" / "current_archive"


def stable_json(value: Any, *, indent: int | None = 2) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=indent,
        separators=(",", ":") if indent is None else None,
    ) + ("\n" if indent is not None else "")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as handle:
        handle.write(text)
        temp_name = handle.name
    os.replace(temp_name, path)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, stable_json(value))


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.split("\n")]
    collapsed: list[str] = []
    previous_blank = False
    for line in lines:
        blank = not line
        if blank and previous_blank:
            continue
        collapsed.append(line)
        previous_blank = blank
    return "\n".join(collapsed).strip()


TOKEN_RE = re.compile(r"[a-z0-9]+|[가-힣]+|[\u3400-\u9fff]+|[\u3040-\u30ff]+", re.I)


def tokens(value: str) -> list[str]:
    return TOKEN_RE.findall(normalize_text(value).casefold())


def token_set(value: str) -> set[str]:
    return set(tokens(value))


def hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def simhash64(value: str) -> int:
    words = tokens(value)
    if not words:
        return 0
    features = words if len(words) < 3 else [" ".join(words[i : i + 3]) for i in range(len(words) - 2)]
    vector = [0] * 64
    for feature in features:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        number = int.from_bytes(digest, "big")
        for bit in range(64):
            vector[bit] += 1 if number & (1 << bit) else -1
    result = 0
    for bit, score in enumerate(vector):
        if score >= 0:
            result |= 1 << bit
    return result


PLACEHOLDER_PATTERNS = (
    re.compile(r"\[[^\]\n]{1,80}\]"),
    re.compile(r"\{[^}\n]{1,80}\}"),
    re.compile(r"<[^>\n]{1,80}>"),
    re.compile(r"(?:\"[^\"\n]{1,120}\"|'[^'\n]{1,120}')"),
    re.compile(r"\b\d+(?:[.:/-]\d+)*\b"),
)


def template_text(value: str) -> str:
    result = normalize_text(value).casefold()
    for pattern in PLACEHOLDER_PATTERNS:
        result = pattern.sub(" <var> ", result)
    result = re.sub(r"\s+", " ", result).strip()
    return result


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: JSON object required")
            yield value


def safe_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith("https://") or value.startswith("http://"):
        return value
    return None


def ensure_directories(root: Path = DATA_ROOT) -> None:
    for name in ("raw", "staging", "review_queue", "decisions", "runs"):
        (root / name).mkdir(parents=True, exist_ok=True)


def load_config(path: Path | None = None) -> dict[str, Any]:
    return read_json(path or DATA_ROOT / "config.json")


def source_id(value: dict[str, Any]) -> str:
    raw = value.get("id") or value.get("prompt_id") or value.get("slug")
    if raw is None:
        raise ValueError("OpenNana record is missing id/slug")
    return str(raw)


def prompt_parts(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        normalized = normalize_text(value)
        return [normalized] if normalized else []
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                text = normalize_text(item)
            elif isinstance(item, dict):
                candidate = item.get("prompt") or item.get("text") or item.get("content") or ""
                text = normalize_text(candidate) if isinstance(candidate, str) else ""
            else:
                text = ""
            if text:
                parts.append(text)
        return parts
    if isinstance(value, dict):
        candidate = value.get("prompt") or value.get("text") or value.get("content") or ""
        return [normalize_text(candidate)] if isinstance(candidate, str) and normalize_text(candidate) else []
    return []

