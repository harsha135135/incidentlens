-- Jobs, actions, evidence, reports and sessions (spec §11), and the roles that use them.
-- Runs as the database owner (migrations only). The application and the collector connect as
-- the non-superuser logins below; their passwords are set by provisioning, never here.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'incidentlens_app') THEN
        CREATE ROLE incidentlens_app NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'incidentlens_collector') THEN
        CREATE ROLE incidentlens_collector NOLOGIN;
    END IF;
END
$$;

-- One row for admission and coordination: create requests lock it, so the queue cap holds under
-- concurrency; the supervisor's heartbeat lives here.
CREATE TABLE control (
    singleton               boolean     PRIMARY KEY DEFAULT true CHECK (singleton),
    supervisor_instance     text,
    supervisor_heartbeat_at timestamptz
);
INSERT INTO control DEFAULT VALUES;

CREATE TABLE investigations (
    id                    uuid        PRIMARY KEY,
    owner                 text        NOT NULL,
    service_id            text        NOT NULL,
    symptom               text        NOT NULL CHECK (length(symptom) BETWEEN 1 AND 2000),
    window_start          timestamptz NOT NULL,
    window_end            timestamptz NOT NULL,
    profile               text        NOT NULL CHECK (profile = 'standard'),
    status                text        NOT NULL CHECK (status IN
                              ('QUEUED', 'RUNNING', 'CANCEL_REQUESTED', 'COMPLETED', 'CANCELLED', 'FAILED')),
    created_at            timestamptz NOT NULL DEFAULT now(),
    queue_expires_at      timestamptz NOT NULL,
    -- Execution fencing: every claim or takeover increments generation and issues a new capability.
    generation            integer     NOT NULL DEFAULT 0,
    lease_until           timestamptz,
    capability_hash       text,
    sandbox_name          text,
    started_at            timestamptz,
    deadline              timestamptz,  -- set once, at first claim; recovery never resets it
    recoveries            integer     NOT NULL DEFAULT 0 CHECK (recoveries BETWEEN 0 AND 2),
    cancel_requested_at   timestamptz,
    finished_at           timestamptz,
    final_reason          text,
    -- The runner must use this server-issued ID for its next action (spec §8).
    next_action_id        uuid,
    next_phase            text        CHECK (next_phase IN
                              ('initial_collection', 'model_decision', 'tool_observation', 'finalization')),
    last_decision         jsonb,
    -- Budget counters (spec §13). Reservations are never released for UNKNOWN model calls.
    model_actions         integer     NOT NULL DEFAULT 0,
    tool_actions          integer     NOT NULL DEFAULT 0,
    tokens_reserved       integer     NOT NULL DEFAULT 0,
    evidence_bytes        integer     NOT NULL DEFAULT 0,
    evidence_count        integer     NOT NULL DEFAULT 0,
    event_sequence        integer     NOT NULL DEFAULT 0,
    CHECK (window_end > window_start)
);

CREATE INDEX investigations_queue ON investigations (created_at, id) WHERE status = 'QUEUED';
CREATE INDEX investigations_active ON investigations (status) WHERE status IN ('RUNNING', 'CANCEL_REQUESTED');
CREATE INDEX investigations_history ON investigations (owner, created_at, id);

CREATE TABLE submission_keys (
    owner            text        NOT NULL,
    endpoint         text        NOT NULL,
    idempotency_key  text        NOT NULL,
    request_hash     text        NOT NULL,
    investigation_id uuid        NOT NULL REFERENCES investigations (id),
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner, endpoint, idempotency_key)
);

CREATE TABLE actions (
    id                     uuid        PRIMARY KEY,
    investigation_id       uuid        NOT NULL REFERENCES investigations (id),
    generation             integer     NOT NULL,
    kind                   text        NOT NULL CHECK (kind IN ('tool', 'model')),
    tool                   text,
    arguments              jsonb       NOT NULL,
    input_hash             text        NOT NULL,
    status                 text        NOT NULL CHECK (status IN
                               ('PENDING', 'RUNNING', 'SUCCEEDED', 'ERROR', 'UNKNOWN', 'INTERRUPTED')),
    reserved_tokens        integer     NOT NULL DEFAULT 0,
    input_tokens           integer,
    output_tokens          integer,
    provider_request_id    text,
    response               jsonb,
    error                  text,
    created_at             timestamptz NOT NULL DEFAULT now(),
    started_at             timestamptz,
    finished_at            timestamptz,
    CHECK ((kind = 'tool') = (tool IS NOT NULL))
);

