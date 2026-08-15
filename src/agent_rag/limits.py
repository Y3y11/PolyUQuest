"""Public hard limits shared by request schemas and deployment policy."""

AGENT_BUDGET_LIMITS = {
    "max_iterations": 8,
    "max_pages": 20,
    "max_depth": 4,
    "max_seconds": 300,
}
