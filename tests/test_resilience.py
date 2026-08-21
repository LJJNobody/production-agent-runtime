import unittest

from agent_runtime.config import CircuitBreakerConfig, RateLimitConfig, RetryConfig
from agent_runtime.errors import CircuitOpenError, PermanentLLMError, TransientLLMError
from agent_runtime.llm.resilience import (
    BreakerState,
    CircuitBreaker,
    ResilientLLMClient,
    TokenBucket,
)
from agent_runtime.models import LLMRequest, LLMResponse, Message

REQUEST = LLMRequest([Message("user", "hello")])


class FlakyProvider:
    def __init__(self, failures):
        self.failures = failures
        self.calls = 0

    async def complete(self, request):
        del request
        self.calls += 1
        if self.calls <= self.failures:
            raise TransientLLMError("temporary")
        return LLMResponse("ok", "flaky")


class PermanentProvider:
    async def complete(self, request):
        del request
        raise PermanentLLMError("bad request")


class ResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_failure_is_retried(self):
        provider = FlakyProvider(2)
        client = ResilientLLMClient(
            provider,
            RetryConfig(max_attempts=3, base_delay_seconds=0, max_delay_seconds=0),
            TokenBucket(RateLimitConfig(capacity=10, refill_per_second=10)),
            CircuitBreaker(CircuitBreakerConfig(failure_threshold=10)),
            random_fn=lambda: 0,
        )
        response = await client.complete(REQUEST)
        self.assertEqual(response.content, "ok")
        self.assertEqual(provider.calls, 3)

    async def test_permanent_failure_is_not_retried(self):
        client = ResilientLLMClient(
            PermanentProvider(),
            RetryConfig(max_attempts=4, base_delay_seconds=0, max_delay_seconds=0),
            TokenBucket(RateLimitConfig(capacity=10, refill_per_second=10)),
            CircuitBreaker(CircuitBreakerConfig()),
        )
        with self.assertRaises(PermanentLLMError):
            await client.complete(REQUEST)

    async def test_circuit_opens_and_recovers_through_half_open(self):
        current = [100.0]
        breaker = CircuitBreaker(
            CircuitBreakerConfig(failure_threshold=2, recovery_timeout_seconds=5),
            clock=lambda: current[0],
        )
        await breaker.record_failure()
        await breaker.record_failure()
        self.assertEqual(breaker.state, BreakerState.OPEN)
        with self.assertRaises(CircuitOpenError):
            await breaker.before_call()
        current[0] += 5
        await breaker.before_call()
        self.assertEqual(breaker.state, BreakerState.HALF_OPEN)
        await breaker.record_success()
        self.assertEqual(breaker.state, BreakerState.CLOSED)


if __name__ == "__main__":
    unittest.main()
