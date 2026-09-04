-- Additive owner-API state only. This migration does not activate model calls.
CREATE TABLE IF NOT EXISTS image_archive_v2.api_model_guard (
  model text PRIMARY KEY CHECK (model='voyage-4-lite'),
  blocked boolean NOT NULL DEFAULT false,
  reason text,
  updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO image_archive_v2.api_model_guard(model) VALUES ('voyage-4-lite') ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS image_archive_v2.api_daily_budget (
  usage_day date NOT NULL,
  model text NOT NULL CHECK (model='voyage-4-lite'),
  reserved_calls integer NOT NULL CHECK (reserved_calls>=0),
  reserved_tokens bigint NOT NULL CHECK (reserved_tokens>=0),
  PRIMARY KEY (usage_day, model)
);
CREATE TABLE IF NOT EXISTS image_archive_v2.api_query_receipts (
  request_id uuid PRIMARY KEY,
  token_id text NOT NULL,
  query_sha256 text NOT NULL CHECK (query_sha256 ~ '^[a-f0-9]{64}$'),
  model text NOT NULL CHECK (model='voyage-4-lite'),
  reserved_tokens integer NOT NULL CHECK (reserved_tokens>0),
  actual_tokens integer CHECK (actual_tokens>=0),
  state text NOT NULL CHECK (state IN ('reserved','observed','uncertain')),
  created_at timestamptz NOT NULL DEFAULT now()
);
-- A timed-out request remains charged against the conservative daily reservation.
-- No automatic retry/refund. Rows are audit records; cleanup is an explicit task.
