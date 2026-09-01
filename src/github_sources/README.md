# GitHub public-source canary

This lane discovers prompt-container and in-repository image candidates from an
explicit allowlist. It is not a general GitHub scraper and it never treats a
repository license as clearance for every prompt, image, logo, linked social
post, or user attachment.

Default offline validation:

```powershell
python src/github_sources/collect_public_repo.py
python -m unittest qa.test_github_source_collector -v
```

Bounded public API canary (read only):

```powershell
python src/github_sources/collect_public_repo.py `
  --repo freestylefly/awesome-gpt-image-2 `
  --fetch `
  --limit 100
```

Persisting the result additionally requires `--apply`; it writes only an
immutable artifact under `data/private-research/github-sources/runs/`. It does
not download blobs, edit canonical data, or publish media. Use the optional
`SOURCE_GITHUB_TOKEN` secret for a higher API allowance. Never place the token
in an artifact or command line.
