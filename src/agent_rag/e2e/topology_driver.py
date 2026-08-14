"""Host-side driver for the real API/Worker production-topology E2E gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import httpx

from agent_rag.e2e.contract import BusinessE2EReport, ContractRecorder

_ROOT = Path(__file__).resolve().parents[3]
_T = TypeVar("_T")
class TopologyE2EDriver:
    def __init__(
        self,
        *,
        compose_file: Path,
        project: str,
        token: str,
        api_base: str,
        fixture_base: str,
        output: Path,
        timeout_seconds: float,
        build: bool,
    ):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,62}", project):
            raise ValueError("Topology Compose project has an invalid format")
        if not re.fullmatch(r"[a-zA-Z0-9._-]{3,80}", token):
            raise ValueError("Topology scenario token has an invalid format")
        self.compose_file = compose_file
        self.project = project
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.fixture_base = fixture_base.rstrip("/")
        self.output = output
        self.timeout_seconds = timeout_seconds
        self.build = build
        self.canonical_url = f"http://e2e.test/access/{token}/"
        self.reader_key = f"{token}-reader"
        self.admin_key = f"{token}-admin"
        self.query = (
            f"How does an employee request {token} production database access? "
            "Provide the steps and approval."
        )
        self.report = BusinessE2EReport(
            scenario_id="production-topology-e2e",
            scenario_token=token,
            code_version=os.getenv("GITHUB_SHA", "").strip(),
        )
        self.recorder = ContractRecorder(self.report)
        self.client = httpx.Client(timeout=15.0, trust_env=False)
        self.compose_env = dict(os.environ)
        self.compose_env["TOPOLOGY_E2E_TOKEN"] = token
        self.compose_env["TOPOLOGY_API_AUTH_KEYS"] = ",".join(
            (
                f"topology-reader:reader:{self._key_hash(self.reader_key)}",
                f"topology-admin:admin:{self._key_hash(self.admin_key)}",
            )
        )
        self.compose_env.setdefault("TOPOLOGY_API_PORT", "18000")
        self.compose_env.setdefault("TOPOLOGY_FIXTURE_PORT", "18080")

    def _compose(self, *arguments: str, capture: bool = False) -> str:
        command = [
            "docker",
            "compose",
            "-f",
            str(self.compose_file),
            "-p",
            self.project,
            *arguments,
        ]
        result = subprocess.run(
            command,
            cwd=_ROOT,
            env=self.compose_env,
            check=True,
            text=True,
            capture_output=capture,
        )
        return result.stdout if capture else ""

    @staticmethod
    def _key_hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _headers(self, role: str | None) -> dict[str, str]:
        if role is None:
            return {}
        selected = self.reader_key if role == "reader" else self.admin_key
        return {"X-API-Key": selected}

    def _api(
        self,
        method: str,
        path: str,
        *,
        key: str | None = "admin",
        expected: int = 200,
        **kwargs: Any,
    ) -> httpx.Response:
        response = self.client.request(
            method,
            f"{self.api_base}{path}",
            headers=self._headers(key),
            **kwargs,
        )
        if response.status_code != expected:
            raise RuntimeError(
                f"{method} {path} returned {response.status_code}, expected {expected}: "
                f"{response.text[:300]}"
            )
        return response

    def _fixture(
        self,
        method: str,
        path: str,
        *,
        expected: int = 200,
    ) -> httpx.Response:
        response = self.client.request(method, f"{self.fixture_base}{path}")
        if response.status_code != expected:
            raise RuntimeError(
                f"Fixture {method} {path} returned {response.status_code}: "
                f"{response.text[:300]}"
            )
        return response

    def _wait(
        self,
        label: str,
        probe: Callable[[], _T | None],
        *,
        timeout: float | None = None,
        interval: float = 0.25,
    ) -> _T:
        deadline = time.monotonic() + (timeout or self.timeout_seconds)
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                result = probe()
                if result is not None:
                    return result
            except Exception as exc:
                last_error = exc
            time.sleep(interval)
        raise TimeoutError(f"Timed out waiting for {label}; last_error={last_error}")

    def _wait_http(self, url: str) -> dict[str, Any]:
        def probe() -> dict[str, Any] | None:
            response = self.client.get(url)
            if response.status_code != 200:
                return None
            return response.json()

        return self._wait(url, probe)

    def _sse_query(self) -> tuple[list[tuple[str, Any]], dict[str, str]]:
        payload = {
            "query": self.query,
            "mode": "block",
            "explore_web": True,
            "persist_discoveries": True,
            "budget": {
                "max_iterations": 2,
                "max_pages": 1,
                "max_depth": 2,
                "max_seconds": 60,
            },
        }
        events: list[tuple[str, Any]] = []
        with self.client.stream(
            "POST",
            f"{self.api_base}/api/agent/query/stream",
            headers=self._headers("reader"),
            json=payload,
            timeout=90.0,
        ) as response:
            response.raise_for_status()
            event_name = "message"
            data_lines: list[str] = []
            for line in response.iter_lines():
                if not line:
                    if data_lines:
                        raw = "\n".join(data_lines)
                        try:
                            data: Any = json.loads(raw)
                        except json.JSONDecodeError:
                            data = raw
                        events.append((event_name, data))
                    event_name = "message"
                    data_lines = []
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
            if data_lines:
                events.append((event_name, json.loads("\n".join(data_lines))))
            headers = {
                "content-type": response.headers.get("content-type", ""),
                "x-request-id": response.headers.get("x-request-id", ""),
            }
        return events, headers

    @staticmethod
    def _done(events: list[tuple[str, Any]]) -> dict[str, Any]:
        done = [data for event, data in events if event == "done"]
        if len(done) != 1 or not isinstance(done[0], dict):
            raise RuntimeError(f"Expected exactly one SSE done event, received {len(done)}")
        return done[0]

    def _jobs(self) -> list[dict[str, Any]]:
        return self._api("GET", "/api/indexing/jobs?limit=50").json()

    def _job(self, job_id: str) -> dict[str, Any]:
        return self._api("GET", f"/api/indexing/jobs/{job_id}").json()

    def _workers(self) -> list[dict[str, Any]]:
        return self._api("GET", "/api/workers/status?limit=20").json()

    def run(self) -> BusinessE2EReport:
        failure: BaseException | None = None
        cold_job: dict[str, Any] = {}
        cold_done: dict[str, Any] = {}
        hot_done: dict[str, Any] = {}
        updated_done: dict[str, Any] = {}
        first_worker: dict[str, Any] = {}
        second_worker: dict[str, Any] = {}
        try:
            with self.recorder.stage("compose_start") as metrics:
                self._compose("config", "--quiet")
                if self.build:
                    self._compose("build", "api")
                self._compose("up", "-d", "neo4j", "qdrant", "fixture", "api")
                fixture_health = self._wait_http(
                    f"{self.fixture_base}/__control/health"
                )
                api_health = self._wait_http(f"{self.api_base}/api/health/ready")
                metrics.update(
                    {
                        "project": self.project,
                        "fixture_health": fixture_health,
                        "api_health": api_health,
                    }
                )
                self.recorder.check(
                    "topology.api_ready",
                    api_health.get("status") == "ok"
                    and api_health.get("neo4j") is True
                    and api_health.get("qdrant") is True,
                    expected="ready with Neo4j and Qdrant",
                    actual=api_health,
                    required=True,
                )

            with self.recorder.stage("authentication") as metrics:
                unauthorized = self._api(
                    "GET",
                    "/api/security/whoami",
                    key=None,
                    expected=401,
                )
                forbidden = self._api(
                    "GET",
                    "/api/indexing/stats",
                    key="reader",
                    expected=403,
                )
                reader = self._api(
                    "GET", "/api/security/whoami", key="reader"
                ).json()
                admin = self._api(
                    "GET", "/api/security/whoami", key="admin"
                ).json()
                metrics.update(
                    {
                        "unauthorized_status": unauthorized.status_code,
                        "forbidden_status": forbidden.status_code,
                        "reader_role": reader.get("role"),
                        "admin_role": admin.get("role"),
                    }
                )
                self.recorder.check(
                    "topology.auth_roles",
                    unauthorized.status_code == 401
                    and forbidden.status_code == 403
                    and reader.get("role") == "reader"
                    and admin.get("role") == "admin",
                    expected="401 / 403 / reader / admin",
                    actual=metrics,
                    required=True,
                )

            with self.recorder.stage("cold_sse_query") as metrics:
                fixture_before = self._fixture("GET", "/__control/state").json()
                events, headers = self._sse_query()
                cold_done = self._done(events)
                event_types = [event for event, _ in events]
                fixture_after = self._fixture("GET", "/__control/state").json()
                jobs = self._jobs()
                if len(jobs) != 1:
                    raise RuntimeError(f"Cold query created {len(jobs)} jobs, expected 1")
                cold_job = jobs[0]
                metrics.update(
                    {
                        "event_types": event_types,
                        "request_id": headers["x-request-id"],
                        "response_status": cold_done.get("response_status"),
                        "pages_fetched": cold_done.get("exploration", {}).get(
                            "pages_fetched"
                        ),
                        "job_status": cold_job.get("status"),
                        "fixture_request_delta": fixture_after["requests"]
                        - fixture_before["requests"],
                    }
                )
                self.report.audit_ids["cold_run"] = str(cold_done.get("run_id", ""))
                self.report.audit_ids["cold_job"] = str(cold_job.get("job_id", ""))
                self.report.audit_ids["cold_patch"] = str(
                    cold_job.get("patch_id", "")
                )
                self.recorder.check(
                    "topology.sse_contract",
                    bool(event_types)
                    and event_types[0] == "run_started"
                    and event_types[-1] == "done"
                    and event_types.count("done") == 1
                    and "action" in event_types
                    and "evidence" in event_types
                    and "assessment" in event_types
                    and headers["content-type"].startswith("text/event-stream")
                    and bool(headers["x-request-id"]),
                    expected="ordered audited SSE event stream",
                    actual=metrics,
                    required=True,
                )
                self.recorder.check(
                    "topology.cold_query_enqueues",
                    cold_done.get("response_status") == "answered"
                    and cold_done.get("exploration", {}).get("pages_fetched") == 1
                    and cold_done.get("exploration", {}).get(
                        "indexing_jobs_queued"
                    )
                    == 1
                    and cold_job.get("status") == "pending"
                    and metrics["fixture_request_delta"] == 1,
                    expected="answered / one fetch / one pending job",
                    actual=metrics,
                    required=True,
                )

            with self.recorder.stage("worker_claim") as metrics:
                self._compose("up", "-d", "worker")

                def claimed() -> dict[str, Any] | None:
                    job = self._job(str(cold_job["job_id"]))
                    return job if job.get("status") == "running" else None

                running_job = self._wait("cold job claimed", claimed)

                def healthy_worker() -> dict[str, Any] | None:
                    records = self._workers()
                    return records[0] if records and records[0].get("healthy") else None

                first_worker = self._wait("first Worker heartbeat", healthy_worker)
                metrics.update(
                    {
                        "job_status": running_job.get("status"),
                        "job_attempts": running_job.get("total_attempts"),
                        "worker_instance": first_worker.get("instance_id"),
                        "worker_healthy": first_worker.get("healthy"),
                    }
                )
                self.report.audit_ids["first_worker"] = str(
                    first_worker.get("instance_id", "")
                )
                self.recorder.check(
                    "topology.cross_process_claim",
                    running_job.get("status") == "running"
                    and first_worker.get("healthy") is True,
                    expected="independent Worker claimed pending API job",
                    actual=metrics,
                    required=True,
                )

            with self.recorder.stage("worker_sigkill") as metrics:
                self._compose("kill", "-s", "SIGKILL", "worker")

                def stale_worker() -> dict[str, Any] | None:
                    records = self._workers()
                    selected = next(
                        (
                            item
                            for item in records
                            if item.get("instance_id")
                            == first_worker.get("instance_id")
                        ),
                        None,
                    )
                    return selected if selected and not selected.get("healthy") else None

                stale = self._wait("killed Worker heartbeat to become stale", stale_worker)
                interrupted_job = self._job(str(cold_job["job_id"]))
                metrics.update(
                    {
                        "worker_instance": stale.get("instance_id"),
                        "worker_state": stale.get("state"),
                        "worker_healthy": stale.get("healthy"),
                        "heartbeat_age_seconds": stale.get("heartbeat_age_seconds"),
                        "job_status": interrupted_job.get("status"),
                    }
                )
                self.recorder.check(
                    "topology.sigkill_preserves_job",
                    stale.get("healthy") is False
                    and stale.get("state") == "running"
                    and interrupted_job.get("status") == "running",
                    expected="stale old heartbeat and durable running job",
                    actual=metrics,
                    required=True,
                )

            with self.recorder.stage("worker_lease_takeover") as metrics:
                self._compose("up", "-d", "worker")

                def replacement() -> dict[str, Any] | None:
                    records = self._workers()
                    return next(
                        (
                            item
                            for item in records
                            if item.get("healthy")
                            and item.get("instance_id")
                            != first_worker.get("instance_id")
                        ),
                        None,
                    )

                second_worker = self._wait("replacement Worker heartbeat", replacement)

                def succeeded() -> dict[str, Any] | None:
                    job = self._job(str(cold_job["job_id"]))
                    return job if job.get("status") == "succeeded" else None

                recovered_job = self._wait("lease takeover publication", succeeded)
                versions = self._api(
                    "GET",
                    "/api/indexing/versions",
                    params={"source_url": self.canonical_url, "limit": 20},
                ).json()
                metrics.update(
                    {
                        "old_worker": first_worker.get("instance_id"),
                        "new_worker": second_worker.get("instance_id"),
                        "job_status": recovered_job.get("status"),
                        "total_attempts": recovered_job.get("total_attempts"),
                        "published_versions": len(
                            [item for item in versions if item.get("status") == "published"]
                        ),
                    }
                )
                self.report.audit_ids["second_worker"] = str(
                    second_worker.get("instance_id", "")
                )
                if versions:
                    self.report.audit_ids["initial_version"] = str(
                        versions[0].get("version_id", "")
                    )
                self.recorder.check(
                    "topology.lease_takeover",
                    second_worker.get("instance_id")
                    != first_worker.get("instance_id")
                    and recovered_job.get("status") == "succeeded"
                    and int(recovered_job.get("total_attempts", 0)) >= 2
                    and bool(versions),
                    expected="new instance reclaims same job and publishes",
                    actual=metrics,
                    required=True,
                )

            with self.recorder.stage("hot_sse_query") as metrics:
                fixture_before = self._fixture("GET", "/__control/state").json()
                events, _ = self._sse_query()
                hot_done = self._done(events)
                fixture_after = self._fixture("GET", "/__control/state").json()
                jobs = self._jobs()
                metrics.update(
                    {
                        "response_status": hot_done.get("response_status"),
                        "pages_fetched": hot_done.get("exploration", {}).get(
                            "pages_fetched"
                        ),
                        "fixture_request_delta": fixture_after["requests"]
                        - fixture_before["requests"],
                        "job_count": len(jobs),
                    }
                )
                self.report.audit_ids["hot_run"] = str(hot_done.get("run_id", ""))
                self.recorder.check(
                    "topology.hot_query_reuses_index",
                    hot_done.get("response_status") == "answered"
                    and hot_done.get("exploration", {}).get("pages_fetched") == 0
                    and metrics["fixture_request_delta"] == 0
                    and len(jobs) == 1,
                    expected="answered from index without fetch or duplicate job",
                    actual=metrics,
                    required=True,
                )

            with self.recorder.stage("freshness_update") as metrics:
                changed_fixture = self._fixture(
                    "POST", "/__control/version/2"
                ).json()
                self._api(
                    "POST",
                    "/api/freshness/refresh-now",
                    params={"url": self.canonical_url},
                )

                def updated_jobs() -> list[dict[str, Any]] | None:
                    jobs = self._jobs()
                    succeeded_jobs = [
                        item for item in jobs if item.get("status") == "succeeded"
                    ]
                    return jobs if len(jobs) >= 2 and len(succeeded_jobs) >= 2 else None

                jobs = self._wait("freshness update publication", updated_jobs)

                def temporal_facts() -> dict[str, Any] | None:
                    stats = self._api(
                        "GET", "/api/indexing/knowledge-stats"
                    ).json()
                    return (
                        stats
                        if stats.get("active") == 1 and stats.get("retired", 0) >= 1
                        else None
                    )

                fact_stats = self._wait("fact retirement and activation", temporal_facts)
                versions = self._api(
                    "GET",
                    "/api/indexing/versions",
                    params={"source_url": self.canonical_url, "limit": 20},
                ).json()
                metrics.update(
                    {
                        "fixture_version": changed_fixture.get("version"),
                        "job_count": len(jobs),
                        "succeeded_jobs": len(
                            [item for item in jobs if item.get("status") == "succeeded"]
                        ),
                        "fact_stats": fact_stats,
                        "published_versions": len(
                            [item for item in versions if item.get("status") == "published"]
                        ),
                    }
                )
                update_jobs = [
                    item for item in jobs if item.get("job_id") != cold_job.get("job_id")
                ]
                if update_jobs:
                    self.report.audit_ids["update_job"] = str(
                        update_jobs[0].get("job_id", "")
                    )
                    self.report.audit_ids["update_patch"] = str(
                        update_jobs[0].get("patch_id", "")
                    )
                if versions:
                    self.report.audit_ids["update_version"] = str(
                        versions[0].get("version_id", "")
                    )
                self.recorder.check(
                    "topology.freshness_updates_facts",
                    changed_fixture.get("version") == 2
                    and len(jobs) >= 2
                    and fact_stats.get("active") == 1
                    and fact_stats.get("retired", 0) >= 1
                    and len(versions) >= 2,
                    expected="v2 published with old fact retired and new fact active",
                    actual=metrics,
                    required=True,
                )

            with self.recorder.stage("updated_hot_query") as metrics:
                fixture_before = self._fixture("GET", "/__control/state").json()
                events, _ = self._sse_query()
                updated_done = self._done(events)
                fixture_after = self._fixture("GET", "/__control/state").json()
                answer = str(updated_done.get("answer", ""))
                metrics.update(
                    {
                        "response_status": updated_done.get("response_status"),
                        "pages_fetched": updated_done.get("exploration", {}).get(
                            "pages_fetched"
                        ),
                        "fixture_request_delta": fixture_after["requests"]
                        - fixture_before["requests"],
                        "answer_contains_new_fact": "Security Review Board" in answer,
                    }
                )
                self.report.audit_ids["updated_run"] = str(
                    updated_done.get("run_id", "")
                )
                self.recorder.check(
                    "topology.updated_hot_query",
                    updated_done.get("response_status") == "answered"
                    and updated_done.get("exploration", {}).get("pages_fetched") == 0
                    and metrics["fixture_request_delta"] == 0
                    and "Security Review Board" in answer,
                    expected="new fact answered from index without fetch",
                    actual=metrics,
                    required=True,
                )

            with self.recorder.stage("audit_and_telemetry") as metrics:
                audit = self._api("GET", "/api/security/audit/stats").json()
                run_ids = [
                    str(cold_done.get("run_id", "")),
                    str(hot_done.get("run_id", "")),
                    str(updated_done.get("run_id", "")),
                ]
                telemetry = [
                    self._api("GET", f"/api/telemetry/runs/{run_id}").json()
                    for run_id in run_ids
                    if run_id
                ]
                runs = [item.get("run", {}) for item in telemetry]
                llm_calls = sum(int(item.get("llm_calls", 0)) for item in runs)
                billable_tokens = sum(
                    int(item.get("billable_input_tokens", 0))
                    + int(item.get("billable_output_tokens", 0))
                    for item in runs
                )
                fixture = self._fixture("GET", "/__control/state").json()
                metrics.update(
                    {
                        "security_audit": audit,
                        "telemetry_runs": len(telemetry),
                        "llm_calls": llm_calls,
                        "billable_tokens": billable_tokens,
                        "fixture_requests": fixture.get("requests"),
                    }
                )
                self.recorder.check(
                    "topology.audit_and_zero_model_cost",
                    audit.get("unauthorized", 0) >= 1
                    and audit.get("forbidden", 0) >= 1
                    and audit.get("allowed", 0) >= 1
                    and len(telemetry) == 3
                    and llm_calls == 0
                    and billable_tokens == 0
                    and fixture.get("requests") == 2,
                    expected="audited roles, three telemetry runs, zero model cost",
                    actual=metrics,
                    required=True,
                )

            self.report.summary = {
                "project": self.project,
                "canonical_url": self.canonical_url,
                "old_worker": first_worker.get("instance_id", ""),
                "new_worker": second_worker.get("instance_id", ""),
                "cold_job_total_attempts": self._job(str(cold_job["job_id"])).get(
                    "total_attempts"
                ),
                "fixture_requests": self._fixture(
                    "GET", "/__control/state"
                ).json().get("requests"),
            }
        except BaseException as exc:
            failure = exc
        finally:
            self.recorder.finish(failure)
            self.recorder.write(self.output)
            self.client.close()
        if failure is not None:
            raise failure
        return self.report

    def collect_logs(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            logs = self._compose("logs", "--no-color", capture=True)
        except Exception as exc:
            logs = f"Unable to collect Compose logs: {type(exc).__name__}: {exc}"
        destination.write_text(logs, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run production-topology E2E")
    parser.add_argument(
        "--compose-file",
        type=Path,
        default=_ROOT / "compose.topology-e2e.yml",
    )
    parser.add_argument(
        "--project",
        default=os.getenv("TOPOLOGY_E2E_PROJECT", "polyuquest-topology-e2e"),
    )
    parser.add_argument(
        "--token",
        default=os.getenv("TOPOLOGY_E2E_TOKEN", f"topology-{uuid.uuid4().hex[:10]}"),
    )
    parser.add_argument(
        "--api-base",
        default=os.getenv("TOPOLOGY_API_BASE", "http://127.0.0.1:18000"),
    )
    parser.add_argument(
        "--fixture-base",
        default=os.getenv("TOPOLOGY_FIXTURE_BASE", "http://127.0.0.1:18080"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/runtime/topology-e2e/report.json"),
    )
    parser.add_argument(
        "--logs",
        type=Path,
        default=Path("data/runtime/topology-e2e/compose.log"),
    )
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    driver = TopologyE2EDriver(
        compose_file=args.compose_file.resolve(),
        project=args.project,
        token=args.token,
        api_base=args.api_base,
        fixture_base=args.fixture_base,
        output=args.output.resolve(),
        timeout_seconds=args.timeout_seconds,
        build=not args.skip_build,
    )
    error: BaseException | None = None
    try:
        report = driver.run()
        print(
            json.dumps(
                {
                    "status": report.status,
                    "duration_ms": report.duration_ms,
                    "checks": len(report.checks),
                    "output": str(args.output.resolve()),
                },
                sort_keys=True,
            )
        )
    except BaseException as exc:
        error = exc
    finally:
        driver.collect_logs(args.logs.resolve())
    if error is not None:
        raise error
