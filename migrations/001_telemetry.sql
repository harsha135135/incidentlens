-- Telemetry collected by the IncidentLens collector. Kept for 24 hours (spec §11).

-- Periodic samples: database blocker samples, collector heartbeats and explicit gap records.
-- A sample with coverage UNAVAILABLE records a failed collection, so missing data is visible.
CREATE TABLE telemetry_samples (
    id                bigserial   PRIMARY KEY,
    service           text        NOT NULL,
    kind              text        NOT NULL,
    observed_at       timestamptz NOT NULL,
    collector_version text        NOT NULL,
    coverage          text        NOT NULL CHECK (coverage IN ('COMPLETE', 'PARTIAL', 'UNAVAILABLE')),
    payload           jsonb       NOT NULL
);

CREATE INDEX telemetry_samples_lookup ON telemetry_samples (service, kind, observed_at);

-- One row per log file read. A rotated or truncated file gets a new row, so byte offsets never
-- collide. read_offset is the end of the last complete line stored.
CREATE TABLE log_files (
    id            bigserial   PRIMARY KEY,
    service       text        NOT NULL,
    source        text        NOT NULL,
    path          text        NOT NULL,
    device        bigint      NOT NULL,
    inode         bigint      NOT NULL,
    read_offset   bigint      NOT NULL DEFAULT 0 CHECK (read_offset >= 0),
    state         text        NOT NULL CHECK (state IN ('ACTIVE', 'FINISHED')),
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX log_files_one_active ON log_files (service, source) WHERE state = 'ACTIVE';

-- Parsed and redacted log lines. Stored in the same transaction that advances read_offset;
-- (source_file_id, byte_offset) makes a re-read after a crash harmless.
CREATE TABLE log_records (
    id             bigserial   PRIMARY KEY,
    source_file_id bigint      NOT NULL REFERENCES log_files (id),
    byte_offset    bigint      NOT NULL,
    service        text        NOT NULL,
    source         text        NOT NULL,
    observed_at    timestamptz,
    record_type    text        NOT NULL,
    event_id       text,
    fields         jsonb       NOT NULL,
    ingested_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_file_id, byte_offset)
);

CREATE INDEX log_records_lookup ON log_records (service, record_type, observed_at);
CREATE INDEX log_records_event ON log_records (event_id) WHERE event_id IS NOT NULL;
