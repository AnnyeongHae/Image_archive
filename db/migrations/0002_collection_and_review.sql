CREATE TABLE IF NOT EXISTS image_archive.sources (
    source_id text PRIMARY KEY,
    source_type text NOT NULL,
    display_name text NOT NULL,
    source_url text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    collection_policy jsonb NOT NULL DEFAULT '{}'::jsonb,
    rights_policy jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_observed_at timestamptz,
    last_success_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS image_archive.source_runs (
    run_id text PRIMARY KEY,
    source_id text NOT NULL REFERENCES image_archive.sources(source_id),
    trigger_type text NOT NULL CHECK (trigger_type IN ('manual', 'schedule', 'backfill', 'canary')),
    status text NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'blocked')),
    upstream_cursor text,
    observed_from timestamptz,
    observed_until timestamptz,
    listed_count integer NOT NULL DEFAULT 0 CHECK (listed_count >= 0),
    new_count integer NOT NULL DEFAULT 0 CHECK (new_count >= 0),
    duplicate_count integer NOT NULL DEFAULT 0 CHECK (duplicate_count >= 0),
    error_code text,
    receipt jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

CREATE TABLE IF NOT EXISTS image_archive.source_items (
    source_id text NOT NULL REFERENCES image_archive.sources(source_id),
    upstream_key text NOT NULL,
    upstream_updated_at timestamptz,
    source_url text NOT NULL,
    title text,
    prompt_sha256 char(64),
    image_sha256 char(64),
    metadata_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    rights_tier char(2) NOT NULL DEFAULT 'P3' CHECK (rights_tier IN ('P1', 'P2', 'P3', 'P4')),
    review_status text NOT NULL DEFAULT 'pending' CHECK (review_status IN ('pending', 'approved', 'held', 'rejected')),
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    last_run_id text REFERENCES image_archive.source_runs(run_id),
    PRIMARY KEY (source_id, upstream_key)
);

CREATE TABLE IF NOT EXISTS image_archive.review_drafts (
    admin_subject text NOT NULL,
    queue_revision char(64) NOT NULL,
    decisions jsonb NOT NULL,
    decision_count integer NOT NULL CHECK (decision_count >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (admin_subject, queue_revision)
);

CREATE TABLE IF NOT EXISTS image_archive.review_decisions (
    decision_batch_id text NOT NULL,
    item_key text NOT NULL,
    admin_subject text NOT NULL,
    queue_revision char(64) NOT NULL,
    decision text NOT NULL CHECK (decision IN ('approve', 'hold', 'reject')),
    reason_code text,
    decided_at timestamptz NOT NULL DEFAULT now(),
    payload_sha256 char(64) NOT NULL,
    PRIMARY KEY (decision_batch_id, item_key)
);

CREATE TABLE IF NOT EXISTS image_archive.duplicate_groups (
    group_id text PRIMARY KEY,
    group_type text NOT NULL CHECK (group_type IN ('exact', 'prompt_near', 'image_near', 'remix')),
    canonical_catalog_key text REFERENCES image_archive.archive_records_private(catalog_key),
    review_status text NOT NULL DEFAULT 'pending' CHECK (review_status IN ('pending', 'confirmed', 'dismissed')),
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS image_archive.duplicate_group_members (
    group_id text NOT NULL REFERENCES image_archive.duplicate_groups(group_id) ON DELETE CASCADE,
    catalog_key text NOT NULL REFERENCES image_archive.archive_records_private(catalog_key) ON DELETE CASCADE,
    relation text NOT NULL,
    score numeric(6,5),
    PRIMARY KEY (group_id, catalog_key)
);

CREATE INDEX IF NOT EXISTS source_items_review_idx
    ON image_archive.source_items(review_status, last_seen_at DESC, source_id);

CREATE INDEX IF NOT EXISTS source_items_prompt_sha_idx
    ON image_archive.source_items(prompt_sha256)
    WHERE prompt_sha256 IS NOT NULL;

CREATE INDEX IF NOT EXISTS source_items_image_sha_idx
    ON image_archive.source_items(image_sha256)
    WHERE image_sha256 IS NOT NULL;

CREATE INDEX IF NOT EXISTS source_runs_source_time_idx
    ON image_archive.source_runs(source_id, started_at DESC);

CREATE INDEX IF NOT EXISTS duplicate_members_catalog_idx
    ON image_archive.duplicate_group_members(catalog_key);
