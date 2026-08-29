from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.model_config import load_model_config


class TestModelConfig(unittest.TestCase):
    def test_module_model_overrides_common_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text(
                "\n".join(
                    [
                        "MODEL_API_KEY=common-key",
                        "MODEL_BASE_URL=http://internal-gateway/v1",
                        "MODEL_TIMEOUT_SECONDS=45",
                        "MODEL_MAX_RETRIES=1",
                        "MODEL_VERIFY_TLS=false",
                        "MODEL_TRUST_ENV=true",
                        "TREE_MODEL=tree-model",
                        "QUALITY_MODEL=quality-model",
                        "TASK_GENERATION_MODEL=task-model",
                    ]
                ),
                encoding="utf-8",
            )

            tree = load_model_config(env, module="tree")
            quality = load_model_config(env, module="quality")
            task = load_model_config(env, module="task_generation")

            self.assertEqual(tree.model, "tree-model")
            self.assertEqual(quality.model, "quality-model")
            self.assertEqual(task.model, "task-model")
            self.assertEqual(tree.base_url, "http://internal-gateway/v1")
            self.assertEqual(tree.api_key, "common-key")
            self.assertEqual(tree.timeout, 45)
            self.assertEqual(tree.max_retries, 1)
            self.assertFalse(tree.verify)
            self.assertTrue(tree.trust_env)


    def test_module_endpoint_and_key_override_legacy_values(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text(
                "\n".join(
                    [
                        "YUNAI_API_KEY=legacy-key",
                        "MODEL_URL=http://legacy-gateway/v1",
                        "MODEL_NAME=legacy-model",
                        "QUALITY_API_KEY=quality-key",
                        "QUALITY_BASE_URL=http://quality-gateway/v1",
                        "QUALITY_MODEL=quality-model",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_model_config(env, module="quality")

            self.assertEqual(config.api_key, "quality-key")
            self.assertEqual(config.base_url, "http://quality-gateway/v1")
            self.assertEqual(config.model, "quality-model")


    def test_missing_endpoint_does_not_fall_back_to_an_external_default(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text("MODEL_NAME=local-model\n", encoding="utf-8")

            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "BASE_URL"):
                    load_model_config(env, module="tree")
