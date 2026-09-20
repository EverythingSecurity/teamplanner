from __future__ import annotations

import pytest

from cis_planning.lifecycle import HumanActionRequired, InvalidTransition, LifecycleService
from cis_planning.models import Actor
from cis_planning.ports import TriageUnavailable
from cis_planning.taxonomy import IntakeClassification as IC, PlannerBucket, RequestStatus as S, SecurityDomain as D

from .conftest import ARCHITECTS, make_msg, make_tri


def recipients(mailer):
    return [(to[0], subj) for to, subj, _ in mailer.sent]


# ------------------------------------------------------------------ happy path
def test_new_request_end_to_end(build, repo, planner, mailer):
    pipe, _ = build(make_tri())
    out = pipe.process_message(make_msg())

    assert out.result == "CREATED" and out.request_id == "CIS-2026-00001"
    rec = repo.get_request(out.request_id)
    assert rec.status == S.ASSIGNED
    assert rec.assigned_architect_id == "A001"
    assert "primary" in rec.assignment_reason
    assert rec.planner_task_id == "T1" and rec.planner_url
    assert planner.tasks["T1"]["title"] == "CIS-2026-00001 | Customer Portal | Architecture Review"
    assert planner.tasks["T1"]["bucket"] == PlannerBucket.ASSIGNED
    assert planner.tasks["T1"]["assignee"] == "ada@contoso.com"
    # architect notified, requester acknowledged
    assert recipients(mailer) == [
        ("ada@contoso.com", "CIS Request CIS-2026-00001 assigned to you - Customer Portal"),
        ("priya.nair@contoso.com", "CIS Request CIS-2026-00001 Registered - Customer Portal"),
    ]
    assert [e["NewStatus"] for e in repo.events("status_history", out.request_id)] == ["RECEIVED", "TRIAGED", "ASSIGNED"]
    assert {e["Action"] for e in repo.events("audit", out.request_id)} >= {"create_request", "auto_assign", "create_task"}
    assert len(repo.events("skill", out.request_id)) == 4
    assert rec.classification_confidence == 0.92 and rec.triage["confidence"] == 0.92


def test_reprocessing_same_email_is_a_noop(build, repo, planner, mailer):
    pipe, triage = build(make_tri())
    pipe.process_message(make_msg())
    again = pipe.process_message(make_msg(message_id="different-graph-id"))  # same Internet Message-ID
    assert again.result == "DUPLICATE"
    assert len(planner.tasks) == 1 and len(mailer.sent) == 2 and len(triage.calls) == 1


def test_own_mailbox_messages_are_ignored(build, repo):
    pipe, triage = build(make_tri())
    out = pipe.process_message(make_msg(sender_email="cis-planning@contoso.com"))
    assert out.result == "IGNORED" and not triage.calls and not repo.list_requests()


# ------------------------------------------------------------------ missing info
def test_missing_information_waits_for_requestor(build, repo, mailer):
    pipe, _ = build(make_tri(information_complete=False, missing_information=["Go-live date", "Data classification see http://evil.example/x"]))
    out = pipe.process_message(make_msg())
    rec = repo.get_request(out.request_id)
    assert rec.status == S.WAITING_FOR_REQUESTOR
    subjects = [s for _, s in recipients(mailer)]
    assert f"Information Required - CIS Request {out.request_id}" in subjects
    body = [b for _, s, b in mailer.sent if s.startswith("Information Required")][0]
    assert "- Go-live date" in body and "evil.example" not in body and "[link removed]" in body


def test_requester_reply_moves_to_waiting_for_cis_without_new_task(build, repo, planner, mailer):
    pipe, triage = build(make_tri(information_complete=False, missing_information=["Go-live date"]))
    rid = pipe.process_message(make_msg()).request_id
    reply = make_tri(intake_classification=IC.REQUESTER_CLARIFICATION, is_new_request=False,
                     existing_request_id=rid, information_complete=True, confidence=0.9)
    triage.queue = [reply]
    out = pipe.process_message(make_msg(message_id="m2", internet_message_id="<m2@x>",
                                        subject=f"RE: Information Required - CIS Request {rid}"))
    assert out.result == "UPDATED"
    assert repo.get_request(rid).status == S.WAITING_FOR_CIS
    assert len(planner.tasks) == 1 and len(repo.list_requests()) == 1
    assert planner.updates[-1][1]["bucket"] == PlannerBucket.WAITING_FOR_CIS
    assert any(subj.startswith("Update on CIS Request") for _, subj in recipients(mailer))


