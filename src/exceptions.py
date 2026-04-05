class SheafError(Exception):
    """Base exception for all sheaf errors."""


class DestinationExistsError(SheafError):
    """Raised when a copy/move destination already exists."""


class ProtocolNotFoundError(SheafError):
    """Raised when a named protocol cannot be found."""


class ProtocolValidationError(SheafError):
    """Raised when a protocol file fails validation."""


class DatabaseError(SheafError):
    """Raised on database operation failures."""


class AdapterError(SheafError):
    """Raised on frontier model adapter failures."""


class ArchiveConfigError(SheafError):
    """Raised when archive root is not configured or invalid."""
