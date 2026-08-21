"""Project-specific exception hierarchy."""


class AgentRuntimeError(Exception):
    """Base error for expected runtime failures."""


class ConfigurationError(AgentRuntimeError):
    pass


class InvalidStateTransition(AgentRuntimeError):
    pass


class RunNotFoundError(AgentRuntimeError):
    pass


class RunQueueFullError(AgentRuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("run queue is at capacity")
        self.retry_after_seconds = retry_after_seconds


class IdempotencyConflictError(AgentRuntimeError):
    pass


class ToolError(AgentRuntimeError):
    pass


class ToolNotFoundError(ToolError):
    pass


class LLMError(AgentRuntimeError):
    pass


class TransientLLMError(LLMError):
    """A provider failure that may succeed when retried."""


class PermanentLLMError(LLMError):
    """A request failure that should not be retried."""


class CircuitOpenError(TransientLLMError):
    pass
