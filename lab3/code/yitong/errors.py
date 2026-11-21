"""Application-level exceptions used by the 一同记账 prototype."""


class AppError(Exception):
    """Base exception for expected application failures."""


class ValidationError(AppError):
    """Raised when user input does not satisfy a business rule."""


class AuthenticationError(AppError):
    """Raised when login credentials are invalid or an account is disabled."""


class AuthorizationError(AppError):
    """Raised when the actor lacks the required ledger permission."""


class NotFoundError(AppError):
    """Raised when a requested object cannot be found."""


class ConflictError(AppError):
    """Raised when the current state makes an operation impossible."""


class InvalidStateError(ConflictError):
    """Raised when a state transition is not allowed."""


class DuplicateSubmissionError(ConflictError):
    """Raised when the request key refers to an unexpired request."""


class SuspiciousDuplicateError(ConflictError):
    """Raised when the record looks duplicated and needs confirmation."""


class SettlementIncompleteError(ConflictError):
    """Raised when a settlement cannot complete yet."""
