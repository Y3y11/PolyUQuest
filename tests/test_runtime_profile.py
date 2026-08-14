from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from agent_rag.config import Settings
from agent_rag.deployment import runtime_profile


def _module_state(*, local_ml: bool, torch: bool, flag: bool = False):
    states = {
        "sentence_transformers": local_ml,
        "torch": torch,
        "FlagEmbedding": flag,
    }
    return lambda module: states[module]


class RuntimeProfileTests(unittest.TestCase):
    def _marker(self, root: Path, value: str) -> Path:
        marker = root / ".runtime-profile"
        marker.write_text(value, encoding="utf-8")
        return marker

    def test_remote_profile_accepts_api_embedding_without_local_ml(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = self._marker(Path(temp_dir), "remote")
            with patch.object(
                runtime_profile,
                "_module_available",
                side_effect=_module_state(local_ml=False, torch=False),
            ):
                capabilities = runtime_profile.validate_runtime_capabilities(
                    declared_profile="remote",
                    embedding_provider="siliconflow",
                    marker_path=marker,
                )

        self.assertEqual(capabilities.effective_profile, "remote")
        self.assertFalse(capabilities.local_ml_available)

    def test_remote_profile_rejects_local_embedding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = self._marker(Path(temp_dir), "remote")
            with (
                patch.object(
                    runtime_profile,
                    "_module_available",
                    side_effect=_module_state(local_ml=False, torch=False),
                ),
                self.assertRaisesRegex(ValueError, "requires.*local-ml"),
            ):
                runtime_profile.validate_runtime_capabilities(
                    declared_profile="remote",
                    embedding_provider="local",
                    marker_path=marker,
                )

    def test_marker_cannot_be_overridden_by_environment_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = self._marker(Path(temp_dir), "remote")
            with self.assertRaisesRegex(ValueError, "does not match"):
                runtime_profile.inspect_runtime_capabilities(
                    "local-ml",
                    marker_path=marker,
                )

    def test_local_ml_profile_requires_both_runtime_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = self._marker(Path(temp_dir), "local-ml")
            with (
                patch.object(
                    runtime_profile,
                    "_module_available",
                    side_effect=_module_state(local_ml=True, torch=False),
                ),
                self.assertRaisesRegex(ValueError, "requires sentence-transformers and torch"),
            ):
                runtime_profile.validate_runtime_capabilities(
                    declared_profile="local-ml",
                    embedding_provider="local",
                    marker_path=marker,
                )

    def test_local_ml_profile_rejects_unused_flag_embedding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = self._marker(Path(temp_dir), "local-ml")
            with (
                patch.object(
                    runtime_profile,
                    "_module_available",
                    side_effect=_module_state(local_ml=True, torch=True, flag=True),
                ),
                self.assertRaisesRegex(ValueError, "not a supported serving dependency"),
            ):
                runtime_profile.validate_runtime_capabilities(
                    declared_profile="local-ml",
                    embedding_provider="local",
                    marker_path=marker,
                )

    def test_host_auto_profile_explains_missing_local_extra(self) -> None:
        with (
            patch.object(
                runtime_profile,
                "_module_available",
                side_effect=_module_state(local_ml=False, torch=False),
            ),
            self.assertRaisesRegex(ValueError, "uv sync --extra local-ml"),
        ):
            runtime_profile.validate_runtime_capabilities(
                declared_profile="auto",
                embedding_provider="local",
                marker_path=Path("missing-marker"),
            )

    def test_settings_fail_before_startup_for_incompatible_profile(self) -> None:
        with (
            patch.object(
                runtime_profile,
                "_module_available",
                side_effect=_module_state(local_ml=False, torch=False),
            ),
            self.assertRaisesRegex(ValidationError, "requires.*local-ml"),
        ):
            Settings(
                _env_file=None,
                app_runtime_profile="remote",
                embedding_provider="local",
            )


if __name__ == "__main__":
    unittest.main()
