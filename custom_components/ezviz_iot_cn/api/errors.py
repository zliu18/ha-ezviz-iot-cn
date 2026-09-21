"""Exception hierarchy for EZVIZ China Open Platform access."""

from __future__ import annotations


class EzvizError(Exception):
    """Base exception for this API package."""


class EzvizConnectionError(EzvizError):
    """Raised when the service cannot be reached."""


class EzvizAuthError(EzvizError):
    """Raised when credentials or an access token are rejected."""


class EzvizRateLimitError(EzvizError):
    """Raised when the service rejects a request due to rate limits."""


class EzvizNotSupportedError(EzvizError):
    """Raised when a device or account does not support an operation."""


class EzvizQueueFullError(EzvizError):
    """Raised when a bounded per-hub request queue is full."""


class EzvizSchedulerClosedError(EzvizError):
    """Raised when work is rejected or cancelled during scheduler shutdown."""


class EzvizProtocolError(EzvizError):
    """Raised when a successful response does not match the documented shape."""


class EzvizApiResponseError(EzvizError):
    """Raised for a non-success EZVIZ application response code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"EZVIZ API returned code {code}")
