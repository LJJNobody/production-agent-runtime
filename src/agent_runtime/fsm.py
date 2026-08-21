"""Seven-state, twelve-transition Agent lifecycle state machine."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Set, Tuple

from agent_runtime.errors import InvalidStateTransition
from agent_runtime.models import RunRecord, RunState

LEGAL_TRANSITIONS: Set[Tuple[RunState, RunState]] = {
    (RunState.CREATED, RunState.READY),
    (RunState.CREATED, RunState.CANCELLED),
    (RunState.READY, RunState.RUNNING),
    (RunState.READY, RunState.CANCELLED),
    (RunState.RUNNING, RunState.WAITING),
    (RunState.RUNNING, RunState.SUCCEEDED),
    (RunState.RUNNING, RunState.FAILED),
    (RunState.RUNNING, RunState.CANCELLED),
    (RunState.WAITING, RunState.RUNNING),
    (RunState.WAITING, RunState.FAILED),
    (RunState.WAITING, RunState.CANCELLED),
    (RunState.FAILED, RunState.READY),
}

TransitionHook = Callable[[RunRecord, RunState, RunState, str], Awaitable[None]]


class StateMachine:
    def __init__(self, hook: Optional[TransitionHook] = None) -> None:
        self._hook = hook
        self._locks: Dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    @staticmethod
    def can_transition(source: RunState, target: RunState) -> bool:
        return (source, target) in LEGAL_TRANSITIONS

    async def transition(
        self,
        run: RunRecord,
        target: RunState,
        reason: str = "",
    ) -> None:
        lock = await self._lock_for(run.id)
        async with lock:
            source = run.state
            if not self.can_transition(source, target):
                raise InvalidStateTransition(
                    f"illegal run state transition: {source.value} -> {target.value}"
                )
            await self._apply(run, source, target, reason)

    async def transition_if_current(
        self,
        run: RunRecord,
        expected: RunState,
        target: RunState,
        reason: str = "",
    ) -> bool:
        """Atomically transition only when the run still has the expected state."""

        lock = await self._lock_for(run.id)
        async with lock:
            if run.state != expected:
                return False
            if not self.can_transition(expected, target):
                raise InvalidStateTransition(
                    f"illegal run state transition: {expected.value} -> {target.value}"
                )
            await self._apply(run, expected, target, reason)
            return True

    async def transition_if_allowed(
        self,
        run: RunRecord,
        target: RunState,
        reason: str = "",
    ) -> bool:
        """Atomically transition if the current state permits the target."""

        lock = await self._lock_for(run.id)
        async with lock:
            source = run.state
            if not self.can_transition(source, target):
                return False
            await self._apply(run, source, target, reason)
            return True

    async def _apply(
        self,
        run: RunRecord,
        source: RunState,
        target: RunState,
        reason: str,
    ) -> None:
        now = time.time()
        run.state = target
        run.updated_at = now
        if target == RunState.RUNNING and run.started_at is None:
            run.started_at = now
        if target in {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}:
            run.finished_at = now
        transition: Dict[str, Any] = {
            "from": source.value,
            "to": target.value,
            "at": now,
        }
        if reason:
            transition["reason"] = reason
        run.transitions.append(transition)
        if self._hook:
            await self._hook(run, source, target, reason)

    async def _lock_for(self, run_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(run_id, asyncio.Lock())

    async def discard(self, run_id: str) -> None:
        """Release the in-process lock after a terminal run is evicted."""

        async with self._locks_guard:
            lock = self._locks.get(run_id)
            if lock is not None and not lock.locked():
                self._locks.pop(run_id, None)
