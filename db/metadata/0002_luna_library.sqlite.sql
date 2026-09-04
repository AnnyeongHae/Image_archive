-- Private full-library checkpoint. Source image approval is not metadata/rights approval.
PRAGMA foreign_keys=ON;
CREATE TABLE snapshot(id INTEGER PRIMARY KEY CHECK(id=1), source_sha256 TEXT NOT NULL,
  migration_sha256 TEXT NOT NULL, schema_version TEXT NOT NULL CHECK(schema_version='luna-library-store-3'),
  source_commit_id TEXT NOT NULL, public_eligible INTEGER NOT NULL CHECK(public_eligible=0));
CREATE TABLE evidence(path TEXT PRIMARY KEY, sha256 TEXT NOT NULL);
CREATE TABLE assets(sha256 TEXT PRIMARY KEY CHECK(length(sha256)=64));
CREATE TABLE asset_locations(sha256 TEXT NOT NULL REFERENCES assets, path TEXT NOT NULL,
  role TEXT NOT NULL, PRIMARY KEY(sha256,path,role));
CREATE TABLE prompts(sha256 TEXT PRIMARY KEY CHECK(length(sha256)=64), original_text TEXT NOT NULL);
CREATE TABLE source_prompt_argument_parses(prompt_sha256 TEXT PRIMARY KEY REFERENCES prompts,
  parser_version TEXT NOT NULL, argument_count INTEGER NOT NULL CHECK(argument_count>=0),
  unparsed_marker_offsets_json TEXT NOT NULL CHECK(json_valid(unparsed_marker_offsets_json)));
CREATE TABLE source_prompt_arguments(prompt_sha256 TEXT NOT NULL REFERENCES prompts, ordinal INTEGER NOT NULL,
  start_char INTEGER NOT NULL CHECK(start_char>=0), end_char INTEGER NOT NULL CHECK(end_char>start_char),
  literal TEXT NOT NULL, name_raw TEXT NOT NULL, default_raw TEXT NOT NULL,
  provenance TEXT NOT NULL CHECK(provenance='literal_source_not_llm_or_human_approval'),
  PRIMARY KEY(prompt_sha256,ordinal));
CREATE TABLE source_items(item_id TEXT PRIMARY KEY, style_id TEXT NOT NULL UNIQUE,
  original_sha256 TEXT NOT NULL REFERENCES assets, prepared_sha256 TEXT NOT NULL REFERENCES assets,
  prompt_sha256 TEXT REFERENCES prompts, approval_state TEXT NOT NULL CHECK(approval_state IN
    ('image_approved','retained_unchecked','archived_alias','unreviewed')),
  source_run_id TEXT NOT NULL, raw_json TEXT NOT NULL CHECK(json_valid(raw_json)),
  rights_json TEXT NOT NULL CHECK(json_valid(rights_json)), public_eligible INTEGER NOT NULL CHECK(public_eligible=0));
CREATE TABLE human_notes(item_id TEXT PRIMARY KEY REFERENCES source_items, memo TEXT NOT NULL,
  provenance TEXT NOT NULL, source_commit_id TEXT NOT NULL);
CREATE TABLE approval_groups(group_id TEXT PRIMARY KEY, representative_item_id TEXT NOT NULL REFERENCES source_items,
  source_commit_id TEXT NOT NULL, raw_json TEXT NOT NULL CHECK(json_valid(raw_json)));
CREATE TABLE group_memberships(group_id TEXT NOT NULL REFERENCES approval_groups, item_id TEXT NOT NULL REFERENCES source_items,
  is_representative INTEGER NOT NULL CHECK(is_representative IN(0,1)), PRIMARY KEY(group_id,item_id));
CREATE TABLE archived_aliases(item_id TEXT PRIMARY KEY REFERENCES source_items,
  representative_item_id TEXT NOT NULL REFERENCES source_items, raw_json TEXT NOT NULL CHECK(json_valid(raw_json)));
CREATE TABLE taxonomy_versions(sha256 TEXT PRIMARY KEY, raw_json TEXT NOT NULL CHECK(json_valid(raw_json)),
  status TEXT NOT NULL CHECK(status='proposal_needs_review'));
CREATE TABLE taxonomy_terms(taxonomy_sha256 TEXT NOT NULL REFERENCES taxonomy_versions, facet TEXT NOT NULL,
  term_id TEXT NOT NULL, label_ko TEXT NOT NULL, raw_json TEXT NOT NULL CHECK(json_valid(raw_json)),
  PRIMARY KEY(taxonomy_sha256,facet,term_id));
