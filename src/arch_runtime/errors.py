"""Stable exception taxonomy for future runtime work packages."""


class ArchRuntimeError(Exception):
    """Base class for errors raised by the runtime package."""


class RuntimeConfigurationError(ArchRuntimeError):
    """Raised when runtime configuration is invalid."""


class RuntimeIntegrityError(ArchRuntimeError):
    """Raised when persisted runtime evidence violates integrity rules."""
