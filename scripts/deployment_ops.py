"""Guarded production Compose backup, restore and health verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "compose.production.yml"
SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
RESTORE_CONFIRMATION = "I_UNDERSTAND_DATA_WILL_BE_REPLACED"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _compose(env_file: Path, *arguments: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "-f",
        str(COMPOSE_FILE),
        *arguments,
    ]


def _run(command: list[str], *, env: dict[str, str], dry_run: bool) -> None:
    print("+", " ".join(command))
    if not dry_run:
        subprocess.run(command, cwd=ROOT, env=env, check=True)


def _git_revision() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def backup(args: argparse.Namespace) -> None:
    backup_id = args.backup_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    if not SAFE_ID.fullmatch(backup_id):
        raise ValueError("backup id may contain only letters, digits, dot, dash and underscore")
    backup_root = args.backup_root.resolve()
    destination = backup_root / backup_id
    if destination.exists():
        raise FileExistsError(f"backup already exists: {destination}")
    if not args.dry_run:
        destination.mkdir(parents=True)
        manifest = {
            "schema_version": 1,
            "backup_id": backup_id,
            "created_at": datetime.now(UTC).isoformat(),
            "git_revision": _git_revision(),
            "compose_sha256": _sha256(COMPOSE_FILE),
            "consistency": "cold",
            "volumes": ["neo4j", "qdrant", "runtime", "cache"],
        }
        (destination / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )

    env = os.environ.copy()
    env.update({"BACKUP_ID": backup_id, "BACKUP_ROOT": str(backup_root)})
    stopped = False
    try:
        _run(
            _compose(args.env_file, "stop", "api", "worker", "frontend", "neo4j", "qdrant"),
            env=env,
            dry_run=args.dry_run,
        )
        stopped = True
        _run(
            _compose(args.env_file, "--profile", "ops", "run", "--rm", "backup"),
            env=env,
            dry_run=args.dry_run,
        )
    finally:
        if stopped or args.dry_run:
            _run(
                _compose(args.env_file, "up", "-d", "neo4j", "qdrant", "worker", "api", "frontend"),
                env=env,
                dry_run=args.dry_run,
            )
    label = "backup plan validated" if args.dry_run else "backup completed"
    print(f"{label}: {destination}")


def restore(args: argparse.Namespace) -> None:
    if args.confirm != RESTORE_CONFIRMATION:
        raise ValueError(f"restore requires --confirm {RESTORE_CONFIRMATION}")
    if not SAFE_ID.fullmatch(args.backup_id):
        raise ValueError("invalid backup id")
    backup_root = args.backup_root.resolve()
    source = backup_root / args.backup_id
    required = {
        "manifest.json",
        "SHA256SUMS",
        "neo4j.tgz",
        "qdrant.tgz",
        "runtime.tgz",
        "cache.tgz",
    }
    if not source.is_dir() or not required.issubset({p.name for p in source.iterdir()}):
        raise FileNotFoundError(f"backup is incomplete: {source}")

    env = os.environ.copy()
    env.update(
        {
            "BACKUP_ID": args.backup_id,
            "BACKUP_ROOT": str(backup_root),
            "CONFIRM_RESTORE": RESTORE_CONFIRMATION,
        }
    )
    _run(
        _compose(args.env_file, "stop", "api", "worker", "frontend", "neo4j", "qdrant"),
        env=env,
        dry_run=args.dry_run,
    )
    # Deliberately do not auto-start after a failed restore: serving a partially
    # replaced data set is less safe than explicit operator intervention.
    _run(
        _compose(args.env_file, "--profile", "ops", "run", "--rm", "restore"),
        env=env,
        dry_run=args.dry_run,
    )
    _run(
        _compose(args.env_file, "up", "-d", "neo4j", "qdrant", "worker", "api", "frontend"),
        env=env,
        dry_run=args.dry_run,
    )
    print(f"restore completed: {source}")


def verify(args: argparse.Namespace) -> None:
    endpoints = (
        f"{args.api_url.rstrip('/')}/api/health/live",
        f"{args.api_url.rstrip('/')}/api/health/ready",
        args.frontend_url,
    )
    deadline = time.monotonic() + args.timeout
    pending = set(endpoints)
    last_errors: dict[str, str] = {}
    while pending and time.monotonic() < deadline:
        for endpoint in tuple(pending):
            try:
                with urllib.request.urlopen(endpoint, timeout=5) as response:
                    if 200 <= response.status < 300:
                        pending.remove(endpoint)
                        print(f"ok: {endpoint}")
            except (OSError, urllib.error.URLError) as exc:
                last_errors[endpoint] = str(exc)
        if pending:
            time.sleep(2)
    if pending:
        details = "; ".join(f"{url}: {last_errors.get(url, 'timeout')}" for url in pending)
        raise RuntimeError(f"deployment verification failed: {details}")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument(
        "--env-file",
        type=Path,
        default=ROOT / "deploy" / ".env.production",
    )
    root.add_argument("--backup-root", type=Path, default=ROOT / "backups")
    root.add_argument("--dry-run", action="store_true")
    commands = root.add_subparsers(dest="command", required=True)

    backup_parser = commands.add_parser("backup")
    backup_parser.add_argument("--backup-id")
    backup_parser.set_defaults(func=backup)

    restore_parser = commands.add_parser("restore")
    restore_parser.add_argument("backup_id")
    restore_parser.add_argument("--confirm", required=True)
    restore_parser.set_defaults(func=restore)

    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    verify_parser.add_argument("--frontend-url", default="http://127.0.0.1:3000")
    verify_parser.add_argument("--timeout", type=float, default=180)
    verify_parser.set_defaults(func=verify)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        args.func(args)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
