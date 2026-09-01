CREATE SCHEMA IF NOT EXISTS image_archive;

CREATE TABLE IF NOT EXISTS image_archive.import_batches (
    batch_id text PRIMARY KEY,
    source_manifest_sha256 char(64) NOT NULL,
    source_path text NOT NULL,
    requested_limit integer,
    status text NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    records_seen integer NOT NULL DEFAULT 0 CHECK (records_seen >= 0),
    records_written integer NOT NULL DEFAULT 0 CHECK (records_written >= 0),
    media_written integer NOT NULL DEFAULT 0 CHECK (media_written >= 0),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    error_code text
);

CREATE TABLE IF NOT EXISTS image_archive.archive_records_private (
    catalog_key text PRIMARY KEY,
    schema_version text NOT NULL,
    lane text NOT NULL,
    record_id text NOT NULL,
    style_id text NOT NULL,
    parent_style_id text,
    title text NOT NULL,
    content_sha256 char(64) NOT NULL,
    prompt_sha256 char(64),
    prompt_text text,
    prompt_language text,
    prompt_format text,
    source_name text,
    source_url text,
    source_type text,
    source_repository text,
    source_commit text,
    rights_tier char(2) NOT NULL CHECK (rights_tier IN ('P1', 'P2', 'P3', 'P4')),
    portfolio_visibility text NOT NULL CHECK (portfolio_visibility IN ('public', 'metadata_link_only', 'admin_only')),
    admin_usage_status text NOT NULL CHECK (admin_usage_status IN ('public_or_metadata', 'reference_allowed', 'quarantine_only')),
    public_metadata_eligible boolean NOT NULL DEFAULT false,
    prompt_publication_eligible boolean NOT NULL DEFAULT false,
    media_publication_eligible boolean NOT NULL DEFAULT false,
    release_eligible boolean NOT NULL DEFAULT false,
    source_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    license_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    rights_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    taxonomy_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    generation_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    review_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    provenance_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    import_batch_id text NOT NULL REFERENCES image_archive.import_batches(batch_id),
    imported_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT archive_private_visibility_guard CHECK (
        (rights_tier = 'P1' AND portfolio_visibility = 'public' AND admin_usage_status = 'public_or_metadata')
        OR (rights_tier = 'P2' AND portfolio_visibility = 'metadata_link_only' AND admin_usage_status = 'public_or_metadata')
        OR (rights_tier = 'P3' AND portfolio_visibility = 'admin_only' AND admin_usage_status = 'reference_allowed')
        OR (rights_tier = 'P4' AND portfolio_visibility = 'admin_only' AND admin_usage_status = 'quarantine_only')
    )
);

CREATE TABLE IF NOT EXISTS image_archive.archive_media_private (
    catalog_key text NOT NULL REFERENCES image_archive.archive_records_private(catalog_key) ON DELETE CASCADE,
    asset_ordinal integer NOT NULL CHECK (asset_ordinal >= 0),
    role text,
    uri text,
    private_path text,
    uri_kind text,
    origin text,
    sha256 char(64),
    mime_type text,
    width integer CHECK (width IS NULL OR width > 0),
    height integer CHECK (height IS NULL OR height > 0),
    generated_staging boolean NOT NULL DEFAULT false,
    release_eligible boolean NOT NULL DEFAULT false,
    PRIMARY KEY (catalog_key, asset_ordinal),
    CONSTRAINT archive_media_no_inline_base64 CHECK (uri IS NULL OR uri NOT LIKE 'data:%')
);

CREATE TABLE IF NOT EXISTS image_archive.archive_records_public (
    catalog_key text PRIMARY KEY REFERENCES image_archive.archive_records_private(catalog_key) ON DELETE CASCADE,
    style_id text NOT NULL,
    title text NOT NULL,
    rights_tier char(2) NOT NULL CHECK (rights_tier IN ('P1', 'P2')),
    source_name text,
    source_url text,
    public_dto jsonb NOT NULL,
    content_sha256 char(64) NOT NULL,
    published_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT archive_public_dto_tier_guard CHECK (public_dto #>> '{rights,rights_tier}' = rights_tier),
    CONSTRAINT archive_public_p2_prompt_guard CHECK (
        rights_tier <> 'P2' OR NOT ((public_dto -> 'prompt') ? 'text')
    ),
    CONSTRAINT archive_public_p2_media_guard CHECK (
        rights_tier <> 'P2' OR jsonb_array_length(COALESCE(public_dto #> '{media,assets}', '[]'::jsonb)) = 0
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS archive_private_style_lane_idx
    ON image_archive.archive_records_private(lane, style_id);

CREATE INDEX IF NOT EXISTS archive_private_rights_idx
    ON image_archive.archive_records_private(rights_tier, portfolio_visibility, catalog_key);

CREATE INDEX IF NOT EXISTS archive_private_source_idx
    ON image_archive.archive_records_private(source_name, catalog_key);

CREATE INDEX IF NOT EXISTS archive_private_prompt_sha_idx
    ON image_archive.archive_records_private(prompt_sha256)
    WHERE prompt_sha256 IS NOT NULL;

CREATE INDEX IF NOT EXISTS archive_private_content_sha_idx
    ON image_archive.archive_records_private(content_sha256);

CREATE INDEX IF NOT EXISTS archive_private_fts_idx
    ON image_archive.archive_records_private
    USING gin (to_tsvector('simple'::regconfig, COALESCE(style_id, '') || ' ' || COALESCE(title, '') || ' ' || COALESCE(prompt_text, '') || ' ' || COALESCE(source_name, '')));

CREATE INDEX IF NOT EXISTS archive_public_fts_idx
    ON image_archive.archive_records_public
    USING gin (to_tsvector('simple'::regconfig, COALESCE(style_id, '') || ' ' || COALESCE(title, '') || ' ' || COALESCE(public_dto ->> 'search_text', '')));
