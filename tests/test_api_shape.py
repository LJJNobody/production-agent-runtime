import json
import tempfile
import time
import unittest
from contextlib import contextmanager

from fastapi.testclient import TestClient

from agent_runtime.api import create_app


@contextmanager
def api_client(*, delay_seconds=0.01, max_concurrency=2, queue_capacity=4):
    config = {
        "provider": {
            "backend": "mock",
            "model": "api-test-model",
            "mock_delay_seconds": delay_seconds,
        },
        "runtime": {
            "max_concurrency": max_concurrency,
            "queue_capacity": queue_capacity,
            "queue_retry_after_seconds": 3,
            "max_run_records": max_concurrency + queue_capacity + 10,
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as config_file:
        json.dump(config, config_file)
        config_file.flush()
        with TestClient(create_app(config_file.name)) as client:
            yield client


class APIContractTests(unittest.TestCase):
    def test_openapi_health_and_structured_errors(self):
        with api_client() as client:
            schema_response = client.get("/openapi.json")
            self.assertEqual(schema_response.status_code, 200)
            paths = schema_response.json()["paths"]
            self.assertIn("/v1/runs", paths)
            self.assertIn("429", paths["/v1/runs"]["post"]["responses"])
            self.assertEqual(client.get("/docs").status_code, 200)

            health = client.get("/healthz")
            self.assertEqual(health.status_code, 200)
            self.assertTrue(health.json()["ready"])

            missing = client.get("/v1/runs/missing")
            self.assertEqual(missing.status_code, 404)
            self.assertEqual(missing.json()["error"]["code"], "run_not_found")

            invalid = client.post("/v1/runs", json={"input": ""})
            self.assertEqual(invalid.status_code, 422)
            self.assertEqual(invalid.json()["error"]["code"], "validation_error")

    def test_idempotency_replays_and_conflicts(self):
        headers = {"Idempotency-Key": "same-request"}
        with api_client() as client:
            first = client.post("/v1/runs", json={"input": "hello"}, headers=headers)
            replay = client.post("/v1/runs", json={"input": "hello"}, headers=headers)
            conflict = client.post(
                "/v1/runs",
                json={"input": "different"},
                headers=headers,
            )

            self.assertEqual(first.status_code, 202)
            self.assertEqual(replay.status_code, 202)
            self.assertEqual(first.json()["id"], replay.json()["id"])
            self.assertEqual(replay.headers["Idempotency-Key"], "same-request")
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(
                conflict.json()["error"]["code"],
                "idempotency_conflict",
            )

    def test_queue_overload_returns_retry_after(self):
        with api_client(delay_seconds=2, max_concurrency=1, queue_capacity=1) as client:
            first = client.post("/v1/runs", json={"input": "first"})
            self.assertEqual(first.status_code, 202)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                state = client.get(f"/v1/runs/{first.json()['id']}").json()["state"]
                if state in {"running", "waiting"}:
                    break
                time.sleep(0.01)
            else:
                self.fail("first run did not start")

            second = client.post("/v1/runs", json={"input": "second"})
            rejected = client.post("/v1/runs", json={"input": "third"})

            self.assertEqual(second.status_code, 202)
            self.assertEqual(rejected.status_code, 429)
            self.assertEqual(rejected.headers["Retry-After"], "3")
            self.assertTrue(rejected.json()["error"]["retryable"])


if __name__ == "__main__":
    unittest.main()