# ------------------------------------------------------------------ human triage
def test_low_confidence_is_retained_for_human_triage_and_not_assigned(build, repo, planner, mailer):
    pipe, _ = build(make_tri(confidence=0.4))
    out = pipe.process_message(make_msg())
    rec = repo.get_request(out.request_id)
    assert out.detail == "human triage" and rec.requires_human_triage
    assert rec.assigned_architect_id is None and rec.status == S.TRIAGED
    assert planner.tasks["T1"]["bucket"] == PlannerBucket.TRIAGED and planner.tasks["T1"]["assignee"] is None
    assert any("below threshold" in r for r in rec.human_triage_reasons)
    # requester still gets a registration, but no model-authored clarification request
    assert [s for _, s in recipients(mailer)] == [f"CIS Request {out.request_id} Registered - Customer Portal"]


def test_no_eligible_architect_routes_to_human(build, repo):
    pipe, _ = build(make_tri(required_skills=[D.AI_SECURITY], security_domains=[D.AI_SECURITY]))
    rec = repo.get_request(pipe.process_message(make_msg()).request_id)
    assert rec.requires_human_triage and rec.assigned_architect_id is None
    assert "No enabled architect covers" in rec.human_triage_reasons[0]


def test_no_taxonomy_fit_routes_to_human(build, repo):
    pipe, _ = build(make_tri(required_skills=[], security_domains=[]))
    rec = repo.get_request(pipe.process_message(make_msg()).request_id)
    assert rec.requires_human_triage and any("controlled taxonomy" in r for r in rec.human_triage_reasons)


def test_contradictory_model_output_routes_to_human(build, repo):
    pipe, _ = build(make_tri(is_new_request=False))
    rec = repo.get_request(pipe.process_message(make_msg()).request_id)
    assert rec.requires_human_triage


def test_noise_is_audited_not_tracked(build, repo, planner):
    pipe, _ = build(make_tri(intake_classification=IC.NOISE, is_new_request=False, is_actionable=False, required_skills=[], security_domains=[]))
    out = pipe.process_message(make_msg(subject="Out of office"))
    assert out.result == "IGNORED" and not repo.list_requests() and not planner.tasks
    assert repo.was_processed("<m1@x>")


def test_low_confidence_noise_is_retained_not_dropped(build, repo):
    pipe, _ = build(make_tri(intake_classification=IC.NOISE, is_new_request=False, is_actionable=False, confidence=0.3))
    assert pipe.process_message(make_msg()).result == "CREATED"
    assert repo.list_requests()[0].requires_human_triage


def test_update_type_without_correlation_creates_flagged_request(build, repo):
    pipe, _ = build(make_tri(intake_classification=IC.EXISTING_REQUEST_UPDATE, is_new_request=False, existing_request_id="CIS-2026-99999"))
    rec = repo.get_request(pipe.process_message(make_msg()).request_id)
    assert rec.requires_human_triage and any("could not be correlated" in r for r in rec.human_triage_reasons)


# ------------------------------------------------------------------ correlation
def test_new_thread_reply_with_different_topic_flags_existing_request(build, repo, planner):
    pipe, triage = build(make_tri())
    rid = pipe.process_message(make_msg()).request_id
    triage.queue = [make_tri()]  # model says NEW_REQUEST on a correlated thread
    out = pipe.process_message(make_msg(message_id="m2", internet_message_id="<m2@x>", subject=f"RE: {rid} other thing"))
    assert out.result == "ESCALATED"
    assert repo.get_request(rid).requires_human_triage
    assert len(repo.list_requests()) == 1 and len(planner.tasks) == 1


def test_correlation_by_conversation_id(build, repo):
    pipe, triage = build(make_tri())
    rid = pipe.process_message(make_msg()).request_id
    triage.queue = [make_tri(intake_classification=IC.EXISTING_REQUEST_UPDATE, is_new_request=False, existing_request_id=rid)]
    pipe.process_message(make_msg(message_id="m2", internet_message_id="<m2@x>", subject="quick question"))
    ctx = triage.calls[-1][1]
    assert ctx["matched_by"] == "conversation_id" and ctx["existing_request"]["request_id"] == rid
    assert len(repo.list_requests()) == 1


