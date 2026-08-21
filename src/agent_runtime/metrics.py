"""Thread-safe in-process metrics for local and single-worker deployments."""

from __future__ import annotations

import threading
from collections import defaultdict
from contextlib import contextmanager
from time import perf_counter
from typing import DefaultDict, Dict, Iterator


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: DefaultDict[str, float] = defaultdict(float)

    def increment(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            self._values[name] += value

    def set(self, name: str, value: float) -> None:
        with self._lock:
            self._values[name] = value

    @contextmanager
    def timer(self, name: str) -> Iterator[None]:
        started = perf_counter()
        try:
            yield
        finally:
            self.increment(f"{name}_seconds_sum", perf_counter() - started)
            self.increment(f"{name}_seconds_count")

    def snapshot(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._values)

    def render_prometheus(self) -> str:
        values = self.snapshot()
        return "\n".join(
            ["# TYPE agent_runtime_metric gauge"]
            + [
                f"agent_runtime_{_sanitize(key)} {value:.9g}"
                for key, value in sorted(values.items())
            ]
        ) + "\n"


def _sanitize(value: str) -> str:
    return "".join(character if character.isalnum() or character == "_" else "_" for character in value)
