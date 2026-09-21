"""Tests for n8n_reliability.retry.RetryPolicy.

The retry policy mirrors what n8n does when a node sets ``retryOnFail`` —
expressed in plain Python so the backoff behavior is testable without an
n8n instance. No network, no sleeping in tests: the sleep function is
injected and recorded.
"""

import random

import pytest

from n8n_reliability.retry import RetryPolicy


def _recorder():
    calls = []

    def sleep(seconds):
        calls.append(seconds)

    return calls, sleep


def test_succeeds_first_try_calls_func_once():
    calls, sleep = _recorder()
    policy = RetryPolicy(sleep=sleep)
    seen = []

    result = policy.run(lambda: seen.append(1) or "ok")

    assert result == "ok"
    assert seen == [1]
    assert calls == []


def test_retries_then_succeeds_counts_attempts():
    calls, sleep = _recorder()
    policy = RetryPolicy(max_attempts=3, base_delay=1.0, jitter=0.0, sleep=sleep)
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("boom")
        return "recovered"

    assert policy.run(flaky) == "recovered"
    assert len(attempts) == 3
    assert calls == [1.0, 2.0]  # exponential: base, base * factor


def test_raises_last_error_after_exhaustion():
    calls, sleep = _recorder()
    policy = RetryPolicy(max_attempts=3, base_delay=1.0, jitter=0.0, sleep=sleep)
    attempts = []

    def always_fails():
        attempts.append(1)
        raise ConnectionError(f"failure #{len(attempts)}")

    with pytest.raises(ConnectionError, match="failure #3"):
        policy.run(always_fails)

    assert len(attempts) == 3  # first try + 2 retries, then it stops
    assert calls == [1.0, 2.0]


def test_no_retry_when_max_attempts_is_one():
    calls, sleep = _recorder()
    policy = RetryPolicy(max_attempts=1, sleep=sleep)
    attempts = []

    def fails():
        attempts.append(1)
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError, match="nope"):
        policy.run(fails)

    assert len(attempts) == 1
    assert calls == []


def test_exponential_backoff_schedule():
    calls, sleep = _recorder()
    policy = RetryPolicy(
        max_attempts=5, base_delay=2.0, backoff_factor=3.0, jitter=0.0, sleep=sleep
    )
    assert policy.delays() == [2.0, 6.0, 18.0, 54.0]


def test_delays_empty_when_single_attempt():
    policy = RetryPolicy(max_attempts=1)
    assert policy.delays() == []


def test_jitter_stays_within_bounds():
    rng = random.Random(42)
    policy = RetryPolicy(
        max_attempts=3, base_delay=2.0, backoff_factor=2.0, jitter=0.5, rng=rng
    )
    delays = policy.delays()
    assert len(delays) == 2
    assert 2.0 <= delays[0] <= 3.0  # base * (1 + jitter)
    assert 4.0 <= delays[1] <= 6.0


def test_non_retryable_error_raises_immediately():
    calls, sleep = _recorder()
    policy = RetryPolicy(
        max_attempts=5, retryable=lambda exc: False, sleep=sleep
    )
    attempts = []

    def fails():
        attempts.append(1)
        raise ValueError("client bug, retrying is pointless")

    with pytest.raises(ValueError):
        policy.run(fails)

    assert len(attempts) == 1  # never retried
    assert calls == []


def test_retryable_predicate_is_selective():
    calls, sleep = _recorder()
    policy = RetryPolicy(
        max_attempts=4,
        base_delay=1.0,
        jitter=0.0,
        retryable=lambda exc: isinstance(exc, ConnectionError),
        sleep=sleep,
    )

    def flaky_then_fatal():
        flaky_then_fatal.n += 1
        if flaky_then_fatal.n == 1:
            raise ConnectionError("transient")
        raise TypeError("permanent")

    flaky_then_fatal.n = 0

    with pytest.raises(TypeError, match="permanent"):
        policy.run(flaky_then_fatal)

    assert flaky_then_fatal.n == 2  # retried the transient one, stopped at the fatal one
    assert calls == [1.0]


def test_on_retry_callback_receives_attempt_info():
    calls, sleep = _recorder()
    seen = []
    policy = RetryPolicy(
        max_attempts=3,
        base_delay=1.0,
        jitter=0.0,
        sleep=sleep,
        on_retry=lambda attempt, exc, delay: seen.append((attempt, str(exc), delay)),
    )

    def fails_twice():
        fails_twice.n += 1
        if fails_twice.n < 3:
            raise ConnectionError(f"down {fails_twice.n}")
        return "up"

    fails_twice.n = 0

    assert policy.run(fails_twice) == "up"
    assert seen == [(1, "down 1", 1.0), (2, "down 2", 2.0)]


def test_args_and_kwargs_pass_through():
    policy = RetryPolicy(sleep=lambda s: None)

    def add(a, b, scale=1):
        return (a + b) * scale

    assert policy.run(add, 2, 3, scale=10) == 50


def test_invalid_max_attempts_raises():
    with pytest.raises(ValueError, match="max_attempts"):
        RetryPolicy(max_attempts=0)


def test_negative_base_delay_raises():
    with pytest.raises(ValueError, match="base_delay"):
        RetryPolicy(base_delay=-1.0)


def test_backoff_factor_below_one_raises():
    with pytest.raises(ValueError, match="backoff_factor"):
        RetryPolicy(backoff_factor=0.5)


def test_jitter_out_of_range_raises():
    with pytest.raises(ValueError, match="jitter"):
        RetryPolicy(jitter=1.0)
    with pytest.raises(ValueError, match="jitter"):
        RetryPolicy(jitter=-0.1)
