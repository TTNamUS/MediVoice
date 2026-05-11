"""Exponential backoff retry decorator for external API calls.

Applies to: Anthropic, Deepgram, Cartesia, Qdrant — any service that can
return 5xx or transient connection errors.

Usage:
    @with_retry(max_attempts=3)
    async def my_api_call(): ...

On failure after all retries, the last exception is re-raised.
If any attempt returns successfully, a "one moment please" prompt is NOT
injected here — that's handled at the pipeline level via a timeout frame.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable
from typing import TypeVar

from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable)

# Retry on these exception base types — covers httpx, grpc, and asyncpg
_RETRYABLE = (
    ConnectionError,
    TimeoutError,
    OSError,
)

try:
    import httpx

    _RETRYABLE = (*_RETRYABLE, httpx.TransportError, httpx.TimeoutException)
except ImportError:
    pass


def with_retry(max_attempts: int = 3, min_wait: float = 1.0, max_wait: float = 4.0):
    """Decorator: retry async function with exponential backoff (1s → 2s → 4s)."""

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            last_exc: Exception | None = None
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(max_attempts),
                wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
                retry=retry_if_exception_type(_RETRYABLE),
                reraise=True,
            ):
                with attempt:
                    try:
                        return await fn(*args, **kwargs)
                    except _RETRYABLE as e:
                        attempt_num = attempt.retry_state.attempt_number
                        logger.warning(
                            "%s attempt %d/%d failed: %s",
                            fn.__name__,
                            attempt_num,
                            max_attempts,
                            e,
                        )
                        last_exc = e
                        raise
            if last_exc:
                raise last_exc

        return wrapper  # type: ignore[return-value]

    return decorator


async def with_timeout_prompt(
    coro,
    timeout_s: float = 1.5,
    on_slow_callback=None,
):
    """Run coro; if it takes > timeout_s, call on_slow_callback (inject 'one moment' prompt).

    The coro still runs to completion — this is for UX only.

    Args:
        coro: The async operation to run (e.g., a tool call).
        timeout_s: Threshold in seconds before firing on_slow_callback.
        on_slow_callback: Optional async callable fired once when threshold is hit.
    """
    slow_fired = False

    async def _watchdog():
        nonlocal slow_fired
        await asyncio.sleep(timeout_s)
        if on_slow_callback and not slow_fired:
            slow_fired = True
            try:
                await on_slow_callback()
            except Exception:
                pass

    watchdog_task = asyncio.create_task(_watchdog())
    try:
        result = await coro
        return result
    finally:
        watchdog_task.cancel()
        try:
            await watchdog_task
        except asyncio.CancelledError:
            pass