CREATE TABLE taxonomy_aliases(taxonomy_sha256 TEXT NOT NULL, facet TEXT NOT NULL, term_id TEXT NOT NULL,
  term TEXT NOT NULL, normalized_term TEXT NOT NULL, PRIMARY KEY(taxonomy_sha256,facet,term_id,normalized_term),
  FOREIGN KEY(taxonomy_sha256,facet,term_id) REFERENCES taxonomy_terms);
CREATE INDEX taxonomy_alias_lookup ON taxonomy_aliases(normalized_term);
CREATE TABLE analysis_runs(run_id TEXT PRIMARY KEY, mode TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL, manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)));
CREATE TABLE analysis_tasks(item_id TEXT PRIMARY KEY REFERENCES source_items, run_id TEXT NOT NULL REFERENCES analysis_runs,
  style_id TEXT NOT NULL UNIQUE, mode TEXT NOT NULL CHECK(mode IN('legacy_reuse','new_compact')),
  input_fingerprint TEXT NOT NULL, batch_id TEXT,
  state TEXT NOT NULL CHECK(state IN('legacy_reused','pending','visual_draft_ready','validated_candidate','invalid_result')),
  usage_state TEXT NOT NULL CHECK(usage_state IN('observed_legacy_scope','usage_pending','observed_batch_scope','observed_turn_scope','usage_unobserved_completed_turn')),
  error_json TEXT CHECK(error_json IS NULL OR json_valid(error_json)), raw_json TEXT NOT NULL CHECK(json_valid(raw_json)));
CREATE TABLE analysis_results(candidate_id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES source_items,
  source_run_id TEXT NOT NULL REFERENCES analysis_runs, result_schema TEXT NOT NULL, raw_sha256 TEXT NOT NULL,
  raw_json TEXT NOT NULL CHECK(json_valid(raw_json)), effective_json TEXT NOT NULL CHECK(json_valid(effective_json)),
  effective_sha256 TEXT NOT NULL, visual_json TEXT NOT NULL CHECK(json_valid(visual_json)),
  prompt_json TEXT NOT NULL CHECK(json_valid(prompt_json)), freeform_json TEXT NOT NULL CHECK(json_valid(freeform_json)),
  review_status TEXT NOT NULL CHECK(review_status='needs_review'), metadata_human_approved INTEGER NOT NULL CHECK(metadata_human_approved=0),
  public_eligible INTEGER NOT NULL CHECK(public_eligible=0), UNIQUE(item_id,source_run_id,raw_sha256));
CREATE TABLE candidate_normalizations(candidate_id TEXT PRIMARY KEY REFERENCES analysis_results,
  adapter_version TEXT NOT NULL, raw_draft_sha256 TEXT NOT NULL,
  raw_draft_json TEXT NOT NULL CHECK(json_valid(raw_draft_json)), effective_draft_json TEXT NOT NULL CHECK(json_valid(effective_draft_json)),
  normalization_json TEXT NOT NULL CHECK(json_valid(normalization_json)));
CREATE TABLE analysis_result_history(item_id TEXT NOT NULL REFERENCES source_items,
  history_sha256 TEXT NOT NULL, source_run_id TEXT NOT NULL REFERENCES analysis_runs,
  input_fingerprint TEXT NOT NULL, raw_sha256 TEXT NOT NULL, draft_sha256 TEXT NOT NULL,
  raw_json TEXT NOT NULL CHECK(json_valid(raw_json)), draft_json TEXT NOT NULL CHECK(json_valid(draft_json)),
  reason TEXT NOT NULL, metadata_human_approved INTEGER NOT NULL CHECK(metadata_human_approved=0),
  PRIMARY KEY(item_id,history_sha256));
CREATE TABLE literal_format_repairs(sha256 TEXT PRIMARY KEY, source_run_id TEXT NOT NULL REFERENCES analysis_runs,
  raw_json TEXT NOT NULL CHECK(json_valid(raw_json)), model_calls INTEGER NOT NULL CHECK(model_calls=0));
CREATE TABLE literal_format_repair_items(repair_sha256 TEXT NOT NULL REFERENCES literal_format_repairs,
  item_id TEXT NOT NULL REFERENCES source_items, PRIMARY KEY(repair_sha256,item_id));
