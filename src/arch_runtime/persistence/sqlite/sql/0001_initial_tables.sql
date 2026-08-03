CREATE TABLE runtime_schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
) STRICT;

CREATE TABLE project_aggregates (
    project_id TEXT PRIMARY KEY,
    record_version INTEGER NOT NULL CHECK (record_version >= 1),
    contract_name TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    schema_fingerprint TEXT NOT NULL,
    record_fingerprint TEXT NOT NULL,
    content_fingerprint TEXT NOT NULL,
    state_json BLOB NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE project_events (
    stream_position INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    aggregate_version_before INTEGER NOT NULL CHECK (aggregate_version_before >= 0),
    aggregate_version_after INTEGER NOT NULL CHECK (aggregate_version_after >= 0),
    is_state_change INTEGER NOT NULL CHECK (is_state_change IN (0, 1)),
    event_type TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    event_fingerprint TEXT NOT NULL,
    previous_event_fingerprint TEXT,
    event_json BLOB NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES project_aggregates(project_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    CHECK (
        (is_state_change = 1 AND aggregate_version_after = aggregate_version_before + 1)
        OR
        (is_state_change = 0 AND aggregate_version_after = aggregate_version_before)
    )
) STRICT;

CREATE TABLE project_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    aggregate_version INTEGER NOT NULL CHECK (aggregate_version >= 1),
    last_stream_position INTEGER NOT NULL CHECK (last_stream_position >= 1),
    contract_name TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    schema_fingerprint TEXT NOT NULL,
    record_fingerprint TEXT NOT NULL,
    content_fingerprint TEXT NOT NULL,
    state_json BLOB NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES project_aggregates(project_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
) STRICT;

CREATE TABLE idempotency_records (
    operation_name TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    project_id TEXT,
    request_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('in_progress', 'completed')),
    result_fingerprint TEXT,
    result_json BLOB,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (operation_name, idempotency_key),
    CHECK (
        (status = 'in_progress' AND result_fingerprint IS NULL
            AND result_json IS NULL AND completed_at IS NULL)
        OR
        (status = 'completed' AND result_fingerprint IS NOT NULL
            AND result_json IS NOT NULL AND completed_at IS NOT NULL)
    )
) STRICT;
