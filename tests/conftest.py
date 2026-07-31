from datetime import UTC, datetime
from pathlib import Path

import pytest
from arch_kernel.contracts import EventEnvelope, ProjectState

from tests.fakes.adapters import FrozenClock

FIXTURES = Path(__file__).parent / "fixtures" / "kernel"


@pytest.fixture
def project_state() -> ProjectState:
    return ProjectState.model_validate_json(
        (FIXTURES / "minimal_project_state.json").read_bytes(),
        strict=True,
    )


@pytest.fixture
def event_envelope() -> EventEnvelope:
    return EventEnvelope.model_validate_json(
        (FIXTURES / "event_envelope.json").read_bytes(),
        strict=True,
    )


@pytest.fixture
def frozen_clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 7, 31, 12, 0, tzinfo=UTC))
