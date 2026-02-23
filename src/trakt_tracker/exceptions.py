class TraktAuthenticationError(RuntimeError):
    """Raised when Trakt authentication fails, e.g., revoked/expired refresh token."""
    pass
