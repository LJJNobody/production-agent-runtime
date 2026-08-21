#!/usr/bin/env python3
"""Measure sequential versus concurrent Agent execution without fixed claims."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Dict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agent_runtime.config import RuntimeConfig  # noqa: E402
from agent_runtime.llm.mock import MockLLMClient  # noqa: E402
from agent_runtime.runtime import AgentRuntime  # noqa: E402


async def measure(task_count: int, concurrency: int, delay_seconds: float) -> Dict[str, float]:
    runtime = AgentRuntime(
        RuntimeConfig(
            max_concurrency=concurrency,
            thread_workers=max(1, concurrency),
            run_timeout_seconds=max(10.0, delay_seconds * task_count * 2),
        ),
        MockLLMClient(delay_seconds=delay_seconds),
    )
    started = time.perf_counter()
    try:
        runs = [await runtime.submit(f"benchmark task {index}") for index in range(task_count)]
        await asyncio.gather(*(runtime.wait(run.id) for run in runs))
        elapsed = time.perf_counter() - started
        successful = sum(run.state.value == "succeeded" for run in runs)
        return {
            "elapsed_seconds": elapsed,
            "successful": successful,
            "tasks_per_second": successful / elapsed if elapsed else 0.0,
        }
    finally:
        await runtime.close()


async def benchmark(task_count: int, concurrency: int, delay_seconds: float):
    sequential = await measure(task_count, 1, delay_seconds)
    concurrent = await measure(task_count, concurrency, delay_seconds)
    return {
        "definition": "sequential wall time / concurrent wall time",
        "task_count": task_count,
        "requested_concurrency": concurrency,
        "simulated_llm_delay_seconds": delay_seconds,
        "sequential": sequential,
        "concurrent": concurrent,
        "speedup": (
            sequential["elapsed_seconds"] / concurrent["elapsed_seconds"]
            if concurrent["elapsed_seconds"]
            else None
        ),
        "theoretical_upper_bound": min(task_count, concurrency),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.tasks <= 0 or args.concurrency <= 0 or args.delay < 0:
        raise SystemExit("tasks/concurrency must be positive and delay non-negative")
    result = asyncio.run(benchmark(args.tasks, args.concurrency, args.delay))
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
