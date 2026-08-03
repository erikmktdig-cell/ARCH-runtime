CREATE INDEX ix_project_aggregates_updated_at
    ON project_aggregates(updated_at);

CREATE INDEX ix_project_aggregates_contract
    ON project_aggregates(contract_name, contract_version);

CREATE INDEX ix_project_events_project_position
    ON project_events(project_id, stream_position);

CREATE INDEX ix_project_events_idempotency_key
    ON project_events(idempotency_key);

CREATE UNIQUE INDEX uq_project_events_state_version
    ON project_events(project_id, aggregate_version_after)
    WHERE is_state_change = 1;

CREATE UNIQUE INDEX uq_project_snapshots_project_version
    ON project_snapshots(project_id, aggregate_version);

CREATE INDEX ix_project_snapshots_project_position
    ON project_snapshots(project_id, last_stream_position);

CREATE INDEX ix_idempotency_records_project
    ON idempotency_records(project_id)
    WHERE project_id IS NOT NULL;
