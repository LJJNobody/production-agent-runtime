"""Concurrent lifecycle-managed Agent execution engine."""

from __future__ import annotations

import asyncio
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

from agent_runtime.agents import AgentContext, create_patterns
from agent_runtime.config import RuntimeConfig
from agent_runtime.errors import (
    IdempotencyConflictError,
    RunNotFoundError,
    RunQueueFullError,
)
from agent_runtime.events import EventBus
from agent_runtime.fsm import StateMachine
from agent_runtime.llm.base import LLMClient
from agent_runtime.metrics import MetricsRegistry
from agent_runtime.models import (
    AgentKind,
    LLMRequest,
    LLMResponse,
    RunRecord,
    RunState,
)
from agent_runtime.sessions import SessionStore
from agent_runtime.tools import ToolRegistry, register_builtin_tools

_TERMINAL_STATES = {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}


class AgentRuntime:
    def __init__(
        self,
        config: RuntimeConfig,
        llm: LLMClient,
        *,
        metrics: Optional[MetricsRegistry] = None,
        executor: Optional[ThreadPoolExecutor] = None,
        event_bus: Optional[EventBus] = None,
        tools: Optional[ToolRegistry] = None,
        own_executor: bool = True,
    ) -> None:
        config.validate()
        self.config = config
        self.llm = llm
        self.metrics = metrics or MetricsRegistry()
        self.executor = executor or ThreadPoolExecutor(
            max_workers=config.thread_workers,
            thread_name_prefix="agent-runtime",
        )
        self._own_executor = own_executor
        self.events = event_bus or EventBus(
            config.audit_capacity,
            metrics=self.metrics,
            executor=self.executor,
        )
        self.tools = tools or ToolRegistry(
            config.tool_timeout_seconds,
            executor=self.executor,
            metrics=self.metrics,
        )
        if tools is None:
            register_builtin_tools(self.tools)
        self.sessions = SessionStore(config.history_messages)
        self.fsm = StateMachine(self._on_transition)
        self.patterns = create_patterns()
        self._runs: Dict[str, RunRecord] = {}
        self._completion_events: Dict[str, asyncio.Event] = {}
        self._idempotency_index: Dict[
            str, Tuple[str, str, AgentKind, Optional[str]]
        ] = {}
        self._idempotency_by_run: Dict[str, str] = {}
        self._queue: "asyncio.Queue[Optional[str]]" = asyncio.Queue(
            maxsize=config.queue_capacity
        )
        self._workers: List["asyncio.Task[None]"] = []
        self._tasks: Dict[str, "asyncio.Task[None]"] = {}
        self._submission_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._started = False
        self._closed = False

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("runtime has been closed")
            if self._started:
                return
            await self.events.start()
            self._workers = [
                asyncio.create_task(
                    self._worker_loop(index),
                    name=f"agent-worker-{index}",
                )
                for index in range(self.config.max_concurrency)
            ]
            self.metrics.set("runtime_concurrency_limit", self.config.max_concurrency)
            self.metrics.set("run_queue_capacity", self.config.queue_capacity)
            self._started = True

    async def submit(
        self,
        input_text: str,
        kind: AgentKind = AgentKind.SIMPLE,
        session_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> RunRecord:
        normalized_input = input_text.strip()
        if not normalized_input:
            raise ValueError("run input must not be empty")
        await self.start()
        if not isinstance(kind, AgentKind):
            kind = AgentKind(kind)
        if idempotency_key is not None:
            if not idempotency_key or len(idempotency_key) > 128:
                raise ValueError("idempotency key must contain 1 to 128 characters")

        async with self._submission_lock:
            await self._cleanup_runs(reserve=1)
            if idempotency_key is not None:
                existing = self._idempotency_index.get(idempotency_key)
                if existing is not None:
                    run_id, previous_input, previous_kind, previous_session_id = existing
                    run = self._runs.get(run_id)
                    if run is not None:
                        if (
                            normalized_input != previous_input
                            or kind != previous_kind
                            or session_id != previous_session_id
                        ):
                            raise IdempotencyConflictError(
                                "idempotency key was already used with a different request"
                            )
                        self.metrics.increment("idempotency_replays_total")
                        return run
                    self._idempotency_index.pop(idempotency_key, None)

            if self._queue.full():
                self.metrics.increment("run_submissions_rejected_total")
                raise RunQueueFullError(self.config.queue_retry_after_seconds)

            run_id = uuid.uuid4().hex
            run = RunRecord(
                id=run_id,
                input=normalized_input,
                kind=kind,
                session_id=session_id or uuid.uuid4().hex,
                trace_id=uuid.uuid4().hex,
            )
            self._runs[run_id] = run
            self._completion_events[run_id] = asyncio.Event()
            if idempotency_key is not None:
                self._idempotency_index[idempotency_key] = (
                    run_id,
                    normalized_input,
                    kind,
                    session_id,
                )
                self._idempotency_by_run[run_id] = idempotency_key
            self.metrics.increment("runs_submitted_total")
            await self.events.emit(
                "run.created",
                trace_id=run.trace_id,
                agent_id=run.id,
                payload={"kind": kind.value, "session_id": run.session_id},
            )
            await self.fsm.transition(run, RunState.READY, "accepted")
            self._queue.put_nowait(run.id)
            self._update_registry_metrics()
            return run

    def get(self, run_id: str) -> RunRecord:
        try:
            return self._runs[run_id]
        except KeyError as exc:
            raise RunNotFoundError(f"unknown run: {run_id}") from exc

    def list_runs(self) -> List[RunRecord]:
        return sorted(self._runs.values(), key=lambda run: run.created_at, reverse=True)

    async def wait(self, run_id: str, timeout: Optional[float] = None) -> RunRecord:
        run = self.get(run_id)
        if run.state in _TERMINAL_STATES:
            return run
        event = self._completion_events[run_id]
        await asyncio.wait_for(event.wait(), timeout=timeout)
        return run

    async def cancel(self, run_id: str) -> bool:
        run = self.get(run_id)
        if run.state in _TERMINAL_STATES:
            return False
        run.cancel_requested = True
        cancelled = await self.fsm.transition_if_allowed(
            run, RunState.CANCELLED, "cancel requested"
        )
        if not cancelled:
            return False
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
        self._completion_events[run_id].set()
        self.metrics.increment("runs_cancelled_total")
        return True

    async def retry(self, run_id: str) -> RunRecord:
        async with self._submission_lock:
            run = self.get(run_id)
            if self._queue.full():
                self.metrics.increment("run_submissions_rejected_total")
                raise RunQueueFullError(self.config.queue_retry_after_seconds)
            await self.fsm.transition(run, RunState.READY, "manual retry")
            run.error = None
            run.output = None
            run.finished_at = None
            run.cancel_requested = False
            self._completion_events[run_id].clear()
            self._queue.put_nowait(run.id)
            self._update_registry_metrics()
            self.metrics.increment("runs_retried_total")
            return run

    def audit_events(self, trace_id: Optional[str] = None):
        return self.events.audit.list(trace_id)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        async with self._submission_lock:
            for run in list(self._runs.values()):
                if run.state in _TERMINAL_STATES:
                    continue
                await self.fsm.transition_if_allowed(
                    run, RunState.CANCELLED, "runtime shutdown"
                )
                self._completion_events[run.id].set()
            active = [task for task in self._tasks.values() if not task.done()]
            for task in active:
                task.cancel()
            workers = [task for task in self._workers if not task.done()]
            for task in workers:
                task.cancel()
        if active or workers:
            await asyncio.gather(*active, *workers, return_exceptions=True)
        await self.events.close()
        self.tools.close()
        if self._own_executor:
            self.executor.shutdown(wait=True, cancel_futures=True)
        self._closed = True
        self._started = False

    async def _worker_loop(self, index: int) -> None:
        del index
        while True:
            run_id = await self._queue.get()
            try:
                if run_id is None:
                    return
                run = self._runs.get(run_id)
                if run is None or run.state in _TERMINAL_STATES:
                    continue
                task = asyncio.create_task(
                    self._execute(run),
                    name=f"agent-run-{run.id}",
                )
                self._tasks[run.id] = task
                try:
                    await asyncio.shield(task)
                finally:
                    self._tasks.pop(run.id, None)
            finally:
                self._queue.task_done()
                self._update_registry_metrics()

    async def _execute(self, run: RunRecord) -> None:
        self.metrics.increment("runs_active")
        self.metrics.set("runtime_concurrency_limit", self.config.max_concurrency)
        try:
            if run.state == RunState.CANCELLED:
                return
            await self.fsm.transition(run, RunState.RUNNING, "worker acquired")
            history = await self.sessions.get(run.session_id)
            context = AgentContext(
                task=run.input,
                history=history,
                max_steps=self.config.max_agent_steps,
                tool_descriptions=self.tools.describe(),
                complete=lambda request, step: self._complete(run, request, step),
                execute_tool=lambda name, arguments, step: self._execute_tool(
                    run, name, arguments, step
                ),
            )
            pattern = self.patterns[run.kind]
            started = time.perf_counter()
            output = await asyncio.wait_for(
                pattern.run(context),
                timeout=self.config.run_timeout_seconds,
            )
            run.output = output
            await self.sessions.append_turn(run.session_id, run.input, output)
            self.metrics.increment("run_latency_seconds_sum", time.perf_counter() - started)
            self.metrics.increment("run_latency_seconds_count")
            succeeded = await self.fsm.transition_if_current(
                run, RunState.RUNNING, RunState.SUCCEEDED, "agent completed"
            )
            if succeeded:
                self.metrics.increment("runs_succeeded_total")
        except asyncio.CancelledError:
            await self.fsm.transition_if_allowed(
                run, RunState.CANCELLED, "task cancelled"
            )
        except Exception as exc:
            run.error = f"{type(exc).__name__}: {exc}"
            failed = await self.fsm.transition_if_allowed(
                run, RunState.FAILED, "execution error"
            )
            if failed:
                self.metrics.increment("runs_failed_total")
            await self.events.emit(
                "run.error",
                trace_id=run.trace_id,
                agent_id=run.id,
                payload={"error": run.error},
            )
        finally:
            self.metrics.increment("runs_active", -1.0)
            if run.state in _TERMINAL_STATES:
                self._completion_events[run.id].set()
            self._update_registry_metrics()

    async def _cleanup_runs(self, reserve: int = 0) -> None:
        now = time.time()
        cutoff = now - self.config.run_retention_seconds
        terminal = sorted(
            (run for run in self._runs.values() if run.state in _TERMINAL_STATES),
            key=lambda run: run.finished_at or run.updated_at,
        )
        remove_ids = {
            run.id
            for run in terminal
            if (run.finished_at or run.updated_at) < cutoff
        }
        retained_after_ttl = len(self._runs) - len(remove_ids)
        overflow = max(
            0,
            retained_after_ttl + reserve - self.config.max_run_records,
        )
        if overflow:
            for run in terminal:
                if run.id not in remove_ids:
                    remove_ids.add(run.id)
                    overflow -= 1
                    if overflow == 0:
                        break
        for run_id in remove_ids:
            self._runs.pop(run_id, None)
            self._completion_events.pop(run_id, None)
            key = self._idempotency_by_run.pop(run_id, None)
            if key is not None:
                self._idempotency_index.pop(key, None)
            await self.fsm.discard(run_id)
            self.metrics.increment("runs_evicted_total")
        self._update_registry_metrics()

    def _update_registry_metrics(self) -> None:
        self.metrics.set("run_queue_depth", self._queue.qsize())
        self.metrics.set("runs_retained", len(self._runs))

    async def _complete(
        self,
        run: RunRecord,
        request: LLMRequest,
        step_name: str,
    ) -> LLMResponse:
        await self.fsm.transition(run, RunState.WAITING, f"LLM: {step_name}")
        await self.events.emit(
            "llm.requested",
            trace_id=run.trace_id,
            agent_id=run.id,
            payload={"step": step_name, "messages": len(request.messages)},
        )
        started = time.perf_counter()
        try:
            response = await self.llm.complete(request)
            run.steps.append(
                {
                    "type": "llm",
                    "name": step_name,
                    "model": response.model,
                    "latency_seconds": time.perf_counter() - started,
                }
            )
            return response
        finally:
            await self.fsm.transition_if_current(
                run,
                RunState.WAITING,
                RunState.RUNNING,
                f"LLM returned: {step_name}",
            )

    async def _execute_tool(
        self,
        run: RunRecord,
        name: str,
        arguments: Dict[str, object],
        step_name: str,
    ) -> str:
        await self.fsm.transition(run, RunState.WAITING, f"tool: {name}")
        await self.events.emit(
            "tool.requested",
            trace_id=run.trace_id,
            agent_id=run.id,
            payload={"step": step_name, "tool": name},
        )
        started = time.perf_counter()
        try:
            result = await self.tools.execute(name, arguments)
            run.steps.append(
                {
                    "type": "tool",
                    "name": name,
                    "latency_seconds": time.perf_counter() - started,
                }
            )
            return result
        finally:
            await self.fsm.transition_if_current(
                run,
                RunState.WAITING,
                RunState.RUNNING,
                f"tool returned: {name}",
            )

    async def _on_transition(
        self,
        run: RunRecord,
        source: RunState,
        target: RunState,
        reason: str,
    ) -> None:
        self.metrics.increment(f"state_transitions_{source.value}_to_{target.value}_total")
        await self.events.emit(
            "run.state_changed",
            trace_id=run.trace_id,
            agent_id=run.id,
            payload={"from": source.value, "to": target.value, "reason": reason},
        )
