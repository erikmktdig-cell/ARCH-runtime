from typing import Any
from unittest.mock import Mock

import pytest
from arch_kernel.contracts import EventEnvelope, ProjectState, SemanticVersion

from arch_runtime.errors import CorruptStoredRecordError, UnsupportedStoredContractError
from arch_runtime.persistence.sqlite import codec as codec_module
from arch_runtime.persistence.sqlite.codec import KernelContractCodec
from tests.fakes.adapters import InMemoryEventStore, stored_project_from_state

pytestmark = pytest.mark.sqlite


@pytest.mark.parametrize(
    "changes",
    [
        {"state_json": b"{}"},
        {"content_fingerprint": "sha256:" + "e" * 64},
        {"schema_fingerprint": "sha256:" + "e" * 64},
    ],
)
def test_codec_rejects_corrupt_project_evidence(
    project_state: ProjectState,
    changes: dict[str, Any],
) -> None:
    stored = stored_project_from_state(project_state).model_copy(update=changes)
    with pytest.raises(CorruptStoredRecordError):
        KernelContractCodec().verify_project(stored)


def test_codec_rejects_project_contract_version(project_state: ProjectState) -> None:
    stored = stored_project_from_state(project_state).model_copy(
        update={"contract_version": SemanticVersion.parse("99.0.0")}
    )
    with pytest.raises(UnsupportedStoredContractError):
        KernelContractCodec().verify_project(stored)


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"event_json": b"{}"}, CorruptStoredRecordError),
        ({"schema_version": SemanticVersion.parse("99.0.0")}, UnsupportedStoredContractError),
        ({"event_fingerprint": "sha256:" + "e" * 64}, CorruptStoredRecordError),
    ],
)
def test_codec_rejects_corrupt_event_evidence(
    event_envelope: EventEnvelope,
    changes: dict[str, Any],
    error: type[Exception],
) -> None:
    store = InMemoryEventStore()
    store.append(event_envelope, version_before=0, version_after=1, previous_event_fingerprint=None)
    with pytest.raises(error):
        KernelContractCodec().verify_event(store.records[0].model_copy(update=changes))


@pytest.mark.parametrize("payload", [b"\xff", b"not-json", b'{"x": NaN}', b'{ "x": 1 }', b"{}"])
def test_codec_rejects_invalid_json_evidence(payload: bytes) -> None:
    with pytest.raises(CorruptStoredRecordError):
        KernelContractCodec().verify_json_evidence(payload, "sha256:" + "a" * 64)


def test_codec_requires_registered_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = Mock()
    registry.get_by_model.return_value = None
    monkeypatch.setattr(codec_module, "build_builtin_contract_registry", lambda: registry)
    with pytest.raises(UnsupportedStoredContractError, match="not registered"):
        KernelContractCodec()
