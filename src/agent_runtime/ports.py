"""Infrastructure ports used by durable runtime adapters."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

from agent_runtime.models import Message, RunRecord


@dataclass(frozen=True)
class IdempotencyRecord:
    key: str
    run_id: str
    request_fingerprint: str


@dataclass(frozen=True)
class RunTask:
    run_id: str
    attempt: int = 1
    enqueued_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.run_id or self.attempt <= 0:
            raise ValueError("run task requires a run id and a positive attempt")


@dataclass(frozen=True)
class Checkpoint:
    run_id: str
    step: int
    state: Dict[str, Any]
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.run_id or self.step < 0:
            raise ValueError("checkpoint requires a run id and a non-negative step")


@dataclass(frozen=True)
class PublishedEvent:
    id: str
    topic: str
    trace_id: str
    agent_id: str
    payload: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)


class RunRepository(Protocol):
    async def add(
        self,
        run: RunRecord,
        idempotency: Optional[IdempotencyRecord] = None,
    ) -> None:
        ...

    async def get(self, run_id: str) -> Optional[RunRecord]:
        ...

    async def list(self) -> List[RunRecord]:
        ...

    async def find_idempotency(self, key: str) -> Optional[IdempotencyRecord]:
        ...

    async def delete(self, run_id: str) -> None:
        ...


class CheckpointStore(Protocol):
    async def save(self, checkpoint: Checkpoint) -> None:
        ...

    async def load(self, run_id: str) -> Optional[Checkpoint]:
        ...

    async def delete(self, run_id: str) -> None:
        ...


class TaskQueue(Protocol):
    @property
    def capacity(self) -> int:
        ...

    def full(self) -> bool:
        ...

    def qsize(self) -> int:
        ...

    def put_nowait(self, task: RunTask) -> None:
        ...

    async def get(self) -> RunTask:
        ...

    def task_done(self) -> None:
        ...


class LeaseManager(Protocol):
    async def acquire(
        self,
        resource_id: str,
        owner_id: str,
        ttl_seconds: float,
    ) -> bool:
        ...

    async def renew(
        self,
        resource_id: str,
        owner_id: str,
        ttl_seconds: float,
    ) -> bool:
        ...

    async def release(self, resource_id: str, owner_id: str) -> bool:
        ...

    async def owner(self, resource_id: str) -> Optional[str]:
        ...


class EventPublisher(Protocol):
    async def publish(self, event: PublishedEvent) -> None:
        ...


class SessionRepository(Protocol):
    async def get(self, session_id: str) -> List[Message]:
        ...

    async def append_turn(self, session_id: str, user: str, assistant: str) -> None:
        ...

    async def delete(self, session_id: str) -> None:
        ...
