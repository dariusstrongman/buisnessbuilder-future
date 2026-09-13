class IdentityError(Exception):
    """Base identity-domain failure."""


class IdentityNotFound(IdentityError):
    pass


class IdentityConflict(IdentityError):
    pass


class AuthorizationDenied(PermissionError, IdentityError):
    pass


class InvalidIdentityTransition(IdentityError):
    pass
