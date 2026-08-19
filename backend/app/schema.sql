-- LinkPlease schema. Idempotent: safe to run on every boot (BLUEPRINT §8.1).
-- Postgres is the queue, the dedup ledger, the rate-limit log and the stats
-- source. Every constraint here is load-bearing; see BLUEPRINT §3.

-- Every webhook delivery ever received. The event-level dedup ledger.
CREATE TABLE IF NOT EXISTS events (
    event_id      TEXT PRIMARY KEY,          -- their id; PK = dedup
    event_type    TEXT NOT NULL,             -- comment.created | comment.deleted
    payload       JSONB NOT NULL,            -- raw body, for audit/debug
    sent_at       TIMESTAMPTZ,               -- their timestamp (unordered!)
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    redeliveries  INT NOT NULL DEFAULT 0,    -- bumped when same event_id re-arrives
    processed_at  TIMESTAMPTZ                -- NULL = matcher has not finished it yet
);
-- Boot recovery + the matcher sweep both need "unprocessed created events".
CREATE INDEX IF NOT EXISTS idx_events_unprocessed
    ON events (received_at) WHERE processed_at IS NULL;

CREATE TABLE IF NOT EXISTS rules (
    rule_id     TEXT PRIMARY KEY,            -- "rule_" + random suffix
    keyword     TEXT NOT NULL,               -- stored as-given; matched case-insensitively
    dm_message  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per (rule, user) DM obligation. THE core table.
CREATE TABLE IF NOT EXISTS dm_jobs (
    job_id           BIGSERIAL PRIMARY KEY,
    rule_id          TEXT NOT NULL REFERENCES rules(rule_id),
    user_id          TEXT NOT NULL,           -- identity is user_id, never username
    username         TEXT,                    -- display only
    comment_id       TEXT NOT NULL,           -- triggering comment (latest if revived)
    post_id          TEXT,
    status           TEXT NOT NULL DEFAULT 'QUEUED',
      -- QUEUED | SENDING | AWAITING_CONFIRM | SENT | FAILED | CANCELLED
    attempt          INT NOT NULL DEFAULT 0,  -- send attempts within current cycle
    cycle            INT NOT NULL DEFAULT 0,  -- bumped on resend (new Idempotency-Key)
    dm_id            TEXT,                    -- from the 202 response
    next_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    check_after      TIMESTAMPTZ,             -- reconciler: next status poll due
    checks           INT NOT NULL DEFAULT 0,  -- how many status polls this cycle
    last_error       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- "The same user never gets DMed twice for the same rule" — enforced by the DB,
-- not by application logic. A CANCELLED job (comment deleted before we sent) is
-- excluded: the user never received that DM, so a new comment may revive it.
CREATE UNIQUE INDEX IF NOT EXISTS uq_live_job ON dm_jobs (rule_id, user_id)
    WHERE status <> 'CANCELLED';
CREATE INDEX IF NOT EXISTS idx_jobs_due
    ON dm_jobs (next_attempt_at) WHERE status = 'QUEUED';
CREATE INDEX IF NOT EXISTS idx_jobs_confirming
    ON dm_jobs (check_after) WHERE status = 'AWAITING_CONFIRM';
CREATE INDEX IF NOT EXISTS idx_jobs_comment ON dm_jobs (comment_id);
CREATE INDEX IF NOT EXISTS idx_jobs_updated ON dm_jobs (updated_at DESC);

-- Rolling-window rate ledger for POST /v1/dm/send (GET reads are free).
CREATE TABLE IF NOT EXISTS send_log (
    id       BIGSERIAL PRIMARY KEY,
    job_id   BIGINT,
    sent_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_send_log_at ON send_log (sent_at DESC);

-- comment.deleted that arrived before (or without) its comment.created.
CREATE TABLE IF NOT EXISTS deleted_comments (
    comment_id  TEXT PRIMARY KEY,
    deleted_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Monotonic counters that are not derivable from the job rows themselves.
CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value BIGINT NOT NULL DEFAULT 0
);
-- Seeded so /stats never has to cope with a missing row.
INSERT INTO counters (name, value) VALUES
    ('duplicates_blocked_rule_user', 0),   -- distinct comments, same (rule,user)
    ('duplicate_events_suppressed', 0),    -- redelivered event_ids that we dropped
    ('duplicate_events_would_dm', 0)       -- redeliveries that would have matched a rule
ON CONFLICT (name) DO NOTHING;
