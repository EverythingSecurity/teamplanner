"""Deterministic architect assignment (agent spec section 10).

Inputs are the required skills, the Architect Capability Matrix and current workload. There is no
model call and no subjective scoring: the same inputs always give the same architect and the same
AssignmentReason.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Architect, ArchitectLoad
from .taxonomy import Availability, SecurityDomain


@dataclass
class AssignmentDecision:
    architect: Architect | None
    reason: str
    rule: str  # explicit_rule | continuity | skill_match | none


@dataclass
class _Candidate:
    architect: Architect
    primary: int
    secondary: int
    load: ArchitectLoad

    @property
    def coverage(self) -> int:
        return self.primary + self.secondary

    @property
    def utilisation(self) -> float:
        cap = self.architect.maximum_capacity
        return self.load.active_request_count / cap if cap else 1.0


class ArchitectAssigner:
    def __init__(
        self,
        *,
        min_coverage: float = 1.0,
        explicit_rules: dict[SecurityDomain, str] | None = None,
    ):
        """
        min_coverage   share of required skills one architect must cover (1.0 = all). Requests that no
                       single architect covers go to human triage rather than a partial match.
        explicit_rules skill -> architect_id routing rules; applied only if that architect is eligible.
        """
        self._min_coverage = min_coverage
        self._rules = explicit_rules or {}

    def assign(
        self,
        required_skills: list[SecurityDomain],
        architects: list[Architect],
        loads: dict[str, ArchitectLoad],
        *,
        related_owner_ids: list[str] | None = None,
    ) -> AssignmentDecision:
        skills = list(dict.fromkeys(required_skills))
        if not skills:
            return AssignmentDecision(None, "No required skills identified.", "none")

        scored = [self._score(a, skills, loads.get(a.architect_id, ArchitectLoad())) for a in architects]
        skill_ok = [c for c in scored if c.architect.enabled and c.coverage / len(skills) >= self._min_coverage]
        eligible = [c for c in skill_ok if self._has_capacity(c)]

        if not eligible:
            if skill_ok:
                return AssignmentDecision(
                    None,
                    f"{len(skill_ok)} architect(s) cover the required skills but none is available with spare capacity.",
                    "none",
                )
            return AssignmentDecision(
                None,
                "No enabled architect covers the required skills: " + ", ".join(s.value for s in skills) + ".",
                "none",
            )

        by_id = {c.architect.architect_id: c for c in eligible}

        for skill in skills:
            target = self._rules.get(skill)
            if target in by_id:
                c = by_id[target]
                return AssignmentDecision(c.architect, self._reason(c, skills, f"explicit routing rule for {skill.value}"), "explicit_rule")

        owners = [by_id[i] for i in (related_owner_ids or []) if i in by_id]
        pool, rule, why = eligible, "skill_match", "best skill match"
        if owners:
            pool, rule, why = owners, "continuity", "continuity: owns related request(s) for this project"

        best = min(
            pool,
            key=lambda c: (-c.primary, -c.secondary, c.utilisation, c.load.active_project_count, c.architect.architect_id),
        )
        return AssignmentDecision(best.architect, self._reason(best, skills, why, len(eligible)), rule)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _score(a: Architect, skills: list[SecurityDomain], load: ArchitectLoad) -> _Candidate:
        primary = sum(1 for s in skills if s in a.primary_skills)
        secondary = sum(1 for s in skills if s not in a.primary_skills and s in a.secondary_skills)
        return _Candidate(a, primary, secondary, load)

    @staticmethod
    def _has_capacity(c: _Candidate) -> bool:
        a = c.architect
        return a.availability != Availability.UNAVAILABLE and c.load.active_request_count < a.maximum_capacity

    @staticmethod
    def _reason(c: _Candidate, skills: list[SecurityDomain], why: str, eligible_count: int | None = None) -> str:
        a = c.architect
        parts = [
            f"Selected by {why}.",
            f"Covers {c.coverage}/{len(skills)} required skills ({c.primary} primary, {c.secondary} secondary).",
            f"Workload {c.load.active_request_count}/{a.maximum_capacity} active requests, "
            f"{c.load.active_project_count} active project(s), availability {a.availability.value}.",
        ]
        if eligible_count is not None:
            parts.append(f"{eligible_count} eligible architect(s) considered.")
        return " ".join(parts)
