-- Idempotent developer observability objects; re-applied by launch.py on every start.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS started_at timestamptz;
CREATE INDEX IF NOT EXISTS jobs_kind_created_idx ON jobs(kind,created_at DESC);
CREATE INDEX IF NOT EXISTS agent_runs_started_idx ON agent_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS tool_audit_created_idx ON tool_audit(created_at DESC);
CREATE INDEX IF NOT EXISTS news_fetched_idx ON news(fetched_at DESC);
CREATE TABLE IF NOT EXISTS worker_heartbeats (
  worker_id text PRIMARY KEY,
  role text NOT NULL, kinds text[] NOT NULL, host text, pid integer,
  started_at timestamptz NOT NULL, last_seen timestamptz NOT NULL,
  running_jobs jsonb NOT NULL DEFAULT '[]'::jsonb,
  processed bigint NOT NULL DEFAULT 0, failed bigint NOT NULL DEFAULT 0
);