def test_update_on_completed_request_needs_human(build, repo):
    pipe, triage = build(make_tri())
    rid = pipe.process_message(make_msg()).request_id
    rec = repo.get_request(rid)
    rec.status = S.COMPLETED
    repo.save_request(rec)
    triage.queue = [make_tri(intake_classification=IC.EXISTING_REQUEST_UPDATE, is_new_request=False, existing_request_id=rid)]
    out = pipe.process_message(make_msg(message_id="m2", internet_message_id="<m2@x>"))
    assert out.result == "ESCALATED" and repo.get_request(rid).requires_human_triage


# ------------------------------------------------------------------ injection / identity
def test_requestor_email_from_body_is_not_trusted(build, repo, mailer):
    pipe, _ = build(make_tri(requestor_email="attacker@example.net"))
    rec = repo.get_request(pipe.process_message(make_msg()).request_id)
    assert rec.requestor_email == "priya.nair@contoso.com"
    assert all("attacker@example.net" not in to for to, _, _ in mailer.sent)


def test_requestor_email_accepted_when_participant(build, repo):
    pipe, _ = build(make_tri(requestor_email="owner@contoso.com"))
    rec = repo.get_request(pipe.process_message(make_msg(cc=["Owner@contoso.com"])).request_id)
    assert rec.requestor_email == "owner@contoso.com"


# ------------------------------------------------------------------ failures and recovery
def test_planner_outage_parks_request_and_retry_creates_exactly_one_task(build, repo, planner, mailer):
    pipe, _ = build(make_tri())
    planner.fail_next_create = True
    out = pipe.process_message(make_msg())
    assert out.result == "EXCEPTION" and not out.completed
    rec = repo.get_request(out.request_id)
    assert rec.status == S.AUTOMATION_EXCEPTION and rec.failed_step == "planner" and rec.resume_status == S.ASSIGNED
    assert not mailer.sent and not repo.was_processed("<m1@x>")

    results = pipe.retry_exceptions()
    assert results[0].result == "CREATED"
    rec = repo.get_request(out.request_id)
    assert rec.status == S.ASSIGNED and rec.failed_step is None and rec.last_error is None
    assert len(planner.tasks) == 1 and len(mailer.sent) == 2

    pipe.retry_exceptions()  # nothing left to retry; still no duplicates
    assert len(planner.tasks) == 1 and len(mailer.sent) == 2


def test_mail_failure_is_retried_without_resending_earlier_notifications(build, repo, planner, mailer):
    pipe, _ = build(make_tri())
    calls = {"n": 0}
    real = mailer.send

    def flaky(to, subject, body):
        calls["n"] += 1
        if calls["n"] == 2:  # architect mail ok, requester ack fails
            from cis_planning.ports import IntegrationError
            raise IntegrationError("mail", "boom")
        return real(to, subject, body)

    mailer.send = flaky
    out = pipe.process_message(make_msg())
    assert out.result == "EXCEPTION"
    pipe.retry_exceptions()
    assert len(mailer.sent) == 2 and len({s for _, s, _ in mailer.sent}) == 2
    assert len(planner.tasks) == 1


def test_triage_outage_keeps_the_message_and_recovers_on_redelivery(build, repo, planner):
    pipe, triage = build(TriageUnavailable("model down"), make_tri())
    msg = make_msg()
    first = pipe.process_message(msg)
    assert first.result == "EXCEPTION" and not first.completed
    rid = first.request_id
    rec = repo.get_request(rid)
    assert rec.status == S.AUTOMATION_EXCEPTION and rec.triage is None
    assert pipe.retry_exceptions()[0].result == "RETRY"  # needs the source email

    triage.queue = [make_tri()]
    second = pipe.process_message(msg)  # the unread email is polled again
    assert second.result == "CREATED" and second.request_id == rid
    assert len(repo.list_requests()) == 1 and len(planner.tasks) == 1
    assert repo.get_request(rid).status == S.ASSIGNED


