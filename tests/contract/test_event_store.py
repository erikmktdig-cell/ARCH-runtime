import pytest
from arch_kernel.contracts import EventEnvelope

from arch_runtime.errors import ConcurrentModificationError
from arch_runtime.ports import EventStore

pytestmark = pytest.mark.contract


def test_event_chain_tail_is_scoped_to_the_project(
    event_envelope: EventEnvelope,
    event_store: EventStore,
) -> None:
    first_position = event_store.append(
        event_envelope,
        version_before=1,
        version_after=2,
        previous_event_fingerprint=None,
    )
    first = event_store.read_stream(event_envelope.project_id)[0]
    second_event = event_envelope.model_copy(
        update={"event_id": type(event_envelope.event_id)("EVT-01HZX7M3FQ1T2Q9V8Y6K4C2B1H")}
    )
    second_position = event_store.append(
        second_event,
        version_before=2,
        version_after=2,
        previous_event_fingerprint=first.event_fingerprint,
    )

    assert (first_position, second_position) == (1, 2)
    remaining = event_store.read_stream(event_envelope.project_id, after_position=1)
    assert remaining[0].stream_position == 2


def test_event_store_rejects_stale_project_tail(
    event_envelope: EventEnvelope,
    event_store: EventStore,
) -> None:
    event_store.append(
        event_envelope,
        version_before=1,
        version_after=2,
        previous_event_fingerprint=None,
    )

    with pytest.raises(ConcurrentModificationError):
        event_store.append(
            event_envelope,
            version_before=2,
            version_after=3,
            previous_event_fingerprint=None,
        )
