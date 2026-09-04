from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PUBLIC_INDEX_RELATIVE = Path("data") / "public-export" / "catalog-index.json"
PUBLIC_SHARDS_RELATIVE = Path("data") / "public-export" / "shards"
DEFAULT_LIMIT = 50
MAX_LIMIT = 100


class PublicCatalogUnavailable(RuntimeError):
    pass


class PublicCatalogRecordNotFound(RuntimeError):
    pass


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise PublicCatalogUnavailable(f"expected JSON object: {path}")
    return payload


def _tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+|[가-힣]+", str(value or "").casefold())


@dataclass
class PublicCatalogStore:
    archive_root: Path
    _index_cache: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _index_mtime_ns: int | None = field(default=None, init=False, repr=False)
    _shard_cache: dict[str, tuple[int, dict[str, Any]]] = field(default_factory=dict, init=False, repr=False)

    @property
    def index_path(self) -> Path:
        return self.archive_root / PUBLIC_INDEX_RELATIVE

    @property
    def shards_dir(self) -> Path:
        return self.archive_root / PUBLIC_SHARDS_RELATIVE

    def summary(self) -> dict[str, Any]:
        index = self._index()
        return {
            "schema_version": "public-catalog-summary-1.0",
            "generated_at": index.get("generated_at"),
            "record_count": int(index.get("record_count") or 0),
            "canonical_record_count": int(index.get("canonical_record_count") or 0),
            "shard_count": int(index.get("shard_count") or 0),
            "style_id_count": int(index.get("style_id_count") or 0),
            "prompt_text_included_count": int(index.get("prompt_text_included_count") or 0),
            "media_asset_included_count": int(index.get("media_asset_included_count") or 0),
            "rights_policy": index.get("rights_policy") or {},
        }

    def search(
        self,
        *,
        q: str | None = None,
        source_name: str | None = None,
        lane: str | None = None,
        rights_tier: str | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        if limit < 1 or limit > MAX_LIMIT:
            raise ValueError(f"limit must be 1..{MAX_LIMIT}")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        rows = list(self._records())
        query_tokens = _tokens(q or "")
        filtered: list[dict[str, Any]] = []
        for row in rows:
            if source_name and str(row.get("source_name") or "").casefold() != source_name.casefold():
                continue
            if lane and str(row.get("lane") or "").casefold() != lane.casefold():
                continue
            if rights_tier and str(row.get("rights_tier") or "").casefold() != rights_tier.casefold():
                continue
            haystack = " ".join(
                [
                    str(row.get("style_id") or ""),
                    str(row.get("title") or ""),
                    str(row.get("source_name") or ""),
                    str(row.get("search_text") or ""),
                ]
            ).casefold()
            if query_tokens and not all(token in haystack for token in query_tokens):
                continue
            filtered.append(row)
        page = filtered[offset : offset + limit]
        return {
            "schema_version": "public-catalog-search-1.0",
            "q": q or "",
            "filters": {
                "source_name": source_name,
                "lane": lane,
                "rights_tier": rights_tier,
            },
            "offset": offset,
            "limit": limit,
            "total": len(filtered),
            "records": page,
        }

    def record(self, style_id: str) -> dict[str, Any]:
        wanted = str(style_id or "").strip()
        if not wanted:
            raise PublicCatalogRecordNotFound("style id is required")
        row = next((item for item in self._records() if str(item.get("style_id") or "") == wanted), None)
        if row is None:
            raise PublicCatalogRecordNotFound(wanted)
        shard_id = str(row.get("shard_id") or "").strip()
        if not shard_id:
            raise PublicCatalogRecordNotFound(wanted)
        shard = self._shard(shard_id)
        for record in shard.get("records") or []:
            if str(record.get("style_id") or "") == wanted:
                return {
                    "schema_version": "public-catalog-record-response-1.0",
                    "record": record,
                }
        raise PublicCatalogRecordNotFound(wanted)

    def _index(self) -> dict[str, Any]:
        path = self.index_path
        if not path.is_file():
            raise PublicCatalogUnavailable("public catalog index is unavailable")
        stat = path.stat()
        if self._index_cache is None or self._index_mtime_ns != stat.st_mtime_ns:
            payload = _load_json(path)
            if not isinstance(payload.get("records"), list):
                raise PublicCatalogUnavailable("public catalog index is malformed")
            self._index_cache = payload
            self._index_mtime_ns = stat.st_mtime_ns
            self._shard_cache.clear()
        return self._index_cache

    def _records(self) -> list[dict[str, Any]]:
        records = self._index().get("records")
        return records if isinstance(records, list) else []

    def _shard(self, shard_id: str) -> dict[str, Any]:
        filename = f"{shard_id}.json"
        path = self.shards_dir / filename
        if not path.is_file():
            raise PublicCatalogUnavailable(f"public shard missing: {filename}")
        stat = path.stat()
        cached = self._shard_cache.get(shard_id)
        if cached and cached[0] == stat.st_mtime_ns:
            return cached[1]
        payload = _load_json(path)
        if payload.get("shard_id") != shard_id or not isinstance(payload.get("records"), list):
            raise PublicCatalogUnavailable(f"public shard malformed: {filename}")
        self._shard_cache[shard_id] = (stat.st_mtime_ns, payload)
        return payload
