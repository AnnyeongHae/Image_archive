-- Private immutable projection only. It is NOT the approval or public search DB.
PRAGMA foreign_keys = ON;
CREATE TABLE snapshot (
  id INTEGER PRIMARY KEY CHECK(id=1),
  schema_version TEXT NOT NULL CHECK(schema_version='luna-candidate-store-1'),
  source_sha256 TEXT NOT NULL, migration_sha256 TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status='needs_review'),
  release_eligible INTEGER NOT NULL CHECK(release_eligible=0),
  public_search_eligible INTEGER NOT NULL CHECK(public_search_eligible=0)
);
CREATE TABLE assets (
  sha256 TEXT PRIMARY KEY CHECK(length(sha256)=64)
);
CREATE TABLE asset_locations (
  sha256 TEXT NOT NULL REFERENCES assets(sha256),
  relative_path TEXT NOT NULL, role TEXT NOT NULL CHECK(role='prepared_image'),
  PRIMARY KEY(sha256, relative_path)
);
CREATE TABLE prompts (
  sha256 TEXT PRIMARY KEY CHECK(length(sha256)=64), original_text TEXT NOT NULL
);
CREATE TABLE approval_groups (
  source_run_id TEXT NOT NULL, source_commit_id TEXT NOT NULL,
  group_id TEXT NOT NULL, representative_item_id TEXT NOT NULL,
  member_count INTEGER NOT NULL CHECK(member_count>1),
  PRIMARY KEY(source_run_id, source_commit_id, group_id)
);
CREATE TABLE items (
  item_id TEXT PRIMARY KEY, style_id TEXT NOT NULL UNIQUE,
  source_image_sha256 TEXT NOT NULL REFERENCES assets(sha256),
  prepared_image_sha256 TEXT NOT NULL REFERENCES assets(sha256),
  prompt_sha256 TEXT NOT NULL REFERENCES prompts(sha256),
  source_run_id TEXT NOT NULL, source_commit_id TEXT NOT NULL, group_id TEXT,
  is_group_representative INTEGER NOT NULL CHECK(is_group_representative IN(0,1)),
  rights_json TEXT NOT NULL CHECK(json_valid(rights_json)),
  FOREIGN KEY(source_run_id, source_commit_id, group_id)
    REFERENCES approval_groups(source_run_id, source_commit_id, group_id)
);
CREATE TABLE taxonomy_versions (
  sha256 TEXT PRIMARY KEY, schema_version TEXT NOT NULL,
  source_relative_path TEXT NOT NULL, raw_json TEXT NOT NULL CHECK(json_valid(raw_json)),
  status TEXT NOT NULL CHECK(status='proposal_needs_review')
);
CREATE TABLE taxonomy_terms (
  taxonomy_sha256 TEXT NOT NULL REFERENCES taxonomy_versions(sha256),
  facet TEXT NOT NULL CHECK(facet IN('use_case','asset_format')),
  term_id TEXT NOT NULL, label_ko TEXT NOT NULL,
  definition_json TEXT NOT NULL CHECK(json_valid(definition_json)),
  PRIMARY KEY(taxonomy_sha256, facet, term_id)
);
CREATE TABLE taxonomy_aliases (
  taxonomy_sha256 TEXT NOT NULL, facet TEXT NOT NULL, term_id TEXT NOT NULL,
  term TEXT NOT NULL, normalized_term TEXT NOT NULL, origin TEXT NOT NULL,
  PRIMARY KEY(taxonomy_sha256, facet, term_id, normalized_term),
  FOREIGN KEY(taxonomy_sha256, facet, term_id) REFERENCES taxonomy_terms(taxonomy_sha256, facet, term_id)
);
CREATE INDEX alias_lookup ON taxonomy_aliases(normalized_term);
CREATE TABLE analysis_runs (
  run_id TEXT PRIMARY KEY, result_schema_version TEXT NOT NULL,
  task_manifest_sha256 TEXT NOT NULL, validated_results_sha256 TEXT NOT NULL,
  import_receipt_sha256 TEXT NOT NULL, model TEXT NOT NULL,
  source_run_id TEXT NOT NULL, source_commit_id TEXT NOT NULL,
  taxonomy_sha256 TEXT REFERENCES taxonomy_versions(sha256),
  manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),
  execution_json TEXT NOT NULL CHECK(json_valid(execution_json))
);
CREATE TABLE run_usage (
  run_id TEXT PRIMARY KEY REFERENCES analysis_runs(run_id),
  receipt_sha256 TEXT NOT NULL, scope TEXT NOT NULL, evidence_status TEXT NOT NULL,
  input_including_cached INTEGER NOT NULL CHECK(input_including_cached>=0),
  cached_input INTEGER NOT NULL CHECK(cached_input>=0 AND cached_input<=input_including_cached),
  uncached_input INTEGER NOT NULL CHECK(uncached_input=input_including_cached-cached_input),
  output_including_reasoning INTEGER NOT NULL CHECK(output_including_reasoning>=0),
  reasoning_output INTEGER NOT NULL CHECK(reasoning_output>=0 AND reasoning_output<=output_including_reasoning),
  total_tokens INTEGER NOT NULL CHECK(total_tokens=input_including_cached+output_including_reasoning),
  actual_billed_tokens INTEGER CHECK(actual_billed_tokens IS NULL),
  actual_billed_cost REAL CHECK(actual_billed_cost IS NULL),
  raw_json TEXT NOT NULL CHECK(json_valid(raw_json))
);
CREATE TABLE candidates (
  candidate_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES analysis_runs(run_id),
  task_id TEXT NOT NULL, item_id TEXT NOT NULL REFERENCES items(item_id),
  input_fingerprint TEXT NOT NULL, result_version TEXT NOT NULL,
  raw_result_sha256 TEXT NOT NULL, raw_json TEXT NOT NULL CHECK(json_valid(raw_json)),
  visual_json TEXT NOT NULL CHECK(json_valid(visual_json)),
  prompt_analysis_json TEXT NOT NULL CHECK(json_valid(prompt_analysis_json)),
  freeform_usage_json TEXT CHECK(freeform_usage_json IS NULL OR json_valid(freeform_usage_json)),
  review_status TEXT NOT NULL CHECK(review_status='needs_review'),
  metadata_human_approved INTEGER NOT NULL CHECK(metadata_human_approved=0),
  release_eligible INTEGER NOT NULL CHECK(release_eligible=0),
  public_search_eligible INTEGER NOT NULL CHECK(public_search_eligible=0),
  UNIQUE(run_id, task_id, result_version)
);
CREATE TABLE usage_assignments (
  candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 2),
  taxonomy_sha256 TEXT NOT NULL, facet TEXT NOT NULL CHECK(facet='use_case'),
  use_case_id TEXT NOT NULL,
  fit TEXT NOT NULL CHECK(fit IN('supported','conditional','speculative')),
  reuse_mode TEXT NOT NULL, evidence_basis TEXT NOT NULL,
  detail_json TEXT NOT NULL CHECK(json_valid(detail_json)),
  PRIMARY KEY(candidate_id, ordinal), UNIQUE(candidate_id, use_case_id),
  FOREIGN KEY(taxonomy_sha256, facet, use_case_id) REFERENCES taxonomy_terms(taxonomy_sha256, facet, term_id)
);
CREATE TABLE candidate_qa (
  candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id), ordinal INTEGER NOT NULL,
  field_path TEXT NOT NULL, status TEXT NOT NULL,
  detail_json TEXT NOT NULL CHECK(json_valid(detail_json)),
  PRIMARY KEY(candidate_id, ordinal)
);
CREATE TABLE candidate_usage (
  candidate_id TEXT PRIMARY KEY REFERENCES candidates(candidate_id),
  evidence_status TEXT NOT NULL CHECK(evidence_status='observed_isolated_local_codex_logs'),
  total_tokens INTEGER NOT NULL CHECK(total_tokens>=0),
  raw_json TEXT NOT NULL CHECK(json_valid(raw_json))
);
CREATE TABLE lexical_documents (
  candidate_id TEXT PRIMARY KEY REFERENCES candidates(candidate_id), text TEXT NOT NULL,
  purpose TEXT NOT NULL CHECK(purpose='private_diagnostic_only'),
  excluded_qa_roots_json TEXT NOT NULL CHECK(json_valid(excluded_qa_roots_json)),
  public_search_eligible INTEGER NOT NULL CHECK(public_search_eligible=0)
);
CREATE VIRTUAL TABLE lexical_fts USING fts5(candidate_id UNINDEXED, text, tokenize='unicode61');
CREATE VIEW public_search_candidates AS SELECT candidate_id, item_id FROM candidates
  WHERE public_search_eligible=1 AND metadata_human_approved=1 AND release_eligible=1;
