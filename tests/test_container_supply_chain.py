from __future__ import annotations

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_rag.deployment import container_contract

ROOT = Path(__file__).resolve().parents[1]


def _load_validator():
    path = ROOT / "scripts" / "validate_supply_chain.py"
    spec = importlib.util.spec_from_file_location("validate_supply_chain", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ContainerRuntimeContractTests(unittest.TestCase):
    def test_contract_reports_all_required_runtime_properties(self) -> None:
        def writable_probe(path: Path) -> tuple[bool, str]:
            if path == Path("/app"):
                return False, "PermissionError"
            return True, "write_succeeded"

        with (
            patch.object(container_contract, "_current_uid", return_value=10001),
            patch.object(container_contract.Path, "exists", return_value=True),
            patch.object(
                container_contract.shutil,
                "which",
                side_effect=lambda name: f"/bin/{name}",
            ),
            patch.object(container_contract.importlib.util, "find_spec", return_value=object()),
            patch.object(container_contract, "_probe_writable", side_effect=writable_probe),
        ):
            report = container_contract.evaluate_container_contract()

        self.assertTrue(report["ok"])
        self.assertEqual(report["uid"], 10001)
        self.assertEqual(report["schema_version"], 1)
        self.assertTrue(all(check["ok"] for check in report["checks"]))

    def test_contract_fails_for_root_missing_command_and_writable_app_root(self) -> None:
        def writable_probe(path: Path) -> tuple[bool, str]:
            return True, "write_succeeded"

        with (
            patch.object(container_contract, "_current_uid", return_value=0),
            patch.object(container_contract.Path, "exists", return_value=True),
            patch.object(container_contract.shutil, "which", return_value=None),
            patch.object(container_contract.importlib.util, "find_spec", return_value=object()),
            patch.object(container_contract, "_probe_writable", side_effect=writable_probe),
        ):
            report = container_contract.evaluate_container_contract()

        failed = {check["name"] for check in report["checks"] if not check["ok"]}
        self.assertFalse(report["ok"])
        self.assertIn("non_root_uid", failed)
        self.assertIn("command:agent-rag-serve", failed)
        self.assertIn("app_root_read_only", failed)


class SupplyChainPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.validator = _load_validator()

    def test_repository_policy_and_workflows_are_valid(self) -> None:
        self.assertEqual(self.validator.validate_repository(ROOT), [])

    def test_exported_environment_is_derived_from_reviewed_policy(self) -> None:
        policy = self.validator.load_policy(ROOT)
        exported = dict(
            line.split("=", 1) for line in self.validator.export_environment(policy)
        )
        self.assertEqual(exported["TRIVY_VERSION"], "0.72.0")
        self.assertEqual(exported["TRIVY_BLOCKING_SEVERITIES"], "CRITICAL")
        self.assertEqual(exported["CONTAINER_UID_GID"], "10001:10001")
        self.assertEqual(exported["TRIVY_IGNORE_UNFIXED"], "true")

    def test_validator_rejects_floating_or_unapproved_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "configs").mkdir()
            (root / "frontend").mkdir()
            (root / ".github" / "workflows").mkdir(parents=True)
            shutil.copy2(ROOT / "configs" / "supply_chain.json", root / "configs")
            shutil.copy2(ROOT / "Dockerfile", root / "Dockerfile")
            shutil.copy2(ROOT / "frontend" / "Dockerfile", root / "frontend" / "Dockerfile")
            for source in (ROOT / ".github" / "workflows").glob("*.yml"):
                shutil.copy2(source, root / ".github" / "workflows" / source.name)

            gate = root / ".github" / "workflows" / "container-supply-chain-gate.yml"
            gate.write_text(
                gate.read_text(encoding="utf-8").replace(
                    "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
                    "actions/checkout@v6",
                    1,
                ),
                encoding="utf-8",
            )

            errors = self.validator.validate_repository(root)

        self.assertTrue(any("actions/checkout@v6" in error for error in errors))

    def test_validator_rejects_blocking_severity_missing_from_report(self) -> None:
        policy = self.validator.load_policy(ROOT)
        policy["trivy"]["blocking_severities"] = "CRITICAL"
        policy["trivy"]["report_severities"] = "HIGH"
        self.assertIn(
            "trivy.blocking_severities must be included in report_severities",
            self.validator.validate_policy(policy),
        )

    def test_validator_requires_runtime_security_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "frontend").mkdir()
            shutil.copy2(ROOT / "Dockerfile", root / "Dockerfile")
            shutil.copy2(ROOT / "frontend" / "Dockerfile", root / "frontend" / "Dockerfile")
            backend = root / "Dockerfile"
            backend.write_text(
                backend.read_text(encoding="utf-8").replace("apt-get upgrade -y", "apt-get check"),
                encoding="utf-8",
            )

            errors = self.validator.validate_dockerfiles(
                root, self.validator.load_policy(ROOT)
            )

        self.assertIn("Dockerfile: runtime OS security upgrade is missing", errors)


if __name__ == "__main__":
    unittest.main()
