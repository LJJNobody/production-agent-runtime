import asyncio
import unittest

from agent_runtime.events import EventBus


class EventBusTests(unittest.IsolatedAsyncioTestCase):
    async def test_failing_subscriber_is_isolated_and_audited(self):
        bus = EventBus(audit_capacity=10)
        delivered = asyncio.Event()

        async def failing(_):
            raise RuntimeError("subscriber failure")

        async def healthy(_):
            delivered.set()

        bus.subscribe("run.created", failing)
        bus.subscribe("run.created", healthy)
        await bus.start()
        try:
            await bus.emit(
                "run.created",
                trace_id="trace",
                agent_id="agent",
                payload={"value": 1},
            )
            await asyncio.wait_for(delivered.wait(), timeout=1)
            self.assertEqual(len(bus.audit.list("trace")), 1)
            self.assertEqual(bus.metrics.snapshot()["event_handler_errors_total"], 1)
        finally:
            await bus.close()


if __name__ == "__main__":
    unittest.main()
