"""
Rate limiting and retry utilities.

Provides:
- @rate_limited decorator: token bucket rate limiting per callable
- retry_on_http_error: tenacity-based retry for transient HTTP failures
"""

import time
import threading
from functools import wraps
from typing import Callable, TypeVar, Any

from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)
import logging

logger = logging.getLogger("advisor.rate_limiter")

F = TypeVar("F", bound=Callable[..., Any])


class TokenBucket:
    """Thread-safe token bucket for rate limiting."""

    def __init__(self, calls_per_minute: int, max_burst: int = 1):
        self.calls_per_minute = calls_per_minute
        self.tokens = 1.0              # Start with 1 token — prevents burst on startup
        # max_burst=1 prevents the bucket from accumulating tokens during long idle
        # periods (e.g. while waiting for an AI call), which would cause a burst when
        # the next phase starts.  Callers that genuinely need burst can pass max_burst>1.
        self.max_tokens = float(max_burst)
        self.refill_rate = calls_per_minute / 60.0  # tokens per second
        self.lock = threading.Lock()
        self.last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.max_tokens, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    def acquire(self) -> None:
        """Block until a token is available."""
        while True:
            with self.lock:
                self._refill()
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
            time.sleep(0.1)


_buckets: dict[str, TokenBucket] = {}
_buckets_lock = threading.Lock()


def rate_limited(
    calls_per_minute: int,
    key: str | None = None,
    max_burst: int = 1,
) -> Callable[[F], F]:
    """
    Decorator that rate-limits a function to `calls_per_minute` calls.

    max_burst controls how many tokens the bucket may accumulate when idle.
    Default is 1, which prevents burst calls after long pauses (e.g. between
    pipeline phases).  Set max_burst > 1 only for APIs that explicitly allow it.

    Usage:
        @rate_limited(calls_per_minute=30)
        def call_api(...): ...
    """
    def decorator(func: F) -> F:
        bucket_key = key or f"{func.__module__}.{func.__qualname__}"
        with _buckets_lock:
            if bucket_key not in _buckets:
                _buckets[bucket_key] = TokenBucket(calls_per_minute, max_burst=max_burst)
        bucket = _buckets[bucket_key]

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            bucket.acquire()
            return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]
    return decorator


def make_retry(
    max_attempts: int = 3,
    min_wait: float = 2.0,
    max_wait: float = 30.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[F], F]:
    """
    Return a tenacity retry decorator for transient errors.

    Usage:
        @make_retry(max_attempts=3, exceptions=(requests.exceptions.RequestException,))
        def call_api(...): ...
    """
    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
        retry=retry_if_exception_type(exceptions),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
