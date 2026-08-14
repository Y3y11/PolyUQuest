"""Validate and export the container supply-chain policy.

The validator intentionally uses only the Python standard library so it can run
before project dependencies are installed in CI.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

POLICY_PATH = Path("configs/supply_chain.json")
WORKFLOW_PATH = Path(".github/workflows/container-supply-chain-gate.yml")
USES_PATTERN = re.compile(r"\buses:\s*[\"']?([^@\s\"']+)@([^\s#\"']+)")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
UID_GID_PATTERN = re.compile(r"^[1-9]\d*:[1-9]\d*$")
ALLOWED_SEVERITIES = {"UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"}


def load_policy(root: Path) -> dict[str, Any]:
    path = root / POLICY_PATH
    with path.open(encoding="utf-8") as handle:
        policy = json.load(handle)
    if not isinstance(policy, dict):
        raise ValueError(f"{POLICY_PATH} must contain a JSON object")
    return policy


def _nested(policy: dict[str, Any], *keys: str) -> Any:
    value: Any = policy
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            raise KeyError(".".join(keys))
        value = value[key]
    return value


def _severity_set(value: Any) -> set[str]:
    if not isinstance(value, str):
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def validate_policy(policy: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    if policy.get("schema_version") != 1:
        errors.append("schema_version must be 1")

    try:
        version = _nested(policy, "trivy", "version")
        if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
            errors.append("trivy.version must use X.Y.Z format")
    except KeyError as exc:
        errors.append(f"missing policy key: {exc.args[0]}")

    try:
        checksum = _nested(policy, "trivy", "linux_amd64_archive_sha256")
        if not isinstance(checksum, str) or not SHA256_PATTERN.fullmatch(checksum):
            errors.append("trivy.linux_amd64_archive_sha256 must be 64 lowercase hex characters")
    except KeyError as exc:
        errors.append(f"missing policy key: {exc.args[0]}")

    for key in ("blocking_severities", "report_severities"):
        try:
            severities = _severity_set(_nested(policy, "trivy", key))
            if not severities or not severities <= ALLOWED_SEVERITIES:
                errors.append(f"trivy.{key} contains an unsupported severity")
        except KeyError as exc:
            errors.append(f"missing policy key: {exc.args[0]}")

    try:
        blocking = _severity_set(_nested(policy, "trivy", "blocking_severities"))
        reported = _severity_set(_nested(policy, "trivy", "report_severities"))
        if not blocking <= reported:
            errors.append("trivy.blocking_severities must be included in report_severities")
    except KeyError:
        pass

    try:
        if not isinstance(_nested(policy, "trivy", "ignore_unfixed"), bool):
            errors.append("trivy.ignore_unfixed must be a boolean")
    except KeyError as exc:
        errors.append(f"missing policy key: {exc.args[0]}")

    try:
        uid_gid = _nested(policy, "contract", "uid_gid")
        if not isinstance(uid_gid, str) or not UID_GID_PATTERN.fullmatch(uid_gid):
            errors.append("contract.uid_gid must use a non-root UID:GID")
    except KeyError as exc:
        errors.append(f"missing policy key: {exc.args[0]}")

    try:
        command = _nested(policy, "contract", "backend_command")
        if not isinstance(command, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", command):
            errors.append("contract.backend_command must be a simple executable name")
    except KeyError as exc:
        errors.append(f"missing policy key: {exc.args[0]}")

    try:
        retention = _nested(policy, "artifacts", "retention_days")
        valid_retention = (
            isinstance(retention, int)
            and not isinstance(retention, bool)
            and 1 <= retention <= 90
        )
        if not valid_retention:
            errors.append("artifacts.retention_days must be an integer from 1 to 90")
    except KeyError as exc:
        errors.append(f"missing policy key: {exc.args[0]}")

    actions = policy.get("actions")
    if not isinstance(actions, dict) or not actions:
        errors.append("actions must contain the approved GitHub Actions pins")
    else:
        for action, commit in actions.items():
            if not isinstance(action, str) or "/" not in action:
                errors.append(f"invalid action name: {action!r}")
            if not isinstance(commit, str) or not COMMIT_PATTERN.fullmatch(commit):
                errors.append(f"action {action} must be pinned to a full commit SHA")

    return errors


def validate_workflows(root: Path, policy: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    workflow_dir = root / ".github" / "workflows"
    actions = policy.get("actions", {})
    if not workflow_dir.is_dir():
        return [".github/workflows directory is missing"]

    workflow_paths = sorted((*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")))
    if not workflow_paths:
        return ["no GitHub Actions workflows found"]

    for path in workflow_paths:
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(root)
        for match in USES_PATTERN.finditer(text):
            action, ref = match.groups()
            if action.startswith("./"):
                continue
            expected = actions.get(action) if isinstance(actions, dict) else None
            if expected is None:
                errors.append(f"{relative}: action {action} is not approved in {POLICY_PATH}")
            elif ref != expected:
                errors.append(f"{relative}: {action}@{ref} must use {expected}")

    gate_path = root / WORKFLOW_PATH
    if not gate_path.is_file():
        errors.append(f"{WORKFLOW_PATH} is missing")
        return errors

    gate = gate_path.read_text(encoding="utf-8")
    forbidden = {
        "pull_request_target:": "pull_request_target is forbidden for image builds",
        "continue-on-error:": "security gate failures must not be ignored",
        ":latest": "mutable :latest image tags are forbidden",
    }
    for token, message in forbidden.items():
        if token in gate:
            errors.append(f"{WORKFLOW_PATH}: {message}")

    required_fragments = {
        "permissions:\n  contents: read": (
            "workflow must grant only read access to repository contents"
        ),
        "persist-credentials: false": "checkout credentials must not persist",
        "python scripts/validate_supply_chain.py validate": "policy validation step is missing",
        "python scripts/validate_supply_chain.py export-env": "policy export step is missing",
        "docker buildx": "BuildKit/buildx evidence step is missing",
        "--read-only": "read-only container contract is missing",
        "--user \"$CONTAINER_UID_GID\"": "non-root UID:GID contract is missing",
        "cyclonedx": "CycloneDX SBOM generation is missing",
        "--exit-code 1": "blocking vulnerability scan is missing",
        "sha256sum --check --strict": "scanner archive checksum verification is missing",
        "$TRIVY_IGNORE_UNFIXED": "ignore-unfixed must be driven by reviewed policy",
        "if: always()": "evidence must upload even when a gate fails",
    }
    for fragment, message in required_fragments.items():
        if fragment not in gate:
            errors.append(f"{WORKFLOW_PATH}: {message}")

    return errors


def validate_dockerfiles(root: Path, policy: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    try:
        uid_gid = str(_nested(policy, "contract", "uid_gid"))
    except KeyError:
        return errors

    for relative in (Path("Dockerfile"), Path("frontend/Dockerfile")):
        path = root / relative
        if not path.is_file():
            errors.append(f"{relative} is missing")
            continue
        text = path.read_text(encoding="utf-8")
        if f"USER {uid_gid}" not in text:
            errors.append(f"{relative}: runtime user must be {uid_gid}")
        if re.search(r"(?im)^\s*FROM\s+\S+:latest(?:\s|$)", text):
            errors.append(f"{relative}: mutable latest base image is forbidden")

    backend_path = root / "Dockerfile"
    if backend_path.is_file():
        backend = backend_path.read_text(encoding="utf-8")
        if "apt-get upgrade -y" not in backend:
            errors.append("Dockerfile: runtime OS security upgrade is missing")

    frontend_path = root / "frontend" / "Dockerfile"
    if frontend_path.is_file():
        frontend = frontend_path.read_text(encoding="utf-8")
        if "apk upgrade --no-cache" not in frontend:
            errors.append("frontend/Dockerfile: runtime OS security upgrade is missing")
        if "rm -rf /usr/local/lib/node_modules/npm" not in frontend:
            errors.append("frontend/Dockerfile: unused runtime npm toolchain must be removed")
    return errors


def validate_repository(root: Path) -> list[str]:
    try:
        policy = load_policy(root)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [f"unable to load {POLICY_PATH}: {exc}"]
    return [
        *validate_policy(policy),
        *validate_workflows(root, policy),
        *validate_dockerfiles(root, policy),
    ]


def export_environment(policy: dict[str, Any]) -> list[str]:
    ignore_unfixed = _nested(policy, "trivy", "ignore_unfixed")
    return [
        f"TRIVY_VERSION={_nested(policy, 'trivy', 'version')}",
        f"TRIVY_ARCHIVE_SHA256={_nested(policy, 'trivy', 'linux_amd64_archive_sha256')}",
        f"TRIVY_BLOCKING_SEVERITIES={_nested(policy, 'trivy', 'blocking_severities')}",
        f"TRIVY_REPORT_SEVERITIES={_nested(policy, 'trivy', 'report_severities')}",
        f"TRIVY_IGNORE_UNFIXED={str(ignore_unfixed).lower()}",
        f"CONTAINER_UID_GID={_nested(policy, 'contract', 'uid_gid')}",
        f"BACKEND_CONTRACT_COMMAND={_nested(policy, 'contract', 'backend_command')}",
        f"ARTIFACT_RETENTION_DAYS={_nested(policy, 'artifacts', 'retention_days')}",
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("validate", "export-env"),
        nargs="?",
        default="validate",
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    errors = validate_repository(root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    if args.command == "export-env":
        for line in export_environment(load_policy(root)):
            print(line)
    else:
        print("Container supply-chain policy is valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
