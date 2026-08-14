from __future__ import annotations

import unittest

from fastapi.routing import APIRoute

from agent_rag.api.main import app
from agent_rag.security.models import Role


def _roles(route: APIRoute) -> set[Role]:
    return {
        role
        for dependency in route.dependant.dependencies
        if isinstance(
            (role := getattr(dependency.call, "required_role", None)), Role
        )
    }


class SecurityRoutePolicyTests(unittest.TestCase):
    def test_every_business_route_has_an_explicit_minimum_role(self) -> None:
        public_prefixes = ("/api/health",)
        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue
            with self.subTest(path=route.path):
                if route.path.startswith(public_prefixes):
                    self.assertEqual(_roles(route), set())
                else:
                    self.assertTrue(_roles(route), f"unprotected route: {route.path}")

    def test_side_effecting_operations_require_admin(self) -> None:
        admin_paths = {
            "/api/indexing/jobs/{job_id}/retry",
            "/api/indexing/reconciliation/runs/{run_id}/execute",
            "/api/freshness/refresh-now",
            "/api/freshness/pause",
            "/api/freshness/resume",
            "/api/security/audit",
            "/api/security/audit/stats",
        }
        routes = {
            route.path: route
            for route in app.routes
            if isinstance(route, APIRoute)
        }
        self.assertTrue(admin_paths <= routes.keys())
        for path in admin_paths:
            with self.subTest(path=path):
                self.assertIn(Role.admin, _roles(routes[path]))

    def test_query_graph_and_operations_have_expected_roles(self) -> None:
        routes = {
            route.path: route
            for route in app.routes
            if isinstance(route, APIRoute)
        }
        self.assertIn(Role.reader, _roles(routes["/api/agent/query"]))
        self.assertIn(Role.reader, _roles(routes["/api/graph/data"]))
        self.assertIn(Role.operator, _roles(routes["/api/indexing/jobs"]))
        self.assertIn(Role.operator, _roles(routes["/api/telemetry/runs"]))


if __name__ == "__main__":
    unittest.main()
