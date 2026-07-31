from importlib.metadata import version

import pytest

import arch_runtime
from arch_runtime.errors import (
    ArchRuntimeError,
    RuntimeConfigurationError,
    RuntimeIntegrityError,
)

pytestmark = pytest.mark.unit


def test_version_has_single_public_source() -> None:
    assert arch_runtime.__version__ == "0.1.0"
    assert arch_runtime.__version__ == version("arch-runtime")
    assert arch_runtime.__all__ == ("__version__",)


@pytest.mark.parametrize("error_type", [RuntimeConfigurationError, RuntimeIntegrityError])
def test_runtime_errors_share_a_stable_base(error_type: type[ArchRuntimeError]) -> None:
    error = error_type("failure")
    assert isinstance(error, ArchRuntimeError)
    assert str(error) == "failure"
