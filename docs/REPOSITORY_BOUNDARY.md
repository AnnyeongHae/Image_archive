# Public Git Repository Boundary

The GitHub repository is a source-code and contract-test boundary. It is not the archive data lake.

## Tracked

- application and pipeline source code
- documentation and operational policies
- GitHub Actions workflows
- tests and validators
- small, redacted contract fixtures
- the explicit public encryption key (never its private pair), and small UI shell files

## Never tracked

- `.env` or credentials
- Neon connection strings or provider API keys
- `data/private-research/`
- raw prompts, remote-media caches, approval receipts, or collection runs
- canonical bulk JSONL, SQLite databases, legacy exports, or generated `dist/`
- `app/data/` local projections until every referenced item is P1/P2
- third-party image mirrors without item-level public rights clearance

The only private-research exception is the redacted GitHub collector registry and fixture under `data/private-research/github-sources/`. The workflow runs `qa/validate_repository_boundary.py` before any collector test.

The current v2 validator rejects all bulk `assets/`, `media/`, generated catalogs,
deployment receipts, private runtimes, archives, database files and reparse paths.
It scans JSONC/SQL and source text, limits each file to 2 MiB, and permits only
exact path-and-value synthetic credential fixtures. Public asset admission would
require a separate reviewed policy change; it is not a blanket exception.

`python -B qa/validate_repository_boundary.py --candidate-files` proposes an
explicit source list without staging. `--worktree --paths ...` reviews selected
working copies. The default command reads actual Git index blobs, so an older
staged secret cannot be hidden by a cleaned working copy. Preserve excluded local
files; exclusion is not deletion or loss of the 19,005-record source archive.

Archive records and originals belong in Neon/private R2 or an equivalent private store. Public WebP/JPEG/PNG delivery is a separate release-gated build.
