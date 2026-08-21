"""Token bucket, circuit breaker, retry, exponential backoff, and jitter."""

from __future__ import annotations

import asyncio
import random
import time
from enum import Enum
from typing import Callable, Optional

from agent_runtime.config import (
    CircuitBreakerConfig,
    RateLimitConfig,
    RetryConfig,
)
from agent_runtime.errors import CircuitOpenError, TransientLLMError
from agent_runtime.llm.base import LLMClient
from agent_runtime.metrics import MetricsRegistry
from agent_runtime.models import LLMRequest, LLMResponse


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class TokenBucket:
    def __init__(
        self,
        config: RateLimitConfig,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        config.validate()
        self.capacity = float(config.capacity)
        self.refill_rate = float(config.refill_per_second)
        self._tokens = self.capacity
        self._updated_at = clock()
        self._clock = clock
        self._lock = asyncio.Lock()

    @property
    def available_tokens(self) -> float:
        return self._tokens

    async def acquire(self, tokens: float = 1.0) -> None:
        if tokens <= 0 or tokens > self.capacity:
            raise ValueError("requested tokens must be in (0, capacity]")
        while True:
            async with self._lock:
                now = self._clock()
                elapsed = max(0.0, now - self._updated_at)
                self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_rate)
                self._updated_at = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                wait_seconds = (tokens - self._tokens) / self.refill_rate
            await asyncio.sleep(wait_seconds)


class CircuitBreaker:
    def __init__(
        self,
        config: CircuitBreakerConfig,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        config.validate()
        self.config = config
        self.state = BreakerState.CLOSED
        self.failure_count = 0
        self._opened_at: Optional[float] = None
        self._half_open_calls = 0
        self._clock = clock
        self._lock = asyncio.Lock()

    async def before_call(self) -> None:
        async with self._lock:
            if self.state == BreakerState.OPEN:
                elapsed = self._clock() - (self._opened_at or 0.0)
                if elapsed < self.config.recovery_timeout_seconds:
                    raise CircuitOpenError("circuit breaker is open")
                self.state = BreakerState.HALF_OPEN
                self._half_open_calls = 0
            if self.state == BreakerState.HALF_OPEN:
                if self._half_open_calls >= self.config.half_open_max_calls:
                    raise CircuitOpenError("circuit breaker half-open probe is busy")
                self._half_open_calls += 1

    async def record_success(self) -> None:
        async with self._lock:
            self.state = BreakerState.CLOSED
            self.failure_count = 0
            self._opened_at = None
            self._half_open_calls = 0

    async def record_failure(self) -> None:
        async with self._lock:
            self.failure_count += 1
            if (
                self.state == BreakerState.HALF_OPEN
                or self.failure_count >= self.config.failure_threshold
            ):
                self.state = BreakerState.OPEN
                self._opened_at = self._clock()
                self._half_open_calls = 0

    async def release_probe(self) -> None:
        """Release a cancelled half-open probe without treating it as a result."""

        async with self._lock:
            if self.state == BreakerState.HALF_OPEN:
                self._half_open_calls = max(0, self._half_open_calls - 1)

    def snapshot(self):
        return {"state": self.state.value, "failure_count": self.failure_count}


class ResilientLLMClient:
    def __init__(
        self,
        provider: LLMClient,
        retry: RetryConfig,
        limiter: TokenBucket,
        breaker: CircuitBreaker,
        metrics: Optional[MetricsRegistry] = None,
        random_fn: Callable[[], float] = random.random,
    ) -> None:
        retry.validate()
        self.provider = provider
        self.retry = retry
        self.limiter = limiter
        self.breaker = breaker
        self.metrics = metrics or MetricsRegistry()
        self._random = random_fn

    async def complete(self, request: LLMRequest) -> LLMResponse:
        last_error: Optional[BaseException] = None
        for attempt in range(1, self.retry.max_attempts + 1):
            await self.limiter.acquire()
            try:
                await self.breaker.before_call()
            except CircuitOpenError:
                self.metrics.increment("circuit_open_rejections_total")
                raise
            self.metrics.increment("llm_attempts_total")
            try:
                response = await self.provider.complete(request)
            except asyncio.CancelledError:
                await self.breaker.release_probe()
                raise
            except TransientLLMError as exc:
                last_error = exc
                await self.breaker.record_failure()
                self.metrics.increment("llm_transient_errors_total")
                if attempt >= self.retry.max_attempts:
                    break
                delay = min(
                    self.retry.max_delay_seconds,
                    self.retry.base_delay_seconds * (2 ** (attempt - 1)),
                )
                jitter = delay * self.retry.jitter_ratio * self._random()
                self.metrics.increment("llm_retries_total")
                await asyncio.sleep(delay + jitter)
                continue
            except Exception:
                # A non-transient response proves the provider is reachable; do not
                # leave a half-open breaker probe permanently occupied.
                await self.breaker.record_success()
                self.metrics.increment("llm_permanent_errors_total")
                raise
            await self.breaker.record_success()
            self.metrics.increment("llm_success_total")
            return response
        assert last_error is not None
        raise last_error
