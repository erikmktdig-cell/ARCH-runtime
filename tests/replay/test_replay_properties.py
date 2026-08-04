from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from arch_runtime.errors import ReplayIntegrityError
from arch_runtime.replay import ReplayService
from tests.replay.conftest import ReplaySeed

pytestmark = pytest.mark.unit


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=(HealthCheck.function_scoped_fixture,),
)
@given(version_before=st.integers(min_value=2, max_value=10_000))
def test_any_version_discontinuity_fails_closed(
    replay_seed: ReplaySeed, version_before: int
) -> None:
    tail = replay_seed.uow._events.records[-1]
    try:
        replay_seed.uow._events.records[-1] = tail.model_copy(
            update={
                "aggregate_version_before": version_before,
                "aggregate_version_after": version_before + 1,
            }
        )
        with pytest.raises(ReplayIntegrityError):
            ReplayService(unit_of_work_factory=lambda: replay_seed.uow).replay_project(
                replay_seed.project_id
            )
    finally:
        replay_seed.uow._events.records[-1] = tail


@settings(
    max_examples=16,
    deadline=None,
    suppress_health_check=(HealthCheck.function_scoped_fixture,),
)
@given(digit=st.sampled_from(tuple("0123456789abcdef")))
def test_any_changed_event_fingerprint_fails_closed(replay_seed: ReplaySeed, digit: str) -> None:
    tail = replay_seed.uow._events.records[-1]
    candidate = "sha256:" + digit * 64
    if candidate == tail.event_fingerprint:
        candidate = "sha256:" + ("0" if digit != "0" else "1") * 64
    try:
        replay_seed.uow._events.records[-1] = tail.model_copy(
            update={"event_fingerprint": candidate}
        )
        with pytest.raises(ReplayIntegrityError):
            ReplayService(unit_of_work_factory=lambda: replay_seed.uow).replay_project(
                replay_seed.project_id
            )
    finally:
        replay_seed.uow._events.records[-1] = tail
