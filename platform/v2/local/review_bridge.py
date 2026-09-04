"""Build source-neutral local admin review from a pinned Actions import.

Default: read-only dry-run. --apply writes a NEW frozen run only. No models,
downloads, cloud writes, seeded human approvals or automatic publication.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from image_rag_eval.intake_review import build_intake_review


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--import-receipt", required=True)
    parser.add_argument("--import-receipt-sha256", required=True)
    parser.add_argument("--media-bindings", required=True)
    parser.add_argument("--baseline-run-id", required=True)
    parser.add_argument("--review-run-id", required=True)
    parser.add_argument("--db", type=Path, default=ROOT / "data/private-research/image-rag-admin/state.sqlite3")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    db = args.db.resolve(strict=True)
    if not db.is_relative_to((ROOT / "data/private-research/image-rag-admin").resolve()) or db.suffix != ".sqlite3":
        raise ValueError("dedicated_local_administrator_database_required")
    result = build_intake_review(ROOT, import_receipt=args.import_receipt,
        import_receipt_sha256=args.import_receipt_sha256, media_bindings=args.media_bindings,
        baseline_run_id=args.baseline_run_id, db_path=db, review_run_id=args.review_run_id, apply=args.apply)
    print(json.dumps(result, ensure_ascii=True))
    return 2 if result["status"].startswith("blocked") else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, TypeError, OSError):
        print('{"status":"blocked","reason":"intake_review_evidence_gate_failed"}', file=sys.stderr)
        raise SystemExit(2)
