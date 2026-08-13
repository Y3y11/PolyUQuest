"""Configurable, open-domain page refresh interval policy."""

from __future__ import annotations

from dataclasses import dataclass

from agent_rag.config import agent_config


@dataclass(frozen=True, slots=True)
class FreshnessPolicy:
    policy_version: str
    min_ttl_hours: float
    default_ttl_hours: float
    max_ttl_hours: float
    unchanged_multiplier: float
    changed_multiplier: float
    hot_access_threshold: int
    hot_access_multiplier: float
    failure_backoff_hours: float

    @classmethod
    def from_config(cls, config: dict | None = None) -> FreshnessPolicy:
        cfg = config or dict(agent_config.get("freshness", {}))
        return cls(
            policy_version=str(cfg.get("policy_version", "adaptive-freshness-v1")),
            min_ttl_hours=float(cfg.get("min_ttl_hours", 6)),
            default_ttl_hours=float(cfg.get("default_ttl_hours", 168)),
            max_ttl_hours=float(cfg.get("max_ttl_hours", 720)),
            unchanged_multiplier=float(cfg.get("unchanged_multiplier", 1.5)),
            changed_multiplier=float(cfg.get("changed_multiplier", 0.5)),
            hot_access_threshold=int(cfg.get("hot_access_threshold", 10)),
            hot_access_multiplier=float(cfg.get("hot_access_multiplier", 0.5)),
            failure_backoff_hours=float(cfg.get("failure_backoff_hours", 1)),
        )

    def clamp(self, hours: float) -> float:
        return round(max(self.min_ttl_hours, min(hours, self.max_ttl_hours)), 4)

    def initial_ttl(self, quality_score: float, business_priority: float = 1.0) -> float:
        quality = max(0.0, min(quality_score, 1.0))
        priority = max(0.25, business_priority)
        # Valuable pages receive slightly more frequent validation. The effect
        # is intentionally bounded; historical changes become the stronger signal.
        quality_multiplier = 1.15 - (0.30 * quality)
        return self.clamp(self.default_ttl_hours * quality_multiplier / priority)

    def after_unchanged(self, current_ttl: float, access_count: int = 0) -> float:
        hours = current_ttl * self.unchanged_multiplier
        if access_count >= self.hot_access_threshold:
            hours *= self.hot_access_multiplier
        return self.clamp(hours)

    def after_changed(self, current_ttl: float, access_count: int = 0) -> float:
        hours = current_ttl * self.changed_multiplier
        if access_count >= self.hot_access_threshold:
            hours *= self.hot_access_multiplier
        return self.clamp(hours)

    def after_failure(self, consecutive_failures: int) -> float:
        exponent = max(0, consecutive_failures - 1)
        return self.clamp(self.failure_backoff_hours * (2**exponent))
