from __future__ import annotations

from cis_planning.assignment import ArchitectAssigner
from cis_planning.models import Architect, ArchitectLoad
from cis_planning.taxonomy import Availability, SecurityDomain as D


def arch(id_, primary=(), secondary=(), cap=5, avail=Availability.AVAILABLE, enabled=True):
    return Architect(architect_id=id_, name=id_, email=f"{id_}@x.com", primary_skills=list(primary),
                     secondary_skills=list(secondary), maximum_capacity=cap, availability=avail, enabled=enabled)


def test_prefers_primary_over_secondary_coverage():
    a, b = arch("A", primary=[D.API_SECURITY]), arch("B", secondary=[D.API_SECURITY])
    d = ArchitectAssigner().assign([D.API_SECURITY], [b, a], {})
    assert d.architect.architect_id == "A" and d.rule == "skill_match"


def test_workload_breaks_ties_and_id_makes_it_deterministic():
    a, b, c = (arch(i, primary=[D.API_SECURITY]) for i in "ABC")
    loads = {"A": ArchitectLoad(active_request_count=3), "B": ArchitectLoad(active_request_count=1), "C": ArchitectLoad(active_request_count=1)}
    assert ArchitectAssigner().assign([D.API_SECURITY], [a, b, c], loads).architect.architect_id == "B"
    assert ArchitectAssigner().assign([D.API_SECURITY], [c, b, a], loads).architect.architect_id == "B"


def test_all_required_skills_must_be_covered_by_default():
    a = arch("A", primary=[D.API_SECURITY])
    d = ArchitectAssigner().assign([D.API_SECURITY, D.AI_SECURITY], [a], {})
    assert d.architect is None and "No enabled architect covers" in d.reason
    assert ArchitectAssigner(min_coverage=0.5).assign([D.API_SECURITY, D.AI_SECURITY], [a], {}).architect.architect_id == "A"


def test_disabled_unavailable_and_full_architects_are_excluded():
    pool = [
        arch("dis", primary=[D.API_SECURITY], enabled=False),
        arch("off", primary=[D.API_SECURITY], avail=Availability.UNAVAILABLE),
        arch("full", primary=[D.API_SECURITY], cap=2),
    ]
    d = ArchitectAssigner().assign([D.API_SECURITY], pool, {"full": ArchitectLoad(active_request_count=2)})
    assert d.architect is None and "spare capacity" in d.reason


def test_limited_availability_is_still_eligible():
    d = ArchitectAssigner().assign([D.API_SECURITY], [arch("A", primary=[D.API_SECURITY], avail=Availability.LIMITED)], {})
    assert d.architect.architect_id == "A"


def test_continuity_beats_lower_workload_but_only_among_eligible():
    a, b = arch("A", primary=[D.API_SECURITY]), arch("B", primary=[D.API_SECURITY])
    loads = {"A": ArchitectLoad(active_request_count=4)}
    d = ArchitectAssigner().assign([D.API_SECURITY], [a, b], loads, related_owner_ids=["A"])
    assert d.architect.architect_id == "A" and d.rule == "continuity"
    loads = {"A": ArchitectLoad(active_request_count=5)}  # A is at capacity, so continuity cannot apply
    assert ArchitectAssigner().assign([D.API_SECURITY], [a, b], loads, related_owner_ids=["A"]).architect.architect_id == "B"


def test_explicit_rule_applies_only_to_eligible_architect():
    a, b = arch("A", primary=[D.API_SECURITY]), arch("B", primary=[D.API_SECURITY])
    rules = {D.API_SECURITY: "B"}
    assert ArchitectAssigner(explicit_rules=rules).assign([D.API_SECURITY], [a, b], {}).rule == "explicit_rule"
    b.enabled = False
    assert ArchitectAssigner(explicit_rules=rules).assign([D.API_SECURITY], [a, b], {}).architect.architect_id == "A"


def test_no_skills_means_no_assignment():
    assert ArchitectAssigner().assign([], [arch("A", primary=[D.API_SECURITY])], {}).architect is None
