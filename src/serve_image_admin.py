"""Start a local-only image administrator; no network or writes without --serve."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from image_rag_eval.admin_server import AdminHTTPServer, media_map
from image_rag_eval.incremental_workflow import load_frozen_workflow


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--seed-decisions", type=Path)
    parser.add_argument("--port", type=int, default=8964)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be 0..65535; 0 selects a free local port")
    root = Path(__file__).resolve().parents[1]
    private_dir = (root / "data/private-research/image-rag-admin").resolve()
    if not private_dir.is_relative_to(root):
        parser.error("private administrator directory must remain inside the archive")
    db_path = (args.db or private_dir / "state.sqlite3").resolve()
    if not db_path.is_relative_to(private_dir) or db_path.suffix != ".sqlite3":
        parser.error("DB must be a .sqlite3 file inside data/private-research/image-rag-admin")
    spec = load_frozen_workflow(root, args.run_id)
    seed = None
    if args.seed_decisions:
        raw = args.seed_decisions.read_bytes()
        seed = json.loads(raw.decode("utf-8-sig"))
        from image_rag_eval.group_workflow import validate_group_workflow_decisions
        normalized = validate_group_workflow_decisions(spec, seed)
        if normalized["private_front_export_status"] != "ready":
            parser.error("seed must be a completed user approval")
    if not args.serve:
        print(json.dumps({"status": "dry_run", "run_id": args.run_id, "database": str(db_path),
                          "port": args.port, "provider_calls": 0, "writes": 0,
                          "seed_approved_images": len(normalized["private_front_export_items"]) if seed else None}, ensure_ascii=False))
        return
    from image_rag_eval.admin_store import AdminStore
    from image_rag_eval.rights import build_rights_catalog
    from image_rag_eval.approval_handoff import prepare_admin_handoff
    from image_rag_eval.approved_library import build_prompt_catalog
    # Bind before initializing persistent state; an occupied port is not killed.
    server = AdminHTTPServer(("127.0.0.1", args.port), store=None, static_dir=root / "app/image-admin",
                             media=media_map(root, args.run_id, spec),
                             validate_source=lambda: load_frozen_workflow(root, args.run_id),
                             rights_catalog=build_rights_catalog(root, spec),
                             prompt_catalog=build_prompt_catalog(root, spec),
                             prepare_handoff=lambda commit_id: prepare_admin_handoff(
                                 root, db_path, args.run_id, apply=True, expected_commit_id=commit_id))
    try:
        server.store = AdminStore(db_path, spec, seed_decisions=seed,
                                  validate_source=lambda: load_frozen_workflow(root, args.run_id))
        server.prepare_committed(server.store.state(), allow_saved_draft=True)
        print(json.dumps({"status": "serving", "url": server.origin + "/", "run_id": args.run_id,
                          "database": str(db_path), "local_only": True, "provider_calls": 0}, ensure_ascii=False), flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
