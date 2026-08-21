#!/usr/bin/env python3
"""Measure logical request success through the configured reliability wrapper."""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agent_runtime.config import (  # noqa: E402
    CircuitBreakerConfig,
    ProviderConfig,
    RateLimitConfig,
    RetryConfig,
)
from agent_runtime.errors import TransientLLMError  # noqa: E402
from agent_runtime.llm.openai_compatible import OpenAICompatibleClient  # noqa: E402
from agent_runtime.llm.resilience import (  # noqa: E402
    CircuitBreaker,
    ResilientLLMClient,
    TokenBucket,
)
from agent_runtime.metrics import MetricsRegistry  # noqa: E402
from agent_runtime.models import LLMRequest, LLMResponse, Message  # noqa: E402


class SimulatedProvider:
    """Independent attempt failures; only for deterministic reliability regression tests."""

    def __init__(self, failure_rate: float, seed: int) -> None:
        self.failure_rate = failure_rate
        self.random = random.Random(seed)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        del request
        if self.random.random() < self.failure_rate:
            raise TransientLLMError("injected transient failure")
        return LLMResponse("ok", "simulated")


async def run_benchmark(
    requests: int,
    concurrency: int,
    attempts: int,
    failure_rate: Optional[float],
    seed: int,
    endpoint: Optional[str],
    model: str,
    api_key_env: str,
):
    metrics = MetricsRegistry()
    if failure_rate is not None:
        provider = SimulatedProvider(failure_rate, seed)
        mode = "simulation"
    else:
        if not endpoint:
            raise ValueError("endpoint is required unless --simulate-failure-rate is used")
        provider = OpenAICompatibleClient(
            ProviderConfig(
                backend="openai_compatible",
                base_url=endpoint,
                model=model,
                api_key_env=api_key_env,
            )
        )
        mode = "real_endpoint"
    client = ResilientLLMClient(
        provider,
        RetryConfig(
            max_attempts=attempts,
            base_delay_seconds=0.01,
            max_delay_seconds=0.1,
            jitter_ratio=0.25,
        ),
        TokenBucket(RateLimitConfig(capacity=max(1, concurrency), refill_per_second=1000)),
        CircuitBreaker(
            CircuitBreakerConfig(
                failure_threshold=max(20, concurrency * attempts * 2),
                recovery_timeout_seconds=1,
            )
        ),
        metrics=metrics,
    )
    semaphore = asyncio.Semaphore(concurrency)
    request = LLMRequest([Message("user", "Reply with OK.")], model=model, max_tokens=8)

    async def execute():
        async with semaphore:
            try:
                await client.complete(request)
                return True
            except Exception:
                return False

    started = time.perf_counter()
    outcomes = await asyncio.gather(*(execute() for _ in range(requests)))
    elapsed = time.perf_counter() - started
    successes = sum(outcomes)
    return {
        "mode": mode,
        "logical_requests": requests,
        "successful": successes,
        "failed": requests - successes,
        "success_rate": successes / requests,
        "concurrency": concurrency,
        "max_attempts": attempts,
        "simulated_attempt_failure_rate": failure_rate,
        "seed": seed if failure_rate is not None else None,
        "elapsed_seconds": elapsed,
        "metrics": metrics.snapshot(),
        "warning": (
            "simulation validates retry behavior; it is not evidence of production API availability"
            if mode == "simulation"
            else None
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--simulate-failure-rate", type=float)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--endpoint")
    parser.add_argument("--model", default="agent-model")
    parser.add_argument("--api-key-env", default="AGENT_LLM_API_KEY")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if min(args.requests, args.concurrency, args.attempts) <= 0:
        raise SystemExit("requests, concurrency, and attempts must be positive")
    if args.simulate_failure_rate is not None and not 0 <= args.simulate_failure_rate <= 1:
        raise SystemExit("simulation failure rate must be in [0, 1]")
    result = asyncio.run(
        run_benchmark(
            args.requests,
            args.concurrency,
            args.attempts,
            args.simulate_failure_rate,
            args.seed,
            args.endpoint,
            args.model,
            args.api_key_env,
        )
    )
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
