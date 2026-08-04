"""Shared construction of authoritative project event envelopes."""

from datetime import datetime

from arch_kernel.contracts import (
    ActorType,
    EventActor,
    EventEnvelope,
    EventId,
    ProjectId,
    SemanticVersion,
    TypedReference,
)


def runtime_contract_version() -> SemanticVersion:
    return SemanticVersion.parse("1.0.0")


def build_project_event(
    *,
    event_id: EventId,
    event_type: str,
    semantic_name: str,
    project_id: ProjectId,
    aggregate_version: SemanticVersion,
    idempotency_key: str,
    actor_type: ActorType,
    actor_id: str,
    actor_display_name: str | None,
    occurred_at: datetime,
    payload: dict[str, object],
) -> EventEnvelope:
    """Build one K04 envelope whose event_type remains the routing authority."""

    actor = EventActor(
        contract_name="event_actor",
        contract_version=runtime_contract_version(),
        schema_uri="urn:arch:contracts:event_actor:1.0.0",
        created_at=occurred_at,
        updated_at=None,
        actor_type=actor_type,
        actor_id=actor_id,
        display_name=actor_display_name,
    )
    aggregate = TypedReference(
        target_type="project",
        target_id=project_id,
        target_version=aggregate_version,
        relation="describes",
    )
    return EventEnvelope.model_validate(
        {
            "contract_name": "event_envelope",
            "contract_version": runtime_contract_version(),
            "schema_uri": "urn:arch:contracts:event_envelope:1.0.0",
            "created_at": occurred_at,
            "updated_at": None,
            "event_id": event_id,
            "event_type": event_type,
            "project_id": project_id,
            "aggregate": aggregate,
            "occurred_at": occurred_at,
            "recorded_at": occurred_at,
            "actor": actor,
            "correlation_id": None,
            "causation_id": None,
            "idempotency_key": idempotency_key,
            "schema_version": runtime_contract_version(),
            "payload": {"event_name": semantic_name, **payload},
        }
    )
