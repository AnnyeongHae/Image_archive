from __future__ import annotations

import argparse
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field

from .auth import AdminPrincipal, authenticate_admin
from .store import ArchiveStore


class ReviewDraftBody(BaseModel):
    decisions: list[dict[str, Any]] = Field(default_factory=list, max_length=5000)


def create_app(
    *,
    store: ArchiveStore | None = None,
    auth_dependency: Callable[..., AdminPrincipal] = authenticate_admin,
) -> FastAPI:
    archive = store or ArchiveStore()
    app = FastAPI(
        title="Image Prompt Archive API",
        version="0.1.0-canary",
        docs_url=None,
        redoc_url=None,
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, str]:
        try:
            archive.ready()
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database_not_ready") from exc
        return {"status": "ready"}

    @app.get("/api/public/v1/records")
    def public_records(
        q: str | None = Query(default=None, max_length=200),
        cursor: str | None = Query(default=None, max_length=1000),
        limit: int = Query(default=50, ge=1, le=50),
    ) -> dict[str, Any]:
        try:
            return archive.list_public(q=q, cursor=cursor, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.get("/api/public/v1/records/{catalog_key:path}")
    def public_record(catalog_key: str) -> dict[str, Any]:
        record = archive.get_public(catalog_key)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record_not_found")
        return record

    @app.get("/api/admin/v1/records")
    def admin_records(
        q: str | None = Query(default=None, max_length=200),
        cursor: str | None = Query(default=None, max_length=1000),
        limit: int = Query(default=50, ge=1, le=50),
        include_quarantine: bool = False,
        principal: AdminPrincipal = Depends(auth_dependency),
    ) -> dict[str, Any]:
        principal.require("archive:read")
        if include_quarantine:
            principal.require("quarantine:read")
        try:
            return archive.list_private(q=q, cursor=cursor, limit=limit, include_quarantine=include_quarantine)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.get("/api/admin/v1/records/{catalog_key:path}")
    def admin_record(
        catalog_key: str,
        principal: AdminPrincipal = Depends(auth_dependency),
    ) -> dict[str, Any]:
        principal.require("archive:read")
        record = archive.get_private(catalog_key)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record_not_found")
        if record.get("rights_tier") == "P4":
            principal.require("quarantine:read")
        return record

    @app.get("/api/admin/v1/review-drafts/{queue_revision}")
    def review_draft(
        queue_revision: str,
        principal: AdminPrincipal = Depends(auth_dependency),
    ) -> dict[str, Any]:
        principal.require("review:write")
        draft = archive.get_review_draft(subject=principal.subject, queue_revision=queue_revision)
        if draft is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="draft_not_found")
        return draft

    @app.put("/api/admin/v1/review-drafts/{queue_revision}")
    def save_review_draft(
        queue_revision: str,
        body: ReviewDraftBody,
        principal: AdminPrincipal = Depends(auth_dependency),
    ) -> dict[str, Any]:
        principal.require("review:write")
        try:
            return archive.put_review_draft(
                subject=principal.subject,
                queue_revision=queue_revision,
                decisions=body.decisions,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return app


app = create_app()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local archive API canary.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "::1"}:
        parser.error("the Python canary is loopback-only; production serving belongs behind Cloudflare Access")
    import uvicorn

    uvicorn.run("src.archive_platform.api:app", host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
