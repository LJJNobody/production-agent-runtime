"""Asynchronous publish-subscribe bus with isolated subscriber bulkheads."""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections import deque
from concurrent.futures import Executor
from dataclasses import asdict, dataclass
from functools import partial
from typing import Any, Callable, Deque, Dict, List, Optional

from agent_runtime.metrics import MetricsRegistry


@dataclass(frozen=True)
class Event:
    id: str
    topic: str
    trace_id: str
    agent_id: str
    timestamp: float
    payload: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class _Subscription:
    topic: str
    handler: Callable[[Event], Any]
    queue: "asyncio.Queue[Optional[Event]]"
    workers: int
    tasks: List["asyncio.Task[None]"]


class AuditTrail:
    def __init__(self, capacity: int = 10000) -> None:
        if capacity <= 0:
            raise ValueError("audit capacity must be positive")
        self._events: Deque[Event] = deque(maxlen=capacity)

    def record(self, event: Event) -> None:
        self._events.append(event)

    def list(self, trace_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return [
            event.to_dict()
            for event in self._events
            if trace_id is None or event.trace_id == trace_id
        ]


class EventBus:
    def __init__(
        self,
        audit_capacity: int = 10000,
        metrics: Optional[MetricsRegistry] = None,
        executor: Optional[Executor] = None,
    ) -> None:
        self.audit = AuditTrail(audit_capacity)
        self.metrics = metrics or MetricsRegistry()
        self.executor = executor
        self._subscriptions: List[_Subscription] = []
        self._started = False

    def subscribe(
        self,
        topic: str,
        handler: Callable[[Event], Any],
        *,
        max_concurrency: int = 1,
        queue_size: int = 100,
    ) -> None:
        if self._started:
            raise RuntimeError("subscriptions must be registered before EventBus.start")
        if max_concurrency <= 0 or queue_size <= 0:
            raise ValueError("subscription limits must be positive")
        self._subscriptions.append(
            _Subscription(
                topic=topic,
                handler=handler,
                queue=asyncio.Queue(maxsize=queue_size),
                workers=max_concurrency,
                tasks=[],
            )
        )

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        for subscription in self._subscriptions:
            subscription.tasks = [
                asyncio.create_task(
                    self._worker(subscription),
                    name=f"event-{subscription.topic}-{index}",
                )
                for index in range(subscription.workers)
            ]

    async def emit(
        self,
        topic: str,
        *,
        trace_id: str,
        agent_id: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Event:
        event = Event(
            id=uuid.uuid4().hex,
            topic=topic,
            trace_id=trace_id,
            agent_id=agent_id,
            timestamp=time.time(),
            payload=dict(payload or {}),
        )
        self.audit.record(event)
        self.metrics.increment("events_published_total")
        for subscription in self._subscriptions:
            if subscription.topic not in {topic, "*"}:
                continue
            try:
                subscription.queue.put_nowait(event)
            except asyncio.QueueFull:
                self.metrics.increment("event_deliveries_dropped_total")
        return event

    async def close(self) -> None:
        if not self._started:
            return
        for subscription in self._subscriptions:
            for _ in subscription.tasks:
                await subscription.queue.put(None)
        tasks = [task for subscription in self._subscriptions for task in subscription.tasks]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._started = False

    async def _worker(self, subscription: _Subscription) -> None:
        while True:
            event = await subscription.queue.get()
            try:
                if event is None:
                    return
                if inspect.iscoroutinefunction(subscription.handler):
                    await subscription.handler(event)
                else:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(
                        self.executor,
                        partial(subscription.handler, event),
                    )
                self.metrics.increment("event_deliveries_total")
            except Exception:
                self.metrics.increment("event_handler_errors_total")
            finally:
                subscription.queue.task_done()
