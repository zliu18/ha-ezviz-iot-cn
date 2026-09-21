"""In-memory observation of EZVIZ Open Platform request usage."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class ApiUsageSnapshot:
    """Content-free request counters for the current integration runtime."""

    request_attempts: int
    rate_limit_events: int
    started_at: datetime
    last_request_at: datetime | None
    last_rate_limit_at: datetime | None


class ApiUsageTracker:
    """Count actual API attempts without estimating an official quota."""

    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(tz=UTC))
        self._started_at = self._now()
        self._request_attempts = 0
        self._rate_limit_events = 0
        self._last_request_at: datetime | None = None
        self._last_rate_limit_at: datetime | None = None
        self._listeners: set[Callable[[bool], None]] = set()

    def subscribe(self, listener: Callable[[bool], None]) -> Callable[[], None]:
        """Subscribe to local counters without creating API traffic."""
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    def refresh(self) -> None:
        """Publish the current local snapshot immediately on user request."""
        for listener in tuple(self._listeners):
            listener(True)

    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener(False)

    @property
    def snapshot(self) -> ApiUsageSnapshot:
        """Return one immutable observation snapshot."""
        return ApiUsageSnapshot(
            request_attempts=self._request_attempts,
            rate_limit_events=self._rate_limit_events,
            started_at=self._started_at,
            last_request_at=self._last_request_at,
            last_rate_limit_at=self._last_rate_limit_at,
        )

    def record_request(self) -> None:
        """Record one actual HTTP request attempt."""
        self._request_attempts += 1
        self._last_request_at = self._now()
        self._notify()

    def record_rate_limit(self) -> None:
        """Record one HTTP or application-level rate-limit response."""
        self._rate_limit_events += 1
        self._last_rate_limit_at = self._now()
        self._notify()
