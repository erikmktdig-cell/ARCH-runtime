from importlib.metadata import version

import pytest

import arch_runtime
from arch_runtime.errors import (
    ArchRuntimeError,
    CorruptStoredRecordError,
    RuntimeConfigurationError,
)

pytestmark = pytest.mark.unit


def test_version_has_single_public_source() -> None:
    assert arch_runtime.__version__ == "0.2.0"
    assert arch_runtime.__version__ == version("arch-runtime")
    assert arch_runtime.__all__ == tuple(sorted(arch_runtime.__all__))
    assert {"Runtime", "RuntimeConfig", "GetProjectResult", "__version__"} <= set(
        arch_runtime.__all__
    )


@pytest.mark.parametrize("error_type", [RuntimeConfigurationError, CorruptStoredRecordError])
def test_runtime_errors_share_a_stable_base(error_type: type[ArchRuntimeError]) -> None:
    error = error_type("failure", operation="test", remediation="retry safely")
    assert isinstance(error, ArchRuntimeError)
    assert str(error) == "failure"