CREATE TABLE draft_format_backups(sha256 TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES source_items,
  source_run_id TEXT NOT NULL REFERENCES analysis_runs, path TEXT NOT NULL,
  raw_json TEXT NOT NULL CHECK(json_valid(raw_json)), repair_kind TEXT NOT NULL CHECK(repair_kind='missing_ocr_null_to_empty_string'));
CREATE TABLE usage_assignments(candidate_id TEXT NOT NULL REFERENCES analysis_results, ordinal INTEGER NOT NULL,
  taxonomy_sha256 TEXT NOT NULL, facet TEXT NOT NULL CHECK(facet='use_case'), use_case_id TEXT NOT NULL,
  fit TEXT NOT NULL CHECK(fit IN('supported','conditional','speculative')),
  raw_json TEXT NOT NULL CHECK(json_valid(raw_json)), PRIMARY KEY(candidate_id,ordinal),
  FOREIGN KEY(taxonomy_sha256,facet,use_case_id) REFERENCES taxonomy_terms);
CREATE TABLE candidate_qa(candidate_id TEXT NOT NULL REFERENCES analysis_results, ordinal INTEGER NOT NULL,
  field_path TEXT NOT NULL, raw_json TEXT NOT NULL CHECK(json_valid(raw_json)), PRIMARY KEY(candidate_id,ordinal));
CREATE TABLE quality_reviews(sha256 TEXT PRIMARY KEY, source_run_id TEXT NOT NULL REFERENCES analysis_runs,
  finding_count INTEGER NOT NULL CHECK(finding_count>=0), raw_json TEXT NOT NULL CHECK(json_valid(raw_json)));
CREATE TABLE token_receipts(sha256 TEXT PRIMARY KEY, source_run_id TEXT NOT NULL REFERENCES analysis_runs,
  path TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN('legacy','compact')),
  total_tokens INTEGER CHECK(total_tokens IS NULL OR total_tokens>=0), scope TEXT NOT NULL,
  actual_billed_tokens INTEGER CHECK(actual_billed_tokens IS NULL), actual_billed_cost REAL CHECK(actual_billed_cost IS NULL),
  raw_json TEXT NOT NULL CHECK(json_valid(raw_json)));
CREATE TABLE token_sessions(session_id TEXT PRIMARY KEY, raw_sha256 TEXT NOT NULL,
  total_tokens INTEGER NOT NULL CHECK(total_tokens>=0), raw_json TEXT NOT NULL CHECK(json_valid(raw_json)));
CREATE TABLE receipt_sessions(receipt_sha256 TEXT NOT NULL REFERENCES token_receipts,
  session_id TEXT NOT NULL REFERENCES token_sessions, PRIMARY KEY(receipt_sha256,session_id));
CREATE TABLE session_items(session_id TEXT NOT NULL REFERENCES token_sessions,
  item_id TEXT NOT NULL REFERENCES analysis_tasks, PRIMARY KEY(session_id,item_id));
CREATE TABLE token_turns(session_id TEXT NOT NULL, turn_id TEXT NOT NULL, raw_sha256 TEXT NOT NULL,
  total_tokens INTEGER CHECK(total_tokens IS NULL OR total_tokens>=0),
  input_tokens INTEGER, output_tokens INTEGER, cached_input_tokens INTEGER, reasoning_output_tokens INTEGER,
  raw_json TEXT NOT NULL CHECK(json_valid(raw_json)), PRIMARY KEY(session_id,turn_id));
CREATE TABLE receipt_turns(receipt_sha256 TEXT NOT NULL REFERENCES token_receipts, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
  PRIMARY KEY(receipt_sha256,session_id,turn_id), FOREIGN KEY(session_id,turn_id) REFERENCES token_turns);
CREATE TABLE turn_items(session_id TEXT NOT NULL, turn_id TEXT NOT NULL, item_id TEXT NOT NULL REFERENCES analysis_tasks,
  PRIMARY KEY(session_id,turn_id,item_id), FOREIGN KEY(session_id,turn_id) REFERENCES token_turns);
CREATE TABLE diagnostic_documents(item_id TEXT PRIMARY KEY REFERENCES source_items, text TEXT NOT NULL,
  approval_state TEXT NOT NULL, public_eligible INTEGER NOT NULL CHECK(public_eligible=0));
CREATE VIRTUAL TABLE diagnostic_fts USING fts5(item_id UNINDEXED,text,tokenize='unicode61');
CREATE VIEW public_search_items AS SELECT item_id FROM source_items WHERE public_eligible=1;
