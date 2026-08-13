from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from agent_rag.agent.evidence import EvidenceEvaluator, is_freshness_sensitive
from agent_rag.agent.query_profile import build_query_profile
from agent_rag.agent.schemas import AgentQueryRequest
from agent_rag.tools._ranking import (
    constraints_supported,
    frontier_score,
    normalize_url,
    trusted_url,
)
from agent_rag.tools.schemas import (
    EvidenceBlock,
    EvidenceRequirement,
    EvidenceScores,
    QueryConstraint,
    QueryProfile,
)


def _evidence(*, content: str, fetched_at: str | None = None) -> EvidenceBlock:
    return EvidenceBlock(
        block_id="block-1",
        content=content,
        source_url="https://docs.example.com/",
        fetched_at=fetched_at,
        scores=EvidenceScores(retrieval=0.9),
    )


def _profile(
    *,
    entity: str,
    aliases: list[str],
    field: str,
    value: str,
    value_aliases: list[str],
    excludes: list[str],
) -> QueryProfile:
    return QueryProfile(
        query="test query",
        constraints=[
            QueryConstraint(kind="entity", label=entity, aliases=aliases),
            QueryConstraint(
                kind="qualifier",
                label=value,
                field=field,
                value=value,
                aliases=value_aliases,
                excludes=excludes,
            ),
        ],
        intents=["procedure"],
        required_claims=[
            EvidenceRequirement(
                claim="actionable procedure",
                evidence_cues=["procedure", "steps", "configure", "deploy"],
            )
        ],
        source="llm_enriched",
    )


class EvidenceEvaluatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.evaluator = EvidenceEvaluator()

    def test_empty_evidence_expands_when_allowed(self) -> None:
        result = self.evaluator.assess("deployment steps", [], can_explore=True)
        self.assertEqual(result.decision, "expand")
        self.assertEqual(result.missing_claims, ["actionable procedure"])

    def test_relevant_fact_evidence_answers(self) -> None:
        result = self.evaluator.assess(
            "service port",
            [_evidence(content="The service port is 8080.")],
        )
        self.assertEqual(result.decision, "answer")

    def test_procedure_query_rejects_related_news(self) -> None:
        profile = _profile(
            entity="Payment Service",
            aliases=["payment service"],
            field="version",
            value="v4",
            value_aliases=["v4", "version 4"],
            excludes=["v3"],
        )
        result = self.evaluator.assess(
            "How do I deploy Payment Service v4?",
            [
                EvidenceBlock(
                    block_id="news",
                    content="Payment Service v4 received an engineering excellence award.",
                    source_url="https://intranet.example.com/news/payment-v4",
                    scores=EvidenceScores(retrieval=0.9),
                )
            ],
            query_profile=profile,
        )
        self.assertEqual(result.decision, "expand")

    def test_procedure_query_accepts_operational_evidence(self) -> None:
        profile = _profile(
            entity="Payment Service",
            aliases=["payment service"],
            field="version",
            value="v4",
            value_aliases=["v4", "version 4"],
            excludes=["v3"],
        )
        result = self.evaluator.assess(
            "How do I deploy Payment Service v4?",
            [
                EvidenceBlock(
                    block_id="guide",
                    content=(
                        "Payment Service v4 deployment procedure: configure the "
                        "environment, then run the release command."
                    ),
                    source_url="https://docs.example.com/payment/v4/deploy",
                    scores=EvidenceScores(retrieval=0.9),
                )
            ],
            query_profile=profile,
        )
        self.assertEqual(result.decision, "answer")

    def test_conflicting_qualifier_continues_exploration(self) -> None:
        profile = _profile(
            entity="Payment Service",
            aliases=["payment service"],
            field="version",
            value="v4",
            value_aliases=["v4", "version 4"],
            excludes=["v3"],
        )
        result = self.evaluator.assess(
            "How do I deploy Payment Service v4?",
            [
                EvidenceBlock(
                    block_id="wrong-version",
                    content="Payment Service v3 deployment procedure: configure legacy mode.",
                    source_url="https://docs.example.com/payment/v3/deploy",
                    scores=EvidenceScores(retrieval=0.9),
                )
            ],
            query_profile=profile,
        )
        self.assertEqual(result.decision, "expand")
        self.assertIn("qualifier", result.reasons[0])

    def test_required_value_plus_conflict_is_still_rejected(self) -> None:
        profile = _profile(
            entity="Payment Service",
            aliases=["payment service"],
            field="version",
            value="v4",
            value_aliases=["v4"],
            excludes=["v3"],
        )
        self.assertFalse(
            constraints_supported(
                profile,
                "Payment Service v4 guide migrated from the incompatible v3 procedure",
            )
        )

    def test_same_contract_covers_education_scenario(self) -> None:
        profile = _profile(
            entity="土木工程系",
            aliases=["土木工程系", "civil and environmental engineering", "cee"],
            field="degree_level",
            value="PhD",
            value_aliases=["phd", "doctor of philosophy", "博士"],
            excludes=["msc", "taught postgraduate"],
        )
        self.assertTrue(
            constraints_supported(
                profile,
                "CEE Doctor of Philosophy (PhD) application procedure",
            )
        )
        self.assertFalse(
            constraints_supported(
                profile,
                "CEE MSc taught postgraduate application procedure",
            )
        )

    def test_frontier_uses_profile_without_domain_rules(self) -> None:
        profile = _profile(
            entity="Payment Service",
            aliases=["payment", "payments"],
            field="version",
            value="v4",
            value_aliases=["v4"],
            excludes=["v3"],
        )
        correct = frontier_score(
            "How do I deploy Payment Service v4?",
            "https://docs.example.com/payments/v4/deployment-guide",
            "Payment Service v4 deployment guide",
            profile,
        )
        wrong = frontier_score(
            "How do I deploy Payment Service v4?",
            "https://docs.example.com/payments/v3/release-news",
            "Payment Service v3 release news",
            profile,
        )
        self.assertGreater(correct, wrong)

    def test_missing_claim_outweighs_repeated_qualifier(self) -> None:
        profile = _profile(
            entity="Payment Service",
            aliases=["payment"],
            field="version",
            value="v4",
            value_aliases=["version 4"],
            excludes=["v3"],
        )
        procedure = frontier_score(
            "How do I deploy Payment Service v4?",
            "https://docs.example.com/payment/application-procedure",
            "Payment Service deployment procedure and requirements",
            profile,
        )
        announcement = frontier_score(
            "How do I deploy Payment Service v4?",
            "https://docs.example.com/payment/v4/award",
            "Payment Service v4 engineering award",
            profile,
        )
        self.assertGreater(procedure, announcement)

    def test_short_anchor_can_match_a_multi_token_claim_cue(self) -> None:
        profile = _profile(
            entity="Department of Computing",
            aliases=["COMP", "CS"],
            field="level",
            value="PhD",
            value_aliases=["Doctor of Philosophy"],
            excludes=[],
        )
        profile.required_claims = [
            EvidenceRequirement(
                claim="doctoral application procedure",
                target_cues=["PhD", "Doctor of Philosophy"],
                evidence_cues=["PhD application", "how to apply"],
            )
        ]
        phd = frontier_score(
            "CS PhD application procedure",
            "https://institution.example/comp/study/phd-and-mphil/",
            "Department of Computing PhD and MPhil",
            profile,
        )
        minor = frontier_score(
            "CS PhD application procedure",
            "https://institution.example/comp/study/minor_cs/",
            "Department of Computing Minor in Computer Science",
            profile,
        )
        self.assertGreater(phd, minor)

    def test_claim_gap_is_reported_before_answering(self) -> None:
        profile = _profile(
            entity="Payment Service",
            aliases=["payment service"],
            field="version",
            value="v4",
            value_aliases=["version 4"],
            excludes=["v3"],
        )
        profile.required_claims.append(
            EvidenceRequirement(
                claim="rollback procedure",
                target_cues=["Payment Service"],
                evidence_cues=["rollback"],
            )
        )
        result = self.evaluator.assess(
            "How do I deploy and roll back Payment Service v4?",
            [_evidence(content="Payment Service v4 deployment procedure: configure and deploy.")],
            query_profile=profile,
        )
        self.assertEqual(result.decision, "expand")
        self.assertIn("rollback procedure", result.missing_claims)

    def test_education_connector_labels_use_same_frontier_contract(self) -> None:
        profile = _profile(
            entity="土木工程系",
            aliases=["civil and environmental engineering", "cee"],
            field="degree_level",
            value="PhD",
            value_aliases=["phd", "doctor of philosophy"],
            excludes=["msc", "taught postgraduate"],
        )
        matching = frontier_score(
            "香港理工大学土木工程系博士怎么申请",
            "https://institution.example/cee/",
            "Department of Civil and Environmental Engineering CEE",
            profile,
        )
        unrelated = frontier_score(
            "香港理工大学土木工程系博士怎么申请",
            "https://institution.example/design/",
            "School of Design",
            profile,
        )
        self.assertGreater(matching, unrelated)

    def test_constraint_matching_is_case_insensitive(self) -> None:
        profile = _profile(
            entity="Civil Engineering",
            aliases=["Department of Civil and Environmental Engineering", "CEE"],
            field="level",
            value="PhD",
            value_aliases=["Doctor of Philosophy"],
            excludes=["MSc"],
        )
        self.assertTrue(
            constraints_supported(
                profile,
                "department of civil and environmental engineering - phd procedure",
            )
        )

    def test_stale_evidence_refreshes_for_freshness_query(self) -> None:
        stale = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        result = self.evaluator.assess(
            "What is the latest release deadline?",
            [_evidence(content="The release deadline is in November.", fetched_at=stale)],
        )
        self.assertEqual(result.decision, "refresh")

    def test_persistence_requires_exploration(self) -> None:
        with self.assertRaises(ValueError):
            AgentQueryRequest(
                query="question", explore_web=False, persist_discoveries=True
            )

    def test_url_policy(self) -> None:
        self.assertTrue(trusted_url("https://www.polyu.edu.hk/study/"))
        self.assertFalse(trusted_url("https://polyu.edu.hk.evil.example/"))
        self.assertFalse(trusted_url("file:///etc/passwd"))
        self.assertEqual(
            normalize_url("HTTPS://WWW.POLYU.EDU.HK:443/study/#top"),
            "https://www.polyu.edu.hk/study/",
        )
        self.assertTrue(is_freshness_sensitive("请告诉我最新的截止日期"))

    def test_baseline_profile_is_domain_agnostic(self) -> None:
        profile = build_query_profile("How do I configure Payment Service v4?")
        self.assertEqual(profile.intents, ["procedure"])
        self.assertEqual(profile.constraints, [])


if __name__ == "__main__":
    unittest.main()
