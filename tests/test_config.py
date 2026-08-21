import os
import unittest
from unittest.mock import patch

from agent_runtime.config import AppConfig
from agent_runtime.errors import ConfigurationError


class ConfigTests(unittest.TestCase):
    def test_defaults_are_valid(self):
        config = AppConfig.from_dict({})
        self.assertEqual(config.provider.backend, "mock")
        self.assertEqual(config.runtime.max_concurrency, 10)
        self.assertEqual(config.runtime.queue_capacity, 100)

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(ConfigurationError):
            AppConfig.from_dict({"unknown": True})

    def test_environment_expansion(self):
        from agent_runtime.config import _expand_env

        with patch.dict(os.environ, {"TEST_AGENT_MODEL": "local-model"}):
            self.assertEqual(_expand_env("${TEST_AGENT_MODEL}"), "local-model")

    def test_run_registry_must_cover_active_and_queued_runs(self):
        with self.assertRaises(ConfigurationError):
            AppConfig.from_dict(
                {
                    "runtime": {
                        "max_concurrency": 2,
                        "queue_capacity": 3,
                        "max_run_records": 4,
                    }
                }
            )


if __name__ == "__main__":
    unittest.main()
