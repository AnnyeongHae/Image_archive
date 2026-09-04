-- Private, snapshot-scoped v2 only. No legacy/public-table mutation.
CREATE SCHEMA IF NOT EXISTS image_archive_v2;
CREATE TABLE IF NOT EXISTS image_archive_v2.schema_migrations (
  version text PRIMARY KEY, sha256 text NOT NULL CHECK (sha256 ~ '^[a-f0-9]{64}$')
);
CREATE TABLE IF NOT EXISTS image_archive_v2.snapshots (
  snapshot_id text PRIMARY KEY CHECK (snapshot_id ~ '^[a-f0-9]{64}$'),
  manifest_sha256 text NOT NULL CHECK (manifest_sha256 ~ '^[a-f0-9]{64}$'),
  manifest_json jsonb NOT NULL,
  state text NOT NULL DEFAULT 'staged' CHECK (state IN ('staged','ready')),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS image_archive_v2.items (
  snapshot_id text NOT NULL REFERENCES image_archive_v2.snapshots,
  item_id text NOT NULL, group_id text NOT NULL, representative_id text NOT NULL,
  original_prompt text NOT NULL, rights_json jsonb NOT NULL,
  metadata_json jsonb NOT NULL, human_note text,
  text_ready boolean NOT NULL, retrieval_text text NOT NULL,
  private_data jsonb NOT NULL,
  PRIMARY KEY (snapshot_id,item_id),
  FOREIGN KEY (snapshot_id,representative_id) REFERENCES image_archive_v2.items(snapshot_id,item_id)
    DEFERRABLE INITIALLY DEFERRED,
  CHECK (jsonb_typeof(rights_json)='object' AND jsonb_typeof(metadata_json)='object'),
  CHECK (metadata_json @> '{"metadata_human_approved":false,"public_eligible":false}'::jsonb),
  CHECK (NOT text_ready OR length(retrieval_text)>0)
);
CREATE INDEX IF NOT EXISTS v2_items_group_idx ON image_archive_v2.items(snapshot_id,group_id,item_id);
CREATE TABLE IF NOT EXISTS image_archive_v2.query_vectors (
  snapshot_id text NOT NULL REFERENCES image_archive_v2.snapshots,
  query_id text NOT NULL, query_text text NOT NULL,
  model text NOT NULL CHECK (model='voyage-4-lite'),
  dimension integer NOT NULL CHECK (dimension=512), vector_json jsonb NOT NULL,
  PRIMARY KEY (snapshot_id,query_id),
  CHECK (jsonb_typeof(vector_json)='array' AND jsonb_array_length(vector_json)=512)
);
-- One-time migration: its complete file digest is pinned by the local sync tool.
CREATE FUNCTION image_archive_v2.only_staged_snapshot_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.state IS DISTINCT FROM 'staged' THEN
    RAISE EXCEPTION 'new snapshot must be staged';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER v2_snapshot_insert BEFORE INSERT ON image_archive_v2.snapshots
  FOR EACH ROW EXECUTE FUNCTION image_archive_v2.only_staged_snapshot_insert();
CREATE FUNCTION image_archive_v2.only_staged_child_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE parent_state text;
BEGIN
  -- The parent row lock is retained until transaction end. A ready transition
  -- uses this same lock, so neither can pass the other between check and write.
  SELECT state INTO parent_state FROM image_archive_v2.snapshots
    WHERE snapshot_id=NEW.snapshot_id FOR UPDATE;
  IF parent_state IS DISTINCT FROM 'staged' THEN
    RAISE EXCEPTION 'snapshot content insert requires staged parent';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER v2_items_insert BEFORE INSERT ON image_archive_v2.items
  FOR EACH ROW EXECUTE FUNCTION image_archive_v2.only_staged_child_insert();
CREATE TRIGGER v2_queries_insert BEFORE INSERT ON image_archive_v2.query_vectors
  FOR EACH ROW EXECUTE FUNCTION image_archive_v2.only_staged_child_insert();
CREATE FUNCTION image_archive_v2.deny_frozen_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'immutable v2 snapshot content';
END $$;
CREATE TRIGGER v2_items_frozen BEFORE UPDATE OR DELETE ON image_archive_v2.items
  FOR EACH ROW EXECUTE FUNCTION image_archive_v2.deny_frozen_change();
CREATE TRIGGER v2_queries_frozen BEFORE UPDATE OR DELETE ON image_archive_v2.query_vectors
  FOR EACH ROW EXECUTE FUNCTION image_archive_v2.deny_frozen_change();
CREATE FUNCTION image_archive_v2.only_ready_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  -- UPDATE already holds this row lock; explicit acquisition documents and
  -- enforces the same serialization boundary as child INSERT triggers.
  PERFORM 1 FROM image_archive_v2.snapshots WHERE snapshot_id=OLD.snapshot_id FOR UPDATE;
  IF TG_OP='UPDATE' AND OLD.state='staged' AND NEW.state='ready'
    AND (to_jsonb(OLD)-'state')=(to_jsonb(NEW)-'state') THEN RETURN NEW; END IF;
  RAISE EXCEPTION 'only staged-to-ready transition permitted';
END $$;
CREATE TRIGGER v2_snapshot_transition BEFORE UPDATE OR DELETE ON image_archive_v2.snapshots
  FOR EACH ROW EXECUTE FUNCTION image_archive_v2.only_ready_transition();
REVOKE ALL ON ALL TABLES IN SCHEMA image_archive_v2 FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA image_archive_v2 FROM PUBLIC;
