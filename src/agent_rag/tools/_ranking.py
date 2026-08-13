"""Small deterministic ranking helpers used before an LLM sees candidates."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from agent_rag.config import crawl_config
from agent_rag.tools.schemas import QueryProfile


def _contains_term(text: str, term: str) -> bool:
    """Match CJK/phrases directly and short Latin slugs on word boundaries."""
    lowered = text.lower()
    normalized = term.lower()
    if re.fullmatch(r"[a-z0-9]+", normalized) and len(normalized) <= 4:
        return (
            re.search(
                rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])",
                lowered,
            )
            is not None
        )
    return normalized in lowered


def _constraint_terms(constraint: object) -> list[str]:
    return [
        value
        for value in [
            getattr(constraint, "label", ""),
            getattr(constraint, "value", ""),
            *getattr(constraint, "aliases", []),
        ]
        if value
    ]


def _requirement_evidence_terms(requirement: object) -> list[str]:
    return [
        value
        for value in [
            *getattr(requirement, "evidence_cues", []),
            *getattr(requirement, "cues", []),
        ]
        if value
    ]


def requirement_match_score(requirement: object, text: str) -> float:
    """Best portable cue match, allowing partial multi-token page labels.

    Navigation anchors are often shorter than the query contract: an anchor
    may say ``PhD and MPhil`` while the cue says ``PhD application``. Exact
    single-word cues still require an exact match; multi-token cues may match
    when at least half of their lexical units are present.
    """
    scores: list[float] = []
    for cue in _requirement_evidence_terms(requirement):
        if _contains_term(text, cue):
            scores.append(1.0)
            continue
        cue_terms = terms(cue)
        if len(cue_terms) >= 2:
            scores.append(lexical_score(cue, text))
    evidence_score = max(scores, default=0.0)
    target_cues = getattr(requirement, "target_cues", [])
    if not target_cues:
        return evidence_score
    target_supported = any(_contains_term(text, cue) for cue in target_cues)
    return evidence_score if target_supported else 0.0


def requirement_coverage(profile: QueryProfile | None, text: str) -> float:
    """Measure which answer claims a navigation target appears able to support."""
    if profile is None:
        return 0.0
    required = [
        item
        for item in profile.required_claims
        if item.required and (item.evidence_cues or item.cues)
    ]
    if not required:
        return 0.0
    matched = sum(
        1
        for item in required
        if requirement_match_score(item, text) >= 0.5
    )
    return matched / len(required)


def requirement_status(
    profile: QueryProfile | None,
    text: str,
) -> tuple[list[str], list[str]]:
    """Return supported and missing claim labels for auditable evidence gating."""
    if profile is None:
        return [], []
    supported: list[str] = []
    missing: list[str] = []
    for item in profile.required_claims:
        if not item.required or not (item.evidence_cues or item.cues):
            continue
        if requirement_match_score(item, text) >= 0.5:
            supported.append(item.claim)
        else:
            missing.append(item.claim)
    return supported, missing


def constraint_coverage(
    profile: QueryProfile | None,
    text: str,
    kinds: set[str] | None = None,
) -> float:
    """Measure portable query-constraint coverage in candidate/evidence text."""
    if profile is None:
        return 1.0
    required = [
        item
        for item in profile.constraints
        if item.required and (kinds is None or item.kind in kinds)
    ]
    if not required:
        return 0.0
    matched = sum(
        1
        for item in required
        if any(_contains_term(text, term) for term in _constraint_terms(item))
    )
    return matched / len(required)


def constraints_supported(
    profile: QueryProfile | None,
    text: str,
    kinds: set[str] | None = None,
) -> bool:
    if profile is None:
        return True
    applicable = [
        item
        for item in profile.constraints
        if item.required and (kinds is None or item.kind in kinds)
    ]
    if not applicable:
        return True
    if constraint_coverage(profile, text, kinds) < 1.0:
        return False
    for item in applicable:
        if not any(_contains_term(text, term) for term in _constraint_terms(item)):
            return False
        if item.kind == "qualifier" and any(
            _contains_term(text, excluded) for excluded in item.excludes
        ):
            return False
    return True


def expanded_query(query: str, profile: QueryProfile | None) -> str:
    """Add resolved aliases for multilingual dense/BM25 recall."""
    if profile is None:
        return query
    aliases = [
        alias
        for constraint in profile.constraints
        for alias in [constraint.label, constraint.value, *constraint.aliases]
        if alias.casefold() not in query.casefold()
    ]
    return " ".join([query, *aliases[:20]])


def terms(text: str) -> set[str]:
    lowered = text.lower()
    latin = re.findall(r"[a-z0-9]{2,}", lowered)
    cjk = re.findall(r"[\u3400-\u9fff]", text)
    return set(latin + cjk)


def lexical_score(query: str, text: str) -> float:
    wanted = terms(query)
    if not wanted:
        return 0.0
    found = terms(text)
    return len(wanted & found) / len(wanted)


def frontier_score(
    query: str,
    url: str,
    anchor_text: str = "",
    profile: QueryProfile | None = None,
) -> float:
    """Rank trusted navigation candidates with auditable intent hints.

    URL slugs and anchor labels carry strong structural signals that a raw
    multilingual token-overlap score misses. Bonuses are bounded and only
    affect which trusted page is fetched next; they never establish evidence.
    """
    target = f"{anchor_text} {url}".lower()
    score = lexical_score(query, target)

    if profile is not None:
        score += 0.6 * constraint_coverage(profile, target)
        # The next page should address what the answer is still missing, not
        # merely repeat the right entity or qualifier. The orchestrator narrows
        # required_claims to the currently unsupported subset each round.
        score += 0.5 * requirement_coverage(profile, target)
        excluded = {
            excluded
            for constraint in profile.constraints
            if constraint.required
            for excluded in constraint.excludes
            if _contains_term(target, excluded)
        }
        score -= min(0.6, 0.2 * len(excluded))

    intents = set(profile.intents) if profile is not None else set()
    if "procedure" in intents and any(
        term in target
        for term in (
            "procedure", "how-to", "howto", "guide", "documentation",
            "manual", "setup", "requirements", "reference", "help", "faq",
        )
    ):
        score += 0.25
    if "procedure" in intents and any(
        term in target
        for term in ("news-and-events", "/news/", "/events/", "award", "/blog/")
    ):
        score -= 0.35
    return round(max(0.0, min(1.0, score)), 6)


def normalize_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower().rstrip(".")
    port = parsed.port
    netloc = hostname
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{hostname}:{port}"
    path = parsed.path or "/"
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def trusted_url(url: str, whitelist: list[str] | None = None) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            return False
        if parsed.port not in {None, 80, 443}:
            return False
    except ValueError:
        return False

    allowed = whitelist or list(crawl_config.get("domain_whitelist", []))
    for pattern in allowed:
        pattern = pattern.lower().rstrip(".")
        if pattern.startswith("*."):
            suffix = pattern[2:]
            if host == suffix or host.endswith(f".{suffix}"):
                return True
        elif host == pattern:
            return True
    return False
