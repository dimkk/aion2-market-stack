CREATE TABLE IF NOT EXISTS users (
  username text PRIMARY KEY,
  user_id text NOT NULL UNIQUE,
  password_hash text NOT NULL,
  role text NOT NULL CHECK (role IN ('registered', 'approved', 'admin')),
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  session_hash text PRIMARY KEY,
  username text NOT NULL REFERENCES users(username) ON DELETE CASCADE,
  csrf_hash text NOT NULL,
  created_at timestamptz NOT NULL,
  expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS desktop_sync_tokens (
  token_id text PRIMARY KEY,
  token_digest text NOT NULL,
  owner_username text NOT NULL REFERENCES users(username) ON DELETE CASCADE,
  scope text NOT NULL,
  label text,
  created_at timestamptz NOT NULL,
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz
);
CREATE INDEX IF NOT EXISTS desktop_sync_tokens_owner_idx
  ON desktop_sync_tokens(owner_username, created_at DESC);

CREATE TABLE IF NOT EXISTS farm_sessions (
  owner_username text NOT NULL REFERENCES users(username) ON DELETE CASCADE,
  session_id text NOT NULL,
  client_session_id text NOT NULL,
  payload_json jsonb NOT NULL,
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  PRIMARY KEY (owner_username, session_id),
  UNIQUE (owner_username, client_session_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
  event_id text PRIMARY KEY,
  occurred_at timestamptz NOT NULL,
  kind text NOT NULL,
  actor_user_id text,
  target_user_id text,
  request_id text NOT NULL,
  outcome text NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_events_time_idx ON audit_events(occurred_at);

CREATE TABLE IF NOT EXISTS migration_receipts (
  migration_id text PRIMARY KEY,
  source_sha256 text NOT NULL UNIQUE CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
  users_count bigint NOT NULL CHECK (users_count >= 0),
  farm_sessions_count bigint NOT NULL CHECK (farm_sessions_count >= 0),
  audit_events_count bigint NOT NULL CHECK (audit_events_count >= 0),
  applied_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS crawler_status (
  crawler_id text PRIMARY KEY,
  payload_json jsonb NOT NULL,
  updated_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS crawler_leases (
  crawler_id text PRIMARY KEY,
  run_id text NOT NULL,
  lease_digest text NOT NULL,
  connected_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS crawler_actions (
  action_id text PRIMARY KEY,
  kind text NOT NULL,
  expected_revision bigint NOT NULL CHECK (expected_revision >= 0),
  status text NOT NULL,
  requested_by_user_id text NOT NULL,
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS crawler_actions_queue_idx
  ON crawler_actions(status, expected_revision, created_at, action_id);

CREATE TABLE IF NOT EXISTS rate_limits (
  limit_key text NOT NULL,
  window_id bigint NOT NULL,
  request_count bigint NOT NULL CHECK (request_count >= 0),
  expires_at timestamptz NOT NULL,
  PRIMARY KEY (limit_key, window_id)
);

-- Bounded cleanup is invoked by the maintenance timer.  It is deliberately
-- separate from request handling so attackers cannot amplify maintenance work.
