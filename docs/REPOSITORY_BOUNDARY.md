# Public Git Repository Boundary

The GitHub repository is a source-code and contract-test boundary. It is not the archive data lake.

## Tracked

- application and pipeline source code
- documentation and operational policies
- GitHub Actions workflows
- tests and validators
- small, redacted contract fixtures
- intentionally curated public demo assets only

## Never tracked

- `.env` or credentials
- Neon connection strings or provider API keys
- `data/private-research/`
- raw prompts, remote-media caches, approval receipts, or collection runs
- canonical bulk JSONL, SQLite databases, legacy exports, or generated `dist/`
- `app/data/` local projections until every referenced item is P1/P2
- third-party image mirrors without item-level public rights clearance

The only private-research exception is the redacted GitHub collector registry and fixture under `data/private-research/github-sources/`. The workflow runs `qa/validate_repository_boundary.py` before any collector test.

Archive records and originals belong in Neon/private R2 or an equivalent private store. Public WebP/JPEG/PNG delivery is a separate release-gated build.
