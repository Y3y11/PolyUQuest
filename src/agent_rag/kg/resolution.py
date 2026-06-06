"""Four-stage entity resolution pipeline."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import structlog
from jinja2 import Template
from rapidfuzz import fuzz, process

from agent_rag.config import aliases_config, thresholds_config
from agent_rag.llm.client import LLMClient

logger = structlog.get_logger(__name__)

# Name normalization for the exact-name shortcut. Folds typographic variants
# that the audit on 2026-04-28 found as ~150-250 false splits in run2:
# "BSc(Hons)" vs "BSc (Hons)", "30-Sep" vs "30 Sep", "Dr X" vs "X", "A & B"
# vs "A and B". We intentionally keep singular/plural and content words
# untouched — those are still legitimate distinctions.
_TITLE_PREFIX_RE = re.compile(
    r"^(?:dr|prof|professor|mr|mrs|ms|miss|sir|madam)\.?\s+",
    re.IGNORECASE,
)
_AMP_RE = re.compile(r"\s*&\s*")
_HYPHEN_RE = re.compile(r"-")
_PAREN_SPACE_RE = re.compile(r"\s+\(")
_WS_RE = re.compile(r"\s+")


def _norm_name(name: str) -> str:
    s = name.strip().lower()
    s = _TITLE_PREFIX_RE.sub("", s)
    s = _AMP_RE.sub(" and ", s)
    s = _HYPHEN_RE.sub(" ", s)
    s = _PAREN_SPACE_RE.sub("(", s)  # "bsc (hons)" -> "bsc(hons)"
    s = _WS_RE.sub(" ", s).strip()
    return s

_ER_PROMPT_PATH = Path(__file__).parent.parent / "llm" / "prompts" / "entity_resolution.j2"
_ER_TEMPLATE = Template(_ER_PROMPT_PATH.read_text(encoding="utf-8"))

_thresholds = thresholds_config.get("entity_resolution", {})
EMBEDDING_WEIGHT = _thresholds.get("embedding_weight", 0.7)
FUZZY_WEIGHT = _thresholds.get("fuzzy_weight", 0.3)
AUTO_MERGE_THRESHOLD = _thresholds.get("auto_merge_threshold", 0.95)
REVIEW_THRESHOLD = _thresholds.get("review_threshold", 0.85)
SURNAME_BONUS = _thresholds.get("surname_bonus_weight", 0.1)


def _entity_id(name: str) -> str:
    return hashlib.md5(name.strip().lower().encode()).hexdigest()


def _build_alias_map() -> dict[str, str]:
    """Build a flat variant->canonical mapping from aliases.yaml."""
    alias_map: dict[str, str] = {}
    for section in ("departments", "programmes", "courses"):
        section_data = aliases_config.get(section, {})
        if not isinstance(section_data, dict):
            continue
        for canonical, variants in section_data.items():
            if not isinstance(variants, list):
                continue
            alias_map[canonical.lower()] = canonical
            for v in variants:
                alias_map[v.lower()] = canonical
    return alias_map


_ALIAS_MAP = _build_alias_map()


# ── Stage 1: Alias dictionary lookup ────────────────────────────

def stage1_alias_lookup(name: str) -> str | None:
    """Return canonical name if found in alias dictionary, else None."""
    return _ALIAS_MAP.get(name.strip().lower())


# ── Stage 2: Prompt-time anchoring (handled at extraction time) ─

# Stage 2 is integrated into extractor.py prompt construction.
# See kg/extractor.py: existing_entities parameter.


# ── Stage 3: Embedding + fuzzy string matching ──────────────────

def stage3_score(
    name_a: str,
    name_b: str,
    emb_sim: float,
    entity_type: str = "",
) -> float:
    """Compute combined similarity score between two entity names."""
    fuzzy_sim = fuzz.ratio(name_a.lower(), name_b.lower()) / 100.0

    bonus = 0.0
    if entity_type == "PERSON":
        parts_a = name_a.split()
        parts_b = name_b.split()
        if parts_a and parts_b and parts_a[-1].lower() == parts_b[-1].lower():
            bonus = SURNAME_BONUS

    combined = EMBEDDING_WEIGHT * emb_sim + FUZZY_WEIGHT * fuzzy_sim + bonus
    return min(combined, 1.0)


def stage3_classify(score: float) -> str:
    """Classify a score into: auto_merge, review, or new_entity."""
    if score >= AUTO_MERGE_THRESHOLD:
        return "auto_merge"
    elif score >= REVIEW_THRESHOLD:
        return "review"
    else:
        return "new_entity"


# ── Stage 4: LLM-assisted fine judgment ─────────────────────────

def stage4_llm_judge(
    entity_a: dict[str, str],
    entity_b: dict[str, str],
    llm: LLMClient | None = None,
) -> dict[str, Any]:
    """Ask LLM whether two entities are the same. Returns {same_entity, confidence, canonical_name}."""
    import json_repair

    prompt = _ER_TEMPLATE.render(entity_a=entity_a, entity_b=entity_b)
    if llm is None:
        llm = LLMClient()

    raw = llm.chat(
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    try:
        result = json_repair.loads(raw)
    except Exception:
        result = {"same_entity": False, "confidence": 0.0, "canonical_name": entity_a["name"]}

    return result


# ── Full pipeline ───────────────────────────────────────────────

class EntityResolver:
    """Manages the entity registry and runs the 4-stage pipeline."""

    def __init__(self, max_llm_calls: int | None = None):
        self.entities: dict[str, dict[str, Any]] = {}
        self._entity_embeddings: dict[str, list[float]] = {}
        self.merge_log: list[dict[str, Any]] = []
        self._llm: LLMClient | None = None
        # Cross-page exact-name shortcut. (lower(name), entity_type) → eid.
        # Lets resolve_batch skip stage3 fuzzy/embedding entirely when a
        # mention's name already matches an existing entity's name or alias
        # exactly (after lowercasing). On a typical PolyU build ~60-80% of
        # mentions are exact-name dupes across pages ("Department of
        # Computing" mentioned on hundreds of pages), so this is the
        # single biggest stage4-skip lever.
        self._exact_name_idx: dict[tuple[str, str], str] = {}
        # Hard ceiling on stage4 LLM calls per resolve run. Once reached,
        # all subsequent review-band candidates are treated as new
        # entities. Caps wall-clock for runaway resolve runs (the 11k
        # stage4 / 6h incident on 2026-04-27). None = unbounded.
        self._max_llm_calls = max_llm_calls
        self._stage4_calls = 0
        self._stage4_capped = False

    def _get_llm(self) -> LLMClient:
        if self._llm is None:
            # Stage4 judge runs ~1500-3000 calls per build. Allow per-stage
            # model override via configs/llm.yaml `resolution.model` so we
            # can swap to a flash model (e.g. Ling-flash-2.0 ~5x faster than
            # the default Pro/V3) without affecting other stages.
            from agent_rag.config import llm_config
            res_cfg = llm_config.get("resolution", {})
            model_override = res_cfg.get("model")
            self._llm = LLMClient(model=model_override) if model_override else LLMClient()
        return self._llm

    def resolve(
        self,
        name: str,
        entity_type: str,
        description: str,
        source_block_id: str,
        embedding: list[float] | None = None,
        all_embeddings: dict[str, list[float]] | None = None,
    ) -> tuple[str, str]:
        """Resolve a new entity mention. Returns (entity_id, canonical_name)."""

        canonical = stage1_alias_lookup(name)
        if canonical:
            eid = _entity_id(canonical)
            if eid in self.entities:
                self.entities[eid]["source_block_ids"].append(source_block_id)
                self.merge_log.append({
                    "action": "stage1_alias_merge",
                    "original": name,
                    "canonical": canonical,
                })
                return eid, canonical
            else:
                self.entities[eid] = {
                    "entity_id": eid,
                    "entity_name": canonical,
                    "entity_type": entity_type,
                    "description": description,
                    "aliases": [name] if name != canonical else [],
                    "source_block_ids": [source_block_id],
                }
                return eid, canonical

        best_score = 0.0
        best_eid: str | None = None
        best_canonical: str | None = None

        for eid, ent in self.entities.items():
            if ent["entity_type"] != entity_type:
                continue

            emb_sim = 0.0
            if embedding and all_embeddings and eid in all_embeddings:
                from numpy import dot
                from numpy.linalg import norm
                a, b = embedding, all_embeddings[eid]
                if norm(a) > 0 and norm(b) > 0:
                    emb_sim = float(dot(a, b) / (norm(a) * norm(b)))

            score = stage3_score(name, ent["entity_name"], emb_sim, entity_type)
            if score > best_score:
                best_score = score
                best_eid = eid
                best_canonical = ent["entity_name"]

            for alias in ent.get("aliases", []):
                score_alias = stage3_score(name, alias, emb_sim, entity_type)
                if score_alias > best_score:
                    best_score = score_alias
                    best_eid = eid
                    best_canonical = ent["entity_name"]

        classification = stage3_classify(best_score)

        if classification == "auto_merge" and best_eid:
            self.entities[best_eid]["source_block_ids"].append(source_block_id)
            if name.lower() != best_canonical.lower():  # type: ignore[union-attr]
                self.entities[best_eid]["aliases"].append(name)
            self.merge_log.append({
                "action": "stage3_auto_merge",
                "original": name,
                "canonical": best_canonical,
                "score": best_score,
            })
            return best_eid, best_canonical  # type: ignore[return-value]

        if classification == "review" and best_eid:
            judgment = stage4_llm_judge(
                {"name": name, "type": entity_type, "description": description, "source_context": ""},
                {
                    "name": best_canonical,  # type: ignore[arg-type]
                    "type": self.entities[best_eid]["entity_type"],
                    "description": self.entities[best_eid]["description"],
                    "source_context": "",
                },
                llm=self._get_llm(),
            )
            if judgment.get("same_entity"):
                canon = judgment.get("canonical_name", best_canonical)
                self.entities[best_eid]["source_block_ids"].append(source_block_id)
                if name.lower() != canon.lower():
                    self.entities[best_eid]["aliases"].append(name)
                self.merge_log.append({
                    "action": "stage4_llm_merge",
                    "original": name,
                    "canonical": canon,
                    "confidence": judgment.get("confidence", 0),
                })
                return best_eid, canon
            else:
                self.merge_log.append({
                    "action": "stage4_llm_new",
                    "original": name,
                    "best_candidate": best_canonical,
                    "score": best_score,
                })

        eid = _entity_id(name)
        self.entities[eid] = {
            "entity_id": eid,
            "entity_name": name,
            "entity_type": entity_type,
            "description": description,
            "aliases": [],
            "source_block_ids": [source_block_id],
        }
        return eid, name

    def get_dry_run_report(self) -> dict[str, Any]:
        stage_counts = {}
        for entry in self.merge_log:
            action = entry["action"]
            stage_counts[action] = stage_counts.get(action, 0) + 1
        return {
            "total_entities": len(self.entities),
            "total_merge_operations": len(self.merge_log),
            "stage_breakdown": stage_counts,
            "merge_log": self.merge_log,
        }

    # ── Batch resolution (preferred, O(N·k) instead of O(N²)) ────

    def resolve_batch(
        self,
        mentions: list[dict[str, Any]],
        embeddings: list[list[float]] | None = None,
    ) -> list[tuple[str, str]]:
        """Resolve a batch of entity mentions in one pass.

        Each mention dict: {name, entity_type, description, source_block_id}.
        The embedding at index i (if provided) corresponds to mentions[i].

        Strategy:
          1. Alias dictionary first.
          2. Type-bucketed fuzzy top-k candidates (rapidfuzz.process.extract).
          3. Combine with embedding similarity for final score, apply thresholds.
          4. LLM stage4 only for borderline cases.

        Returns list of (entity_id, canonical_name) aligned with `mentions`.
        """
        import numpy as np

        n = len(mentions)
        if n == 0:
            return []
        if embeddings is not None and len(embeddings) != n:
            raise ValueError("embeddings length must match mentions length")

        results: list[tuple[str, str] | None] = [None] * n

        # Stage 1: alias dictionary — fast path
        pending_idx: list[int] = []
        for i, m in enumerate(mentions):
            canonical = stage1_alias_lookup(m["name"])
            if canonical:
                eid = _entity_id(canonical)
                if eid in self.entities:
                    self.entities[eid]["source_block_ids"].append(m["source_block_id"])
                else:
                    self.entities[eid] = {
                        "entity_id": eid,
                        "entity_name": canonical,
                        "entity_type": m["entity_type"],
                        "description": m["description"],
                        "aliases": [m["name"]] if m["name"] != canonical else [],
                        "source_block_ids": [m["source_block_id"]],
                    }
                    self._exact_name_idx[(_norm_name(canonical), m["entity_type"])] = eid
                self._exact_name_idx[(_norm_name(m["name"]), m["entity_type"])] = eid
                self.merge_log.append({
                    "action": "stage1_alias_merge",
                    "original": m["name"],
                    "canonical": canonical,
                })
                results[i] = (eid, canonical)
            else:
                pending_idx.append(i)

        # Build per-type buckets of existing entities (for blocked fuzzy)
        def _type_bucket(etype: str) -> tuple[list[str], list[str], list[str]]:
            names: list[str] = []
            eids: list[str] = []
            aliases_flat: list[tuple[str, str]] = []
            for eid, ent in self.entities.items():
                if ent["entity_type"] != etype:
                    continue
                names.append(ent["entity_name"])
                eids.append(eid)
                for al in ent.get("aliases", []):
                    aliases_flat.append((al, eid))
            alias_strs = [a[0] for a in aliases_flat]
            return names, eids, alias_strs

        # For each pending mention, within its type bucket, pick best candidate
        for i in pending_idx:
            m = mentions[i]
            name = m["name"]
            etype = m["entity_type"]

            # Exact-name shortcut: normalized (name, type) lookup. Folds
            # typographic variants (whitespace, & vs and, hyphens, title
            # prefixes) per audit findings on 2026-04-28. Skips fuzzy /
            # embedding / stage4 entirely.
            exact_hit = self._exact_name_idx.get((_norm_name(name), etype))
            if exact_hit and exact_hit in self.entities:
                self.entities[exact_hit]["source_block_ids"].append(m["source_block_id"])
                self.merge_log.append({
                    "action": "exact_name_merge",
                    "original": name,
                    "canonical": self.entities[exact_hit]["entity_name"],
                })
                results[i] = (exact_hit, self.entities[exact_hit]["entity_name"])
                continue

            names, eids, alias_strs = _type_bucket(etype)
            best_score = 0.0
            best_eid: str | None = None
            best_canonical: str | None = None

            if names:
                # Top-5 fuzzy candidates in the type bucket
                matches = process.extract(
                    name, names, scorer=fuzz.ratio, limit=5
                )
                for match_name, fuzzy_score, idx in matches:
                    cand_eid = eids[idx]
                    fuzzy_sim = fuzzy_score / 100.0

                    emb_sim = 0.0
                    if embeddings is not None and cand_eid in self._entity_embeddings:
                        a = embeddings[i]
                        b = self._entity_embeddings[cand_eid]
                        na = np.linalg.norm(a)
                        nb = np.linalg.norm(b)
                        if na > 0 and nb > 0:
                            emb_sim = float(np.dot(a, b) / (na * nb))

                    bonus = 0.0
                    if etype == "PERSON":
                        p_a = name.split()
                        p_b = match_name.split()
                        if p_a and p_b and p_a[-1].lower() == p_b[-1].lower():
                            bonus = SURNAME_BONUS
                    score = min(
                        EMBEDDING_WEIGHT * emb_sim + FUZZY_WEIGHT * fuzzy_sim + bonus,
                        1.0,
                    )
                    if score > best_score:
                        best_score = score
                        best_eid = cand_eid
                        best_canonical = match_name

                # Alias matches (blocked, same type bucket)
                if alias_strs:
                    # alias_strs and aliases_flat built concurrently; reconstruct mapping
                    alias_hits = process.extract(
                        name, alias_strs, scorer=fuzz.ratio, limit=5
                    )
                    # Build alias->eid map from the same bucket
                    alias_eid_map: dict[str, str] = {}
                    for eid, ent in self.entities.items():
                        if ent["entity_type"] != etype:
                            continue
                        for al in ent.get("aliases", []):
                            alias_eid_map[al] = eid
                    for alias_name, fuzzy_score, _ in alias_hits:
                        fuzzy_sim = fuzzy_score / 100.0
                        cand_eid = alias_eid_map.get(alias_name)
                        if not cand_eid:
                            continue
                        emb_sim = 0.0
                        if embeddings is not None and cand_eid in self._entity_embeddings:
                            a = embeddings[i]
                            b = self._entity_embeddings[cand_eid]
                            na = np.linalg.norm(a)
                            nb = np.linalg.norm(b)
                            if na > 0 and nb > 0:
                                emb_sim = float(np.dot(a, b) / (na * nb))
                        score = min(
                            EMBEDDING_WEIGHT * emb_sim + FUZZY_WEIGHT * fuzzy_sim,
                            1.0,
                        )
                        if score > best_score:
                            best_score = score
                            best_eid = cand_eid
                            best_canonical = self.entities[cand_eid]["entity_name"]

            classification = stage3_classify(best_score)

            if classification == "auto_merge" and best_eid and best_canonical:
                self.entities[best_eid]["source_block_ids"].append(m["source_block_id"])
                if name.lower() != best_canonical.lower():
                    self.entities[best_eid]["aliases"].append(name)
                    self._exact_name_idx[(_norm_name(name), etype)] = best_eid
                self.merge_log.append({
                    "action": "stage3_auto_merge",
                    "original": name,
                    "canonical": best_canonical,
                    "score": best_score,
                })
                results[i] = (best_eid, best_canonical)
                continue

            if classification == "review" and best_eid and best_canonical:
                # Hard cap on stage4 LLM calls. Once exceeded, all
                # remaining review-band candidates fall through to "new
                # entity" so the build can finish in bounded time.
                if (
                    self._max_llm_calls is not None
                    and self._stage4_calls >= self._max_llm_calls
                ):
                    if not self._stage4_capped:
                        self._stage4_capped = True
                        logger.warning(
                            "stage4_cap_reached",
                            max_llm_calls=self._max_llm_calls,
                            remaining_review_band="downgraded to new_entity",
                        )
                    self.merge_log.append({
                        "action": "stage4_capped_new",
                        "original": name,
                        "best_candidate": best_canonical,
                        "score": best_score,
                    })
                else:
                    self._stage4_calls += 1
                    judgment = stage4_llm_judge(
                        {"name": name, "type": etype, "description": m["description"], "source_context": ""},
                        {
                            "name": best_canonical,
                            "type": self.entities[best_eid]["entity_type"],
                            "description": self.entities[best_eid]["description"],
                            "source_context": "",
                        },
                        llm=self._get_llm(),
                    )
                    if judgment.get("same_entity"):
                        canon = judgment.get("canonical_name", best_canonical)
                        self.entities[best_eid]["source_block_ids"].append(m["source_block_id"])
                        if name.lower() != canon.lower():
                            self.entities[best_eid]["aliases"].append(name)
                            self._exact_name_idx[(_norm_name(name), etype)] = best_eid
                        self.merge_log.append({
                            "action": "stage4_llm_merge",
                            "original": name,
                            "canonical": canon,
                            "confidence": judgment.get("confidence", 0),
                        })
                        results[i] = (best_eid, canon)
                        continue
                    else:
                        self.merge_log.append({
                            "action": "stage4_llm_new",
                            "original": name,
                            "best_candidate": best_canonical,
                            "score": best_score,
                        })

            # Create a new entity
            eid = _entity_id(name)
            self.entities[eid] = {
                "entity_id": eid,
                "entity_name": name,
                "entity_type": etype,
                "description": m["description"],
                "aliases": [],
                "source_block_ids": [m["source_block_id"]],
            }
            self._exact_name_idx[(_norm_name(name), etype)] = eid
            if embeddings is not None:
                self._entity_embeddings[eid] = embeddings[i]
            results[i] = (eid, name)

        return [r if r else ("", "") for r in results]
