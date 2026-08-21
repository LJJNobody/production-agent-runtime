"""Strict JSON configuration and environment expansion."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Type, TypeVar

from agent_runtime.errors import ConfigurationError

T = TypeVar("T")
_ENV_PATTERN = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int = 4
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 8.0
    jitter_ratio: float = 0.25

    def validate(self) -> None:
        if self.max_attempts <= 0:
            raise ConfigurationError("retry.max_attempts must be positive")
        if not 0 <= self.base_delay_seconds <= self.max_delay_seconds:
            raise ConfigurationError("retry delays must satisfy 0 <= base <= max")
        if not 0 <= self.jitter_ratio <= 1:
            raise ConfigurationError("retry.jitter_ratio must be in [0, 1]")


@dataclass(frozen=True)
class RateLimitConfig:
    capacity: float = 20.0
    refill_per_second: float = 10.0

    def validate(self) -> None:
        if self.capacity <= 0 or self.refill_per_second <= 0:
            raise ConfigurationError("rate limit capacity and refill rate must be positive")


@dataclass(frozen=True)
class CircuitBreakerConfig:
    failure_threshold: int = 5
    recovery_timeout_seconds: float = 30.0
    half_open_max_calls: int = 1

    def validate(self) -> None:
        if min(self.failure_threshold, self.half_open_max_calls) <= 0:
            raise ConfigurationError("circuit breaker counts must be positive")
        if self.recovery_timeout_seconds < 0:
            raise ConfigurationError("circuit breaker recovery timeout must be non-negative")


@dataclass(frozen=True)
class ProviderConfig:
    backend: str = "mock"
    base_url: str = "http://127.0.0.1:8001/v1"
    model: str = "mock-agent-model"
    api_key_env: str = "AGENT_LLM_API_KEY"
    timeout_seconds: float = 60.0
    temperature: float = 0.0
    max_tokens: int = 1024
    mock_delay_seconds: float = 0.0

    def validate(self) -> None:
        if self.backend not in {"mock", "openai_compatible"}:
            raise ConfigurationError("provider.backend must be mock or openai_compatible")
        if self.backend == "openai_compatible" and not self.base_url.startswith(("http://", "https://")):
            raise ConfigurationError("provider.base_url must be an HTTP(S) URL")
        if not self.model.strip() or self.timeout_seconds <= 0 or self.max_tokens <= 0:
            raise ConfigurationError("provider model, timeout, and max_tokens are invalid")
        if self.temperature < 0 or self.mock_delay_seconds < 0:
            raise ConfigurationError("provider temperatures and delays must be non-negative")


@dataclass(frozen=True)
class RuntimeConfig:
    max_concurrency: int = 10
    queue_capacity: int = 100
    queue_retry_after_seconds: int = 1
    thread_workers: int = 10
    max_agent_steps: int = 8
    tool_timeout_seconds: float = 30.0
    run_timeout_seconds: float = 300.0
    run_retention_seconds: float = 3600.0
    max_run_records: int = 10000
    history_messages: int = 20
    audit_capacity: int = 10000

    def validate(self) -> None:
        values = (
            self.max_concurrency,
            self.queue_capacity,
            self.queue_retry_after_seconds,
            self.thread_workers,
            self.max_agent_steps,
            self.max_run_records,
            self.history_messages,
            self.audit_capacity,
        )
        if (
            min(values) <= 0
            or self.tool_timeout_seconds <= 0
            or self.run_timeout_seconds <= 0
            or self.run_retention_seconds <= 0
        ):
            raise ConfigurationError("runtime limits and timeouts must be positive")
        minimum_records = self.max_concurrency + self.queue_capacity
        if self.max_run_records < minimum_records:
            raise ConfigurationError(
                "runtime.max_run_records must cover active and queued runs"
            )


@dataclass(frozen=True)
class AppConfig:
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)

    def validate(self) -> None:
        self.provider.validate()
        self.retry.validate()
        self.rate_limit.validate()
        self.circuit_breaker.validate()
        self.runtime.validate()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AppConfig":
        allowed = {"provider", "retry", "rate_limit", "circuit_breaker", "runtime"}
        unknown = set(raw) - allowed
        if unknown:
            raise ConfigurationError(f"unknown top-level keys: {sorted(unknown)}")
        config = cls(
            provider=_from_mapping(ProviderConfig, raw.get("provider", {})),
            retry=_from_mapping(RetryConfig, raw.get("retry", {})),
            rate_limit=_from_mapping(RateLimitConfig, raw.get("rate_limit", {})),
            circuit_breaker=_from_mapping(
                CircuitBreakerConfig, raw.get("circuit_breaker", {})
            ),
            runtime=_from_mapping(RuntimeConfig, raw.get("runtime", {})),
        )
        config.validate()
        return config

    @classmethod
    def load(cls, path: str) -> "AppConfig":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"failed to load config {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigurationError("config root must be an object")
        return cls.from_dict(_expand_env(raw))


def _from_mapping(model: Type[T], raw: Any) -> T:
    if not isinstance(raw, Mapping):
        raise ConfigurationError(f"{model.__name__} config must be an object")
    unknown = set(raw) - set(getattr(model, "__dataclass_fields__"))
    if unknown:
        raise ConfigurationError(f"unknown {model.__name__} keys: {sorted(unknown)}")
    return model(**dict(raw))


def _expand_env(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, str):
        match = _ENV_PATTERN.match(value)
        if match:
            name = match.group(1)
            if name not in os.environ:
                raise ConfigurationError(f"required environment variable is missing: {name}")
            return os.environ[name]
    return value
