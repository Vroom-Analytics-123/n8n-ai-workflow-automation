"""Exponential-backoff retry policy for flaky external calls.

This is the Python mirror of what n8n does when a node sets ``retryOnFail``:
try the call, wait a growing delay, try again, and only give up after the
configured attempts. Expressing it in plain Python makes the backoff
schedule testable without an n8n instance — no network, no real sleeping
(the sleep function is injectable).
"""

from __future__ import annotations

import random
import time
from typing import Callable, TypeVar

T = TypeVar("T")

Retryable = Callable[[Exception], bool]
OnRetry = Callable[[int, Exception, float], None]


class RetryPolicy:
    """Run a callable, retrying failures with exponential backoff.

    Args:
        max_attempts: Total attempts including the first try. Must be >= 1.
            ``1`` means "try once, never retry".
        base_delay: Seconds to wait before the first retry.
        backoff_factor: Each subsequent delay is multiplied by this (>= 1).
        jitter: Fraction of each delay added as random jitter, in [0, 1).
            ``0`` disables jitter (deterministic delays, handy for tests).
        retryable: Predicate deciding whether an exception is worth retrying.
            Defaults to retrying everything. Return ``False`` for permanent
            failures (bad input, auth errors) so they surface immediately.
        on_retry: Optional callback ``(attempt, exception, delay_seconds)``
            invoked before each retry — wire it to your logging/alerting.
        sleep: Injectable sleep function. Tests pass a recorder; production
            passes ``time.sleep`` (the default).
        rng: Injectable random generator for jitter. Tests seed it.
    """

    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        backoff_factor: float = 2.0,
        jitter: float = 0.1,
        retryable: Retryable | None = None,
        on_retry: OnRetry | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if base_delay < 0:
            raise ValueError("base_delay must be >= 0")
        if backoff_factor < 1:
            raise ValueError("backoff_factor must be >= 1")
        if not 0 <= jitter < 1:
            raise ValueError("jitter must be in [0, 1)")
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.backoff_factor = backoff_factor
        self.jitter = jitter
        self.retryable = retryable if retryable is not None else (lambda exc: True)
        self.on_retry = on_retry
        self.sleep = sleep
        self.rng = rng if rng is not None else random.Random()

    def delays(self) -> list[float]:
        """Planned delays (seconds) before attempts 2..N.

        Empty when ``max_attempts == 1``. With ``jitter=0`` the schedule is
        exactly ``base_delay * backoff_factor**k``.
        """
        planned: list[float] = []
        for step in range(self.max_attempts - 1):
            nominal = self.base_delay * (self.backoff_factor**step)
            if self.jitter:
                nominal = nominal * (1 + self.rng.uniform(0, self.jitter))
            planned.append(nominal)
        return planned

    def run(self, func: Callable[..., T], *args, **kwargs) -> T:
        """Call ``func`` with retry. Returns its result.

        Raises:
            The last exception raised by ``func`` once attempts are
            exhausted, or immediately if ``retryable`` says the error is
            permanent.
        """
        plan = self.delays()
        for attempt in range(1, self.max_attempts + 1):
            try:
                return func(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - policy is exception-agnostic
                if not self.retryable(exc) or attempt == self.max_attempts:
                    raise
                delay = plan[attempt - 1]
                if self.on_retry is not None:
                    self.on_retry(attempt, exc, delay)
                self.sleep(delay)
        raise AssertionError("unreachable: loop always returns or raises")
