from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_rag.security.models import SecurityAuditEvent
from agent_rag.security.store import SecurityAuditStore


def _event(event_id: str, created_at: str, outcome: str = "allowed"):
    return SecurityAuditEvent(
        event_id=event_id,
        request_id=f"req-{event_id}",
        created_at=created_at,
        principal_id="service-a",
        role="reader",
        auth_mode="api_key",
        method="GET",
        route="/api/query",
        required_role="reader",
        status_code=200,
        outcome=outcome,
    )


class SecurityAuditStoreTests(unittest.TestCase):
    def test_filters_stats_and_retention_purge(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SecurityAuditStore(Path(temp_dir) / "audit.sqlite3")
            store.record(_event("old", "2026-01-01T00:00:00+00:00"))
            store.record(
                _event("new", "2026-08-14T00:00:00+00:00", "forbidden")
            )

            self.assertEqual(store.stats()["total"], 2)
            self.assertEqual(len(store.list(outcome="forbidden")), 1)
            self.assertEqual(
                store.purge("2026-06-01T00:00:00+00:00"),
                1,
            )
            self.assertEqual([event.event_id for event in store.list()], ["new"])


if __name__ == "__main__":
    unittest.main()
