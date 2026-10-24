-- SafeRepo run ledger schema + seed row.
--
-- Postgres runs this automatically on the first boot of an empty data
-- directory (compose mounts ./seeds at /docker-entrypoint-initdb.d).
-- The api process also runs the same CREATE TABLE at startup, so the schema
-- is correct even if the volume already existed.
--
-- Re-running this script must not duplicate anything: the ON CONFLICT
-- clause makes the seed idempotent.

CREATE TABLE IF NOT EXISTS runs (
    run_id           TEXT PRIMARY KEY,
    sandbox_name     TEXT NOT NULL,
    problem_id       TEXT NOT NULL,
    state            TEXT NOT NULL,
    execution_target TEXT NOT NULL,
    detail           JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS runs_state_idx ON runs (state);
CREATE INDEX IF NOT EXISTS runs_sandbox_idx ON runs (sandbox_name);

INSERT INTO runs (run_id, sandbox_name, problem_id, state, execution_target)
VALUES ('seed-demo-0001', 'sbx-seeddemo1', 'issue-failing-test', 'deleted', 'celesto_sandbox')
ON CONFLICT (run_id) DO NOTHING;
