import asyncio
import time
import unittest

from agent_runtime.config import RuntimeConfig
from agent_runtime.errors import (
    IdempotencyConflictError,
    PermanentLLMError,
    RunNotFoundError,
    RunQueueFullError,
)
from agent_runtime.llm.mock import MockLLMClient
from agent_runtime.models import AgentKind, LLMResponse, RunState
from agent_runtime.runtime import AgentRuntime


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_four_patterns_complete_and_emit_audit(self):
        runtime = AgentRuntime(RuntimeConfig(), MockLLMClient())
        try:
            runs = [
                await runtime.submit(f"task {kind.value}", kind, "shared-session")
                for kind in AgentKind
            ]
            await asyncio.gather(*(runtime.wait(run.id, timeout=2) for run in runs))
            self.assertTrue(all(run.state == RunState.SUCCEEDED for run in runs))
            self.assertTrue(all(runtime.audit_events(run.trace_id) for run in runs))
        finally:
            await runtime.close()

    async def test_concurrent_runs_overlap(self):
        runtime = AgentRuntime(
            RuntimeConfig(max_concurrency=5, thread_workers=2),
            MockLLMClient(delay_seconds=0.05),
        )
        try:
            started = time.perf_counter()
            runs = [await runtime.submit(f"task {index}") for index in range(5)]
            await asyncio.gather(*(runtime.wait(run.id, timeout=2) for run in runs))
            elapsed = time.perf_counter() - started
            self.assertLess(elapsed, 0.20)
        finally:
            await runtime.close()

    async def test_running_task_can_be_cancelled(self):
        runtime = AgentRuntime(
            RuntimeConfig(max_concurrency=1),
            MockLLMClient(delay_seconds=1),
        )
        try:
            run = await runtime.submit("slow task")
            await asyncio.sleep(0.02)
            self.assertTrue(await runtime.cancel(run.id))
            await runtime.wait(run.id, timeout=1)
            self.assertEqual(run.state, RunState.CANCELLED)
        finally:
            await runtime.close()

    async def test_failed_run_can_be_retried(self):
        class FailsOnce:
            def __init__(self):
                self.calls = 0

            async def complete(self, request):
                del request
                self.calls += 1
                if self.calls == 1:
                    raise PermanentLLMError("first call fails")
                return LLMResponse("recovered", "test")

        runtime = AgentRuntime(RuntimeConfig(), FailsOnce())
        try:
            run = await runtime.submit("retry me")
            await runtime.wait(run.id, timeout=1)
            self.assertEqual(run.state, RunState.FAILED)
            await runtime.retry(run.id)
            await runtime.wait(run.id, timeout=1)
            self.assertEqual(run.state, RunState.SUCCEEDED)
            self.assertEqual(run.output, "recovered")
            transitions = [
                (item["from"], item["to"]) for item in run.transitions
            ]
            self.assertIn(("failed", "ready"), transitions)
        finally:
            await runtime.close()

    async def test_bounded_queue_rejects_without_creating_unbounded_tasks(self):
        class BlockingLLM:
            def __init__(self):
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def complete(self, request):
                del request
                self.started.set()
                await self.release.wait()
                return LLMResponse("done", "test")

        llm = BlockingLLM()
        runtime = AgentRuntime(
            RuntimeConfig(max_concurrency=1, queue_capacity=1),
            llm,
        )
        try:
            first = await runtime.submit("first")
            await asyncio.wait_for(llm.started.wait(), timeout=1)
            second = await runtime.submit("second")
            with self.assertRaises(RunQueueFullError):
                await runtime.submit("third")
            self.assertLessEqual(len(runtime._tasks), 1)
            self.assertEqual(len(runtime.list_runs()), 2)

            llm.release.set()
            await asyncio.gather(
                runtime.wait(first.id, timeout=1),
                runtime.wait(second.id, timeout=1),
            )
        finally:
            llm.release.set()
            await runtime.close()

    async def test_idempotency_replays_and_detects_payload_conflicts(self):
        runtime = AgentRuntime(RuntimeConfig(), MockLLMClient(delay_seconds=0.01))
        try:
            first = await runtime.submit("same", idempotency_key="request-1")
            replay = await runtime.submit("same", idempotency_key="request-1")
            self.assertIs(first, replay)
            with self.assertRaises(IdempotencyConflictError):
                await runtime.submit("different", idempotency_key="request-1")
            await runtime.wait(first.id, timeout=1)
        finally:
            await runtime.close()

    async def test_terminal_runs_are_evicted_at_record_limit(self):
        runtime = AgentRuntime(
            RuntimeConfig(
                max_concurrency=1,
                queue_capacity=1,
                max_run_records=2,
            ),
            MockLLMClient(),
        )
        try:
            first = await runtime.submit("first")
            await runtime.wait(first.id, timeout=1)
            second = await runtime.submit("second")
            await runtime.wait(second.id, timeout=1)
            third = await runtime.submit("third")
            with self.assertRaises(RunNotFoundError):
                runtime.get(first.id)
            await runtime.wait(third.id, timeout=1)
        finally:
            await runtime.close()

    async def test_terminal_runs_are_evicted_after_retention_window(self):
        runtime = AgentRuntime(
            RuntimeConfig(run_retention_seconds=0.001),
            MockLLMClient(),
        )
        try:
            expired = await runtime.submit("expires")
            await runtime.wait(expired.id, timeout=1)
            await asyncio.sleep(0.01)
            current = await runtime.submit("current")
            with self.assertRaises(RunNotFoundError):
                runtime.get(expired.id)
            await runtime.wait(current.id, timeout=1)
        finally:
            await runtime.close()


if __name__ == "__main__":
    unittest.main()
