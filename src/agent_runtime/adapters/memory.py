"""In-memory adapters for tests and single-process development."""

from __future__ import annotations

import asyncio
import copy
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from agent_runtime.models import RunRecord
from agent_runtime.ports import (
    Checkpoint,
    CheckpointStore,
    EventPublisher,
    IdempotencyRecord,
    LeaseManager,
    PublishedEvent,
    RunRepository,
    RunTask,
    SessionRepository,
    TaskQueue,
)
from agent_runtime.sessions import SessionStore


class InMemoryRunRepository(RunRepository):
    def __init__(self) -> None:
        self._runs: Dict[str, RunRecord] = {}
        self._idempotency: Dict[str, IdempotencyRecord] = {}
        self._idempotency_by_run: Dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def add(
        self,
        run: RunRecord,
        idempotency: Optional[IdempotencyRecord] = None,
    ) -> None:
        async with self._lock:
            if run.id in self._runs:
                raise ValueError(f"duplicate run id: {run.id}")
            if idempotency is not None:
                if idempotency.run_id != run.id:
                    raise ValueError("idempotency record must reference the added run")
                if idempotency.key in self._idempotency:
                    raise ValueError(f"duplicate idempotency key: {idempotency.key}")
            self._runs[run.id] = run
            if idempotency is not None:
                self._idempotency[idempotency.key] = idempotency
                self._idempotency_by_run[run.id] = idempotency.key

    async def get(self, run_id: str) -> Optional[RunRecord]:
        async with self._lock:
            return self._runs.get(run_id)

    async def list(self) -> List[RunRecord]:
        async with self._lock:
            return sorted(
                self._runs.values(),
                key=lambda run: run.created_at,
                reverse=True,
            )

    async def find_idempotency(self, key: str) -> Optional[IdempotencyRecord]:
        async with self._lock:
            return self._idempotency.get(key)

    async def delete(self, run_id: str) -> None:
        async with self._lock:
            self._runs.pop(run_id, None)
            key = self._idempotency_by_run.pop(run_id, None)
            if key is not None:
                self._idempotency.pop(key, None)


class InMemoryCheckpointStore(CheckpointStore):
    def __init__(self) -> None:
        self._checkpoints: Dict[str, Checkpoint] = {}
        self._lock = asyncio.Lock()

    async def save(self, checkpoint: Checkpoint) -> None:
        async with self._lock:
            previous = self._checkpoints.get(checkpoint.run_id)
            if previous is not None and checkpoint.step < previous.step:
                raise ValueError("checkpoint step cannot move backwards")
            self._checkpoints[checkpoint.run_id] = copy.deepcopy(checkpoint)

    async def load(self, run_id: str) -> Optional[Checkpoint]:
        async with self._lock:
            checkpoint = self._checkpoints.get(run_id)
            return copy.deepcopy(checkpoint)

    async def delete(self, run_id: str) -> None:
        async with self._lock:
            self._checkpoints.pop(run_id, None)


class InMemoryTaskQueue(TaskQueue):
    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("queue capacity must be positive")
        self._capacity = capacity
        self._queue: "asyncio.Queue[RunTask]" = asyncio.Queue(maxsize=capacity)

    @property
    def capacity(self) -> int:
        return self._capacity

    def full(self) -> bool:
        return self._queue.full()

    def qsize(self) -> int:
        return self._queue.qsize()

    def put_nowait(self, task: RunTask) -> None:
        self._queue.put_nowait(task)

    async def get(self) -> RunTask:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()


@dataclass(frozen=True)
class _Lease:
    owner_id: str
    expires_at: float


class InMemoryLeaseManager(LeaseManager):
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._leases: Dict[str, _Lease] = {}
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        resource_id: str,
        owner_id: str,
        ttl_seconds: float,
    ) -> bool:
        self._validate(resource_id, owner_id, ttl_seconds)
        async with self._lock:
            now = self._clock()
            current = self._leases.get(resource_id)
            if current is not None and current.expires_at > now:
                return current.owner_id == owner_id
            self._leases[resource_id] = _Lease(owner_id, now + ttl_seconds)
            return True

    async def renew(
        self,
        resource_id: str,
        owner_id: str,
        ttl_seconds: float,
    ) -> bool:
        self._validate(resource_id, owner_id, ttl_seconds)
        async with self._lock:
            now = self._clock()
            current = self._leases.get(resource_id)
            if (
                current is None
                or current.owner_id != owner_id
                or current.expires_at <= now
            ):
                return False
            self._leases[resource_id] = _Lease(owner_id, now + ttl_seconds)
            return True

    async def release(self, resource_id: str, owner_id: str) -> bool:
        async with self._lock:
            current = self._leases.get(resource_id)
            if current is None or current.owner_id != owner_id:
                return False
            self._leases.pop(resource_id, None)
            return True

    async def owner(self, resource_id: str) -> Optional[str]:
        async with self._lock:
            current = self._leases.get(resource_id)
            if current is None:
                return None
            if current.expires_at <= self._clock():
                self._leases.pop(resource_id, None)
                return None
            return current.owner_id

    @staticmethod
    def _validate(resource_id: str, owner_id: str, ttl_seconds: float) -> None:
        if not resource_id or not owner_id or ttl_seconds <= 0:
            raise ValueError("lease requires resource, owner, and a positive TTL")


class InMemoryEventPublisher(EventPublisher):
    def __init__(self) -> None:
        self._events: List[PublishedEvent] = []
        self._lock = asyncio.Lock()

    async def publish(self, event: PublishedEvent) -> None:
        async with self._lock:
            self._events.append(copy.deepcopy(event))

    async def list(self) -> List[PublishedEvent]:
        async with self._lock:
            return copy.deepcopy(self._events)


class InMemorySessionRepository(SessionStore, SessionRepository):
    """SessionStore exposed under the durable port's adapter vocabulary."""

    async def delete(self, session_id: str) -> None:
        await self.clear(session_id)