def test_triage_recovery_that_finds_noise_cancels_the_placeholder(build, repo):
    pipe, triage = build(TriageUnavailable("down"))
    msg = make_msg()
    rid = pipe.process_message(msg).request_id
    triage.queue = [make_tri(intake_classification=IC.NOISE, is_new_request=False, is_actionable=False)]
    assert pipe.process_message(msg).result == "IGNORED"
    assert repo.get_request(rid).status == S.CANCELLED


# ------------------------------------------------------------------ continuity + override
def test_continuity_prefers_owner_of_related_project_request(build, repo):
    pipe, triage = build(make_tri())
    first = pipe.process_message(make_msg())
    # Ben ranks lower on skills but nothing here should move the second request off Ada
    second = pipe.process_message(make_msg(message_id="m2", internet_message_id="<m2@x>", conversation_id="c2"))
    assert repo.get_request(second.request_id).assigned_architect_id == "A001"
    assert "continuity" in repo.get_request(second.request_id).assignment_reason


def test_capacity_limit_spills_to_next_architect_or_human(build, repo):
    from cis_planning.models import Architect
    tiny = [Architect(architect_id="A001", name="Ada", email="ada@contoso.com", primary_skills=[D.API_SECURITY, D.CLOUD_SECURITY], maximum_capacity=1)]
    pipe, _ = build(make_tri(), architects=tiny)
    pipe.process_message(make_msg())
    out = pipe.process_message(make_msg(message_id="m2", internet_message_id="<m2@x>", conversation_id="c2"))
    rec = repo.get_request(out.request_id)
    assert rec.requires_human_triage and "spare capacity" in rec.human_triage_reasons[0]


def test_human_override_preserves_history_and_updates_planner(build, repo, planner, mailer):
    pipe, _ = build(make_tri())
    rid = pipe.process_message(make_msg()).request_id
    with pytest.raises(PermissionError):
        pipe.override_assignment(rid, "A002", Actor.automation("x"))
    pipe.override_assignment(rid, "A002", Actor.human("Sam Planner"), "network heavy")
    rec = repo.get_request(rid)
    assert rec.assigned_architect_id == "A002" and "Sam Planner" in rec.assignment_reason
    hist = repo.events("assignment_history", rid)
    assert [(h["PreviousArchitect"], h["NewArchitect"]) for h in hist] == [(None, "A001"), ("A001", "A002")]
    assert hist[1]["Actor"] == "HUMAN:Sam Planner" and hist[1]["Reason"] == "network heavy"
    assert planner.updates[-1][1]["assignee_email"] == "ben@contoso.com"
    assert recipients(mailer)[-1][0] == "ben@contoso.com"


def test_human_override_resolves_a_human_triage_request(build, repo):
    pipe, _ = build(make_tri(confidence=0.3))
    rid = pipe.process_message(make_msg()).request_id
    pipe.override_assignment(rid, "A001", Actor.human("Sam"))
    rec = repo.get_request(rid)
    assert rec.status == S.ASSIGNED and not rec.requires_human_triage


# ------------------------------------------------------------------ lifecycle guardrails
def test_ai_cannot_transition_and_completion_needs_a_human(repo):
    life = LifecycleService(repo)
    from cis_planning.models import RequestRecord, utcnow
    rec = RequestRecord(request_id="CIS-2026-00001", email_message_id="m", email_dedupe_key="k", received_date=utcnow(), status=S.REVIEW)
    repo.save_request(rec)
    with pytest.raises(HumanActionRequired):
        life.transition(rec, S.COMPLETED, Actor.ai("model"))
    with pytest.raises(HumanActionRequired):
        life.transition(rec, S.COMPLETED, Actor.automation("email-parser"))
    life.transition(rec, S.COMPLETED, Actor.human("Sam"), "completion process")
    assert rec.completion_date is not None
    with pytest.raises(InvalidTransition):
        life.transition(rec, S.RECEIVED, Actor.human("Sam"))
    hist = repo.events("status_history", rec.request_id)
    assert hist[-1]["InitiatedBy"] == "HUMAN:Sam" and hist[-1]["PreviousStatus"] == "REVIEW"


def test_request_ids_are_sequential_and_unique(repo):
    ids = [repo.allocate_request_id() for _ in range(3)]
    assert ids == ["CIS-2026-00001", "CIS-2026-00002", "CIS-2026-00003"]
