class CompanyBrainError(Exception):
    """Base error for fail-closed Company Brain operations."""


class NotFoundError(CompanyBrainError):
    pass


class ConflictError(CompanyBrainError):
    pass


class InvalidTransitionError(CompanyBrainError):
    pass


class ScopeError(CompanyBrainError):
    pass


class ValidationError(CompanyBrainError):
    pass
