"""FastAPI adapter for the Agent Runtime control plane."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from agent_runtime.config import AppConfig
from agent_runtime.errors import (
    AgentRuntimeError,
    IdempotencyConflictError,
    RunNotFoundError,
    RunQueueFullError,
)
from agent_runtime.factory import create_runtime
from agent_runtime.models import AgentKind

try:
    from fastapi import FastAPI, Header, Request, Response, status
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, Field
except ImportError as exc:
    raise RuntimeError("API dependencies are missing; install the api extra") from exc


class RunRequest(BaseModel):
    input: str = Field(min_length=1, max_length=32768)
    pattern: AgentKind = AgentKind.SIMPLE
    session_id: Optional[str] = Field(default=None, min_length=1, max_length=128)


class RunResponse(BaseModel):
    id: str
    input: str
    kind: str
    session_id: str
    trace_id: str
    state: str
    output: Optional[str]
    error: Optional[str]
    created_at: float
    updated_at: float
    started_at: Optional[float]
    finished_at: Optional[float]
    transitions: List[Dict[str, Any]]
    steps: List[Dict[str, Any]]
    audit: Optional[List[Dict[str, Any]]] = None


class CancelResponse(BaseModel):
    cancelled: bool
    run: RunResponse


class HealthResponse(BaseModel):
    status: str
    ready: bool
    queue_depth: int
    queue_capacity: int
    retained_runs: int


class ErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool
    details: Optional[List[Dict[str, Any]]] = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


def _error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: Optional[List[Dict[str, Any]]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> JSONResponse:
    payload = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=message,
            retryable=retryable,
            details=details,
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(exclude_none=True),
        headers=headers,
    )


def create_app(config_path: str = "config/dev.json") -> FastAPI:
    state: Dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        config = AppConfig.load(config_path)
        runtime = create_runtime(config)
        await runtime.start()
        state["runtime"] = runtime
        try:
            yield
        finally:
            await runtime.close()
            state.clear()

    app = FastAPI(
        title="Production-oriented Agent Runtime",
        description="Bounded, finite-state AI Agent execution control plane.",
        version="0.2.0",
        lifespan=lifespan,
    )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError):
        details = [
            {
                "location": list(item["loc"]),
                "message": item["msg"],
                "type": item["type"],
            }
            for item in exc.errors()
        ]
        return _error_response(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_error",
            "request validation failed",
            details=details,
        )

    @app.exception_handler(RunNotFoundError)
    async def not_found_handler(_: Request, exc: RunNotFoundError):
        return _error_response(
            status.HTTP_404_NOT_FOUND,
            "run_not_found",
            str(exc),
        )

    @app.exception_handler(IdempotencyConflictError)
    async def idempotency_conflict_handler(
        _: Request, exc: IdempotencyConflictError
    ):
        return _error_response(
            status.HTTP_409_CONFLICT,
            "idempotency_conflict",
            str(exc),
        )

    @app.exception_handler(RunQueueFullError)
    async def queue_full_handler(_: Request, exc: RunQueueFullError):
        return _error_response(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "run_queue_full",
            str(exc),
            retryable=True,
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )

    @app.exception_handler(AgentRuntimeError)
    async def runtime_error_handler(_: Request, exc: AgentRuntimeError):
        return _error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "runtime_error",
            str(exc),
            retryable=True,
        )

    @app.get("/healthz", response_model=HealthResponse, tags=["operations"])
    async def healthz():
        runtime = state.get("runtime")
        if runtime is None:
            return HealthResponse(
                status="starting",
                ready=False,
                queue_depth=0,
                queue_capacity=0,
                retained_runs=0,
            )
        snapshot = runtime.metrics.snapshot()
        return HealthResponse(
            status="ok",
            ready=True,
            queue_depth=int(snapshot.get("run_queue_depth", 0)),
            queue_capacity=runtime.queue_capacity,
            retained_runs=len(runtime.list_runs()),
        )

    @app.get("/metrics", tags=["operations"])
    async def metrics():
        runtime = state.get("runtime")
        text = runtime.metrics.render_prometheus() if runtime else "agent_runtime_ready 0\n"
        return Response(text, media_type="text/plain; version=0.0.4")

    @app.post(
        "/v1/runs",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=RunResponse,
        responses={
            409: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
        },
        tags=["runs"],
    )
    async def submit_run(
        request: RunRequest,
        response: Response,
        idempotency_key: Optional[str] = Header(
            default=None,
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
        ),
    ):
        runtime = state["runtime"]
        run = await runtime.submit(
            request.input,
            request.pattern,
            request.session_id,
            idempotency_key,
        )
        if idempotency_key is not None:
            response.headers["Idempotency-Key"] = idempotency_key
        return run.to_dict()

    @app.get(
        "/v1/runs/{run_id}",
        response_model=RunResponse,
        responses={404: {"model": ErrorResponse}},
        tags=["runs"],
    )
    async def get_run(run_id: str, include_audit: bool = False):
        runtime = state["runtime"]
        run = runtime.get(run_id)
        payload = run.to_dict()
        if include_audit:
            payload["audit"] = runtime.audit_events(run.trace_id)
        return payload

    @app.post(
        "/v1/runs/{run_id}/cancel",
        response_model=CancelResponse,
        responses={404: {"model": ErrorResponse}},
        tags=["runs"],
    )
    async def cancel_run(run_id: str):
        runtime = state["runtime"]
        cancelled = await runtime.cancel(run_id)
        run = runtime.get(run_id)
        return {"cancelled": cancelled, "run": run.to_dict()}

    return app
