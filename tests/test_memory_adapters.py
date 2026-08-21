import asyncio
import unittest

from agent_runtime.adapters import (
    InMemoryCheckpointStore,
    InMemoryEventPublisher,
    InMemoryLeaseManager,
    InMemoryRunRepository,
    InMemorySessionRepository,
    InMemoryTaskQueue,
)
from agent_runtime.models import AgentKind, RunRecord
from agent_runtime.ports import (
    Checkpoint,
    IdempotencyRecord,
    PublishedEvent,
    RunTask,
)


def make_run(run_id="run-1"):
    return RunRecord(
        id=run_id,
        input="test",
        kind=AgentKind.SIMPLE,
        session_id="session-1",
        trace_id="trace-1",
    )


class MemoryAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_repository_keeps_idempotency_index_consistent(self):
        repository = InMemoryRunRepository()
        run = make_run()
        record = IdempotencyRecord("key-1", run.id, "fingerprint-1")

        await repository.add(run, record)
        self.assertIs(await repository.get(run.id), run)
        self.assertEqual(await repository.find_idempotency("key-1"), record)
        with self.assertRaises(ValueError):
            await repository.add(make_run("run-2"), record)

        await repository.delete(run.id)
        self.assertIsNone(await repository.get(run.id))
        self.assertIsNone(await repository.find_idempotency("key-1"))

    async def test_checkpoint_is_monotonic_and_copied(self):
        store = InMemoryCheckpointStore()
        checkpoint = Checkpoint("run-1", 2, {"messages": ["one"]})
        await store.save(checkpoint)

        loaded = await store.load("run-1")
        self.assertIsNotNone(loaded)
        loaded.state["messages"].append("mutated")
        reloaded = await store.load("run-1")
        self.assertEqual(reloaded.state, {"messages": ["one"]})

        with self.assertRaises(ValueError):
            await store.save(Checkpoint("run-1", 1, {}))

    async def test_task_queue_is_bounded_and_fifo(self):
        queue = InMemoryTaskQueue(capacity=1)
        task = RunTask("run-1")
        queue.put_nowait(task)
        self.assertTrue(queue.full())
        with self.assertRaises(asyncio.QueueFull):
            queue.put_nowait(RunTask("run-2"))
        self.assertEqual(await queue.get(), task)
        queue.task_done()

    async def test_expired_lease_can_be_acquired_by_another_owner(self):
        now = [100.0]
        leases = InMemoryLeaseManager(clock=lambda: now[0])

        self.assertTrue(await leases.acquire("run-1", "worker-a", 5))
        self.assertFalse(await leases.acquire("run-1", "worker-b", 5))
        self.assertEqual(await leases.owner("run-1"), "worker-a")
        now[0] = 106.0
        self.assertTrue(await leases.acquire("run-1", "worker-b", 5))
        self.assertFalse(await leases.renew("run-1", "worker-a", 5))
        self.assertTrue(await leases.release("run-1", "worker-b"))

    async def test_event_and_session_adapters_copy_and_bound_data(self):
        publisher = InMemoryEventPublisher()
        event = PublishedEvent("event-1", "run.created", "trace-1", "run-1", {"x": 1})
        await publisher.publish(event)
        events = await publisher.list()
        events[0].payload["x"] = 2
        self.assertEqual((await publisher.list())[0].payload["x"], 1)

        sessions = InMemorySessionRepository(max_messages=2)
        await sessions.append_turn("session-1", "first", "answer-1")
        await sessions.append_turn("session-1", "second", "answer-2")
        history = await sessions.get("session-1")
        self.assertEqual([message.content for message in history], ["second", "answer-2"])
        await sessions.delete("session-1")
        self.assertEqual(await sessions.get("session-1"), [])


if __name__ == "__main__":
    unittest.main()