CREATE INDEX actions_pending ON actions (created_at, id) WHERE status = 'PENDING';
CREATE INDEX actions_by_job ON actions (investigation_id, created_at);

-- Saved observations. ordinal gives the stable handle (E1, E2, ...) shown to the model (D27).
CREATE TABLE evidence (
    id               uuid        PRIMARY KEY,
    action_id        uuid        NOT NULL UNIQUE REFERENCES actions (id),
    investigation_id uuid        NOT NULL REFERENCES investigations (id),
    ordinal          integer     NOT NULL CHECK (ordinal > 0),
    envelope         jsonb       NOT NULL,
    sha256           text        NOT NULL,
    byte_count       integer     NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (investigation_id, ordinal)
);

CREATE TABLE progress_events (
    investigation_id uuid        NOT NULL REFERENCES investigations (id),
    sequence         integer     NOT NULL CHECK (sequence > 0),
    type             text        NOT NULL,
    payload          jsonb       NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (investigation_id, sequence)
);

CREATE TABLE reports (
    investigation_id uuid        PRIMARY KEY REFERENCES investigations (id),
    schema_version   text        NOT NULL,
    outcome          text        NOT NULL CHECK (outcome IN
                         ('SUPPORTED', 'TENTATIVE', 'INCONCLUSIVE', 'NO_FAULT_OBSERVED')),
    report           jsonb       NOT NULL,
    source           text        NOT NULL CHECK (source IN ('model', 'fallback')),
    model_id         text,
    prompt_version   text,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sessions (
    token_hash  text        PRIMARY KEY,
    identity    text        NOT NULL,
    csrf_token  text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    revoked_at  timestamptz
);

CREATE TABLE login_attempts (
    attempted_at timestamptz NOT NULL DEFAULT now(),
    succeeded    boolean     NOT NULL
);
CREATE INDEX login_attempts_recent ON login_attempts (attempted_at);

-- Terminal investigations and terminal actions are immutable, whoever connects (spec §11).
CREATE FUNCTION reject_terminal_investigation_update() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF OLD.status IN ('COMPLETED', 'CANCELLED', 'FAILED') THEN
        RAISE EXCEPTION 'investigation % is terminal (%) and cannot change', OLD.id, OLD.status;
    END IF;
    IF NEW.status <> OLD.status AND NOT (
           (OLD.status = 'QUEUED' AND NEW.status IN ('RUNNING', 'CANCELLED', 'FAILED'))
        OR (OLD.status = 'RUNNING' AND NEW.status IN ('COMPLETED', 'CANCEL_REQUESTED', 'FAILED', 'QUEUED'))
        OR (OLD.status = 'CANCEL_REQUESTED' AND NEW.status = 'CANCELLED')) THEN
        RAISE EXCEPTION 'transition % -> % is not allowed', OLD.status, NEW.status;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER investigations_terminal_guard BEFORE UPDATE ON investigations
    FOR EACH ROW EXECUTE FUNCTION reject_terminal_investigation_update();

CREATE FUNCTION reject_terminal_action_update() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF OLD.status IN ('SUCCEEDED', 'ERROR', 'UNKNOWN', 'INTERRUPTED') THEN
        RAISE EXCEPTION 'action % is terminal (%) and cannot change', OLD.id, OLD.status;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER actions_terminal_guard BEFORE UPDATE ON actions
    FOR EACH ROW EXECUTE FUNCTION reject_terminal_action_update();

-- Application: no UPDATE or DELETE on evidence, progress events or reports.
GRANT SELECT, UPDATE ON control TO incidentlens_app;
GRANT SELECT, INSERT, UPDATE ON investigations TO incidentlens_app;
GRANT SELECT, INSERT ON submission_keys TO incidentlens_app;
GRANT SELECT, INSERT, UPDATE ON actions TO incidentlens_app;
GRANT SELECT, INSERT ON evidence, progress_events, reports TO incidentlens_app;
GRANT SELECT, INSERT ON sessions TO incidentlens_app;
GRANT UPDATE (revoked_at) ON sessions TO incidentlens_app;
GRANT SELECT, INSERT ON login_attempts TO incidentlens_app;
GRANT SELECT ON telemetry_samples, log_files, log_records TO incidentlens_app;

-- Collector: only the telemetry tables; it deletes telemetry older than 24 hours.
GRANT SELECT, INSERT, DELETE ON telemetry_samples, log_records TO incidentlens_collector;
GRANT SELECT, INSERT, UPDATE ON log_files TO incidentlens_collector;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO incidentlens_collector;
