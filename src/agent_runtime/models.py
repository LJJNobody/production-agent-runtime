"""Domain objects shared by the runtime, agents, and API."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional


class RunState(str, Enum):
    CREATED = "created"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentKind(str, Enum):
    SIMPLE = "simple"
    REACT = "react"
    REFLECTION = "reflection"
    PLAN_SOLVE = "plan_solve"


@dataclass(frozen=True)
class Message:
    role: str
    content: str
    name: Optional[str] = None

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant", "tool"}:
            raise ValueError(f"unsupported message role: {self.role}")
        if not self.content.strip():
            raise ValueError("message content must not be empty")

    def to_dict(self) -> Dict[str, str]:
        value = {"role": self.role, "content": self.content}
        if self.name:
            value["name"] = self.name
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Message":
        return cls(
            role=str(value["role"]),
            content=str(value["content"]),
            name=str(value["name"]) if value.get("name") else None,
        )


@dataclass(frozen=True)
class LLMRequest:
    messages: List[Message]
    model: Optional[str] = None
    temperature: float = 0.0
    max_tokens: int = 1024

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("LLM request requires at least one message")
        if self.temperature < 0 or self.max_tokens <= 0:
            raise ValueError("invalid generation parameters")


@dataclass(frozen=True)
class LLMResponse:
    content: str
    model: str
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    finish_reason: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RunRecord:
    id: str
    input: str
    kind: AgentKind
    session_id: str
    trace_id: str
    state: RunState = RunState.CREATED
    output: Optional[str] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    transitions: List[Dict[str, Any]] = field(default_factory=list)
    steps: List[Dict[str, Any]] = field(default_factory=list)
    cancel_requested: bool = False

    def __post_init__(self) -> None:
        if not self.id or not self.input.strip() or not self.session_id:
            raise ValueError("run id, input, and session id are required")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "input": self.input,
            "kind": self.kind.value,
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "state": self.state.value,
            "output": self.output,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "transitions": list(self.transitions),
            "steps": list(self.steps),
        }
