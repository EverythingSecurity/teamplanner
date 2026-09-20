"""MVP orchestration (agent spec section 24).

email -> actionable? -> correlate/duplicate check -> AI triage -> persist -> deterministic assignment
      -> Planner task -> notify architect -> acknowledge requester -> timestamps + audit

Every side-effecting step is idempotent (checks the record / communication log first), so a failed
run can be retried from the top without duplicate records, tasks or emails.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .assignment import ArchitectAssigner
from .communications import (
    CommType, CommunicationService, architect_assigned, architect_update, missing_information,
    registered, sanitize_items,
)
from .config import Settings
from .correlation import Correlation, correlate
from .lifecycle import LifecycleService
from .models import Actor, EmailMessage, RequestRecord, TriageResult, is_valid_email, utcnow
from .ports import ArchitectDirectory, IntegrationError, Mailer, PlannerClient, TriageClient
from .repository import Repository
from .taxonomy import (
    INACTIVE_STATUSES, STATUS_TO_BUCKET, IntakeClassification as IC, PlannerBucket, RequestStatus as S,
)

log = logging.getLogger("cis_planning")

_UPDATE_TYPES = {IC.EXISTING_REQUEST_UPDATE, IC.REQUESTER_CLARIFICATION, IC.DUPLICATE}
_NO_ACTION_TYPES = {IC.INFORMATIONAL, IC.NON_CIS_OPERATIONAL, IC.NOISE}

AUTOMATION = Actor.automation("pipeline")
AI_TRIAGE = Actor.ai("foundry-triage")


@dataclass
class Outcome:
    result: str  # CREATED | UPDATED | ESCALATED | IGNORED | DUPLICATE | EXCEPTION | RETRY
    request_id: str | None = None
    detail: str = ""

    @property
    def completed(self) -> bool:
        """True when the source email can be marked as handled. RETRY/EXCEPTION keep it in the inbox."""
        return self.result not in {"RETRY", "EXCEPTION"}


def resolve_requestor_email(msg: EmailMessage, tri: TriageResult) -> str:
    """Only trust a model-supplied requester address if it is a real participant of the email."""
    candidate = (tri.requestor_email or "").strip().lower()
    return candidate if candidate in msg.participants else msg.sender_email


def planner_title(record: RequestRecord) -> str:
    def clean(v: str, fallback: str) -> str:
        return " ".join(v.split()) or fallback
    title = " | ".join([
        record.request_id, clean(record.project_name, "Unspecified project"), clean(record.request_type, "Unspecified type"),
    ])
    return title[:255]


def planner_description(record: RequestRecord, architect_name: str | None, source_ref: str) -> str:
    return "\n".join([
        f"Request ID: {record.request_id}",
        f"Requester: {record.requestor_name or 'Unknown'} <{record.requestor_email}>",
        f"Project: {record.project_name or 'Not specified'}",
        f"Request Type: {record.request_type or 'Not specified'}",
        f"Summary: {record.summary}",
        f"Required Skills: {', '.join(s.value for s in record.required_skills) or 'None identified'}",
        f"Security Domains: {', '.join(s.value for s in record.security_domains) or 'None identified'}",
        f"Priority (recommended): {record.priority.value}",
        f"Complexity (recommended): {record.complexity.value}",
        f"Received: {record.received_date.isoformat()}",
        f"Assigned Architect: {architect_name or 'Unassigned'}",
        f"Human triage required: {'YES - ' + '; '.join(record.human_triage_reasons) if record.requires_human_triage else 'no'}",
        f"Source email: {source_ref}",
        f"Tracking record: {record.request_id}",
    ])


class Pipeline:
    def __init__(
        self, *, repo: Repository, triage: TriageClient, planner: PlannerClient, mailer: Mailer,
        architects: ArchitectDirectory, settings: Settings, assigner: ArchitectAssigner | None = None,
        own_addresses: set[str] | None = None, now=utcnow,
    ):
        self._repo = repo
        self._triage = triage
        self._planner = planner
        self._architects = architects
        self._settings = settings
        self._assigner = assigner or ArchitectAssigner(min_coverage=settings.assignment_min_coverage)
        self._own = {a.lower() for a in (own_addresses or set())}
        self._now = now
        self._life = LifecycleService(repo, now)
        self._comms = CommunicationService(mailer, repo, now)

    # ================================================================== entry points
    def process_message(self, msg: EmailMessage) -> Outcome:
        key = msg.dedupe_key
        if msg.sender_email in self._own:
            self._audit("intake", "ignore_own_message", input_ref=key, output={"reason": "sent by CIS mailbox"})
            self._repo.mark_processed(key, "IGNORED", None)
            return Outcome("IGNORED", detail="own message")
        if self._repo.was_processed(key):
            self._audit("correlation", "duplicate_message", input_ref=key, result="SKIPPED")
            return Outcome("DUPLICATE", detail="message already processed")

        pending = self._repo.find_by_email(msg)  # earlier run left a recoverable record
        if pending is not None:
            return self._resume(pending, msg)

        corr = correlate(msg, self._repo)
        try:
            tri = self._triage.triage(msg, corr.context())
        except IntegrationError as exc:
            return self._triage_failed(msg, corr, exc)
        self._audit("triage", "ai_triage", actor=AI_TRIAGE, input_ref=key, output=tri.model_dump(mode="json"),
                    confidence=tri.confidence, request_id=corr.existing.request_id if corr.existing else None)
        return self._route(msg, corr, tri)

    def retry_exceptions(self) -> list[Outcome]:
        """Re-drive requests stuck in AUTOMATION_EXCEPTION whose triage is already stored."""
        results = []
        for record in self._repo.list_requests(status=S.AUTOMATION_EXCEPTION):
            results.append(self._resume(record, None))
        return results

    # ================================================================== routing
    def _general_reasons(self, msg: EmailMessage, tri: TriageResult) -> list[str]:
        thr = self._settings.confidence_threshold
        reasons = []
        if tri.requires_human_triage:
            reasons.append("Triage model requested human review")
        if tri.intake_classification == IC.REQUIRES_HUMAN_TRIAGE:
            reasons.append("Intent ambiguous or not reliably classifiable")
        if tri.confidence < thr:
            reasons.append(f"Confidence {tri.confidence:.2f} below threshold {thr:.2f}")
        reasons.extend(tri.consistency_problems())
        if not is_valid_email(msg.sender_email):
            reasons.append("Requester identity cannot be established (no valid sender address)")
        if not tri.information_complete and not sanitize_items(tri.missing_information):
            reasons.append("Missing information could not be determined")
        return reasons

    def _route(self, msg: EmailMessage, corr: Correlation, tri: TriageResult) -> Outcome:
        reasons = self._general_reasons(msg, tri)
        cls = tri.intake_classification

        if corr.existing is not None:
            return self._handle_update(corr.existing, msg, tri, reasons)

        if corr.ambiguous:
            reasons.append("Correlation ambiguous between " + ", ".join(corr.ambiguous_ids))

        if cls in _UPDATE_TYPES and not corr.ambiguous:
            ref = tri.existing_request_id
            existing = self._repo.get_request(ref) if ref and ref in corr.referenced_ids else None
            if existing is not None:
                return self._handle_update(existing, msg, tri, reasons)
            reasons.append("Message reads as an update but could not be correlated to a known request")

        if cls == IC.NEW_REQUEST and not tri.required_skills:
            reasons.append("No required skill identified from the controlled taxonomy")

        if not reasons and cls in _NO_ACTION_TYPES:
            self._audit("intake", "no_action", input_ref=msg.dedupe_key, output={"classification": cls.value})
            self._repo.mark_processed(msg.dedupe_key, cls.value, None)
            return Outcome("IGNORED", detail=cls.value)

        record = self._new_record(msg, self._repo.allocate_request_id())
        self._apply_triage(record, msg, tri)
        for r in reasons:
            record.flag_for_human(r)
        self._repo.save_request(record)
        self._repo.add_status_history(record.request_id, None, S.RECEIVED, self._now(), "AUTOMATION:pipeline", "request created")
        self._audit("record", "create_request", request_id=record.request_id, input_ref=msg.dedupe_key,
                    output={"human_triage_reasons": record.human_triage_reasons}, confidence=tri.confidence)
        self._advance(record)
        return self._final_outcome(record)

    # ================================================================== update path
    def _handle_update(self, record: RequestRecord, msg: EmailMessage, tri: TriageResult, reasons: list[str]) -> Outcome:
        key = msg.dedupe_key
        self._audit("correlation", "correlated_update", request_id=record.request_id, input_ref=key,
                    output={"sender": msg.sender_email, "classification": tri.intake_classification.value})
        if tri.intake_classification == IC.NOISE and not reasons:
            self._repo.mark_processed(key, "IGNORED", record.request_id)
            return Outcome("IGNORED", record.request_id, "noise on existing thread")

        if tri.intake_classification == IC.NEW_REQUEST:
            reasons.append("Reply on an existing thread appears to raise a different, new request")
        if record.status in INACTIVE_STATUSES:
            reasons.append(f"Message received on a {record.status.value} request; reopening is a human decision")

        try:
            record.modified_date = self._now()
            if reasons:
                for r in reasons:
                    record.flag_for_human(r)
                self._repo.save_request(record)
                self._audit("triage", "escalate_update", request_id=record.request_id, input_ref=key, output={"reasons": reasons})
                self._notify_architect_of_update(record, msg, "A message on this request needs human triage: " + "; ".join(reasons))
                self._repo.mark_processed(key, "ESCALATED", record.request_id)
                return Outcome("ESCALATED", record.request_id, "; ".join(reasons))

            if (
                record.status == S.WAITING_FOR_REQUESTOR
                and tri.intake_classification in (IC.REQUESTER_CLARIFICATION, IC.EXISTING_REQUEST_UPDATE)
                and tri.information_complete
            ):
                self._life.transition(record, S.WAITING_FOR_CIS, AUTOMATION, "requester replied with required information")
                record.information_complete = True
                record.missing_information = []
                self._sync_bucket(record)
            self._repo.save_request(record)
            self._notify_architect_of_update(record, msg, "The requester has sent an update on this request.")
        except IntegrationError as exc:
            self._audit(exc.component, "update_failed", request_id=record.request_id, input_ref=key, result="ERROR", error=str(exc)[:500])
            return Outcome("RETRY", record.request_id, str(exc)[:200])
        self._repo.mark_processed(key, "UPDATED", record.request_id)
        return Outcome("UPDATED", record.request_id)

    def _notify_architect_of_update(self, record: RequestRecord, msg: EmailMessage, note: str) -> None:
        if not record.assigned_architect_id:
            return
        arch = self._architect(record.assigned_architect_id)
        subject, body = architect_update(record, arch.name, note)
        self._comms.send(record, CommType.ARCHITECT_UPDATE, arch.email, subject, body, dedupe_key=msg.dedupe_key)

    # ================================================================== recovery
    def _triage_failed(self, msg: EmailMessage, corr: Correlation, exc: IntegrationError) -> Outcome:
        self._audit(exc.component, "ai_triage", actor=AI_TRIAGE, input_ref=msg.dedupe_key, result="ERROR", error=str(exc)[:500])
        if corr.existing is not None:
            return Outcome("RETRY", corr.existing.request_id, "triage unavailable; message left for retry")
        # Never drop a message: keep a recoverable record so an operator or the next run can finish it.
        record = self._new_record(msg, self._repo.allocate_request_id())
        record.flag_for_human("AI triage failed; awaiting retry")
        self._repo.save_request(record)
        self._repo.add_status_history(record.request_id, None, S.RECEIVED, self._now(), "AUTOMATION:pipeline", "request created (triage failed)")
        self._enter_exception(record, "triage", exc)
        return Outcome("EXCEPTION", record.request_id, str(exc)[:200])

    def _resume(self, record: RequestRecord, msg: EmailMessage | None) -> Outcome:
        if record.status == S.AUTOMATION_EXCEPTION and record.triage is None and msg is None:
            return Outcome("RETRY", record.request_id, "source email needed to retry triage")
        if record.status == S.AUTOMATION_EXCEPTION:
            self._life.transition(record, record.resume_status or S.RECEIVED, AUTOMATION, "retry after automation exception")
        if record.triage is None and msg is not None:
            try:
                tri = self._triage.triage(msg, {"matched_by": None, "existing_request": None, "referenced_request_ids": []})
            except IntegrationError as exc:
                self._audit(exc.component, "ai_triage_retry", actor=AI_TRIAGE, request_id=record.request_id, result="ERROR", error=str(exc)[:500])
                self._enter_exception(record, "triage", exc)
                return Outcome("EXCEPTION", record.request_id, str(exc)[:200])
            self._audit("triage", "ai_triage_retry", actor=AI_TRIAGE, request_id=record.request_id,
                        output=tri.model_dump(mode="json"), confidence=tri.confidence)
            reasons = self._general_reasons(msg, tri)
            if not reasons and tri.intake_classification in _NO_ACTION_TYPES:
                self._life.transition(record, S.CANCELLED, AUTOMATION, f"retry triage: {tri.intake_classification.value}")
                self._repo.mark_processed(record.email_dedupe_key, "IGNORED", record.request_id)
                return Outcome("IGNORED", record.request_id, "cancelled after retry triage")
            record.human_triage_reasons = [r for r in record.human_triage_reasons if r != "AI triage failed; awaiting retry"]
            record.requires_human_triage = bool(record.human_triage_reasons)
            self._apply_triage(record, msg, tri)
            if tri.intake_classification == IC.NEW_REQUEST and not tri.required_skills:
                reasons.append("No required skill identified from the controlled taxonomy")
            for r in reasons:
                record.flag_for_human(r)
            self._repo.save_request(record)
        self._advance(record)
        return self._final_outcome(record)

    def _enter_exception(self, record: RequestRecord, step: str, exc: Exception) -> None:
        if record.status != S.AUTOMATION_EXCEPTION:
            self._life.transition(record, S.AUTOMATION_EXCEPTION, AUTOMATION, f"failed at {step}")
        record.failed_step = step
        record.last_error = f"{type(exc).__name__}: {str(exc)[:400]}"
        self._repo.save_request(record)
        self._audit(getattr(exc, "component", step), f"step_failed:{step}", request_id=record.request_id,
                    result="ERROR", error=record.last_error)
        log.warning("request %s moved to AUTOMATION_EXCEPTION at %s", record.request_id, step)

    def _final_outcome(self, record: RequestRecord) -> Outcome:
        if record.status == S.AUTOMATION_EXCEPTION:
            return Outcome("EXCEPTION", record.request_id, record.last_error or "")
        return Outcome("CREATED", record.request_id, "human triage" if record.requires_human_triage else "")

    # ================================================================== record building
    def _new_record(self, msg: EmailMessage, request_id: str) -> RequestRecord:
        return RequestRecord(
            request_id=request_id, email_message_id=msg.message_id, email_dedupe_key=msg.dedupe_key,
            conversation_id=msg.conversation_id, received_date=msg.received_at,
            requestor_name=msg.sender_name, requestor_email=msg.sender_email,
            created_date=self._now(), modified_date=self._now(),
        )

    def _apply_triage(self, record: RequestRecord, msg: EmailMessage, tri: TriageResult) -> None:
        record.requestor_email = resolve_requestor_email(msg, tri)
        record.requestor_name = tri.requestor_name or msg.sender_name
        record.business_unit = tri.business_unit
        record.project_name = tri.project_name
        record.request_type = tri.request_type
        record.summary = tri.short_summary
        record.detailed_summary = tri.detailed_summary
        record.priority, record.complexity, record.risk_indicator = tri.priority, tri.complexity, tri.risk_indicator
        record.required_skills = list(dict.fromkeys(tri.required_skills))
        record.security_domains = list(dict.fromkeys(tri.security_domains))
        record.information_complete = tri.information_complete
        record.missing_information = sanitize_items(tri.missing_information)
        record.classification_confidence = tri.confidence
        record.triage = tri.model_dump(mode="json")
        record.modified_date = self._now()

    # ================================================================== forward progress
    def _advance(self, record: RequestRecord) -> None:
        """Idempotent: runs every remaining step for the record; on failure parks it in AUTOMATION_EXCEPTION."""
        step = "triaged"
        try:
            if record.status == S.RECEIVED:
                self._life.transition(record, S.TRIAGED, AUTOMATION, "AI triage stored")
                self._repo.add_skills(record.request_id, self._skill_rows(record))
            step = "assignment"
            self._assign_if_needed(record)
            step = "requester_wait_state"
            self._wait_for_requestor_if_needed(record)
            step = "planner"
            self._ensure_planner_task(record)
            step = "notify_architect"
            self._notify_architect(record)
            step = "acknowledge_requester"
            self._acknowledge_requester(record)
        except Exception as exc:  # integration failures and unexpected errors are both recoverable
            self._enter_exception(record, step, exc)
            return
        self._repo.mark_processed(record.email_dedupe_key, "CREATED", record.request_id)

    @staticmethod
    def _skill_rows(record: RequestRecord) -> list[dict]:
        conf = record.classification_confidence
        rows = [{"Skill": s.value, "SkillType": "REQUIRED", "Confidence": conf} for s in record.required_skills]
        rows += [{"Skill": s.value, "SkillType": "DOMAIN", "Confidence": conf} for s in record.security_domains]
        return rows

    def _architect(self, architect_id: str):
        for a in self._architects.list_architects():
            if a.architect_id == architect_id:
                return a
        raise IntegrationError("assignment", f"architect {architect_id} not found in capability matrix")

    def _assign_if_needed(self, record: RequestRecord) -> None:
        if record.assigned_architect_id or record.requires_human_triage:
            return
        related = [
            r.assigned_architect_id for r in self._repo.list_requests(active_only=True)
            if r.request_id != record.request_id and r.assigned_architect_id
            and record.project_name.strip() and r.project_name.strip().lower() == record.project_name.strip().lower()
        ]
        decision = self._assigner.assign(
            record.required_skills, self._architects.list_architects(), self._repo.architect_loads(),
            related_owner_ids=related,
        )
        if decision.architect is None:
            record.flag_for_human(decision.reason)
            self._repo.save_request(record)
            self._audit("assignment", "no_eligible_architect", request_id=record.request_id, output={"reason": decision.reason}, result="HUMAN_TRIAGE")
            return
        now = self._now()
        record.assigned_architect_id = decision.architect.architect_id
        record.assignment_date = now
        record.assignment_reason = decision.reason
        self._life.transition(record, S.ASSIGNED, AUTOMATION, decision.reason)
        self._repo.add_assignment_history({
            "RequestID": record.request_id, "PreviousArchitect": None, "NewArchitect": decision.architect.architect_id,
            "Timestamp": now.isoformat(), "Actor": "AUTOMATION:pipeline", "Reason": decision.reason, "Rule": decision.rule,
        })
        self._audit("assignment", "auto_assign", request_id=record.request_id,
                    output={"architect_id": decision.architect.architect_id, "rule": decision.rule, "reason": decision.reason})

    def _wait_for_requestor_if_needed(self, record: RequestRecord) -> None:
        if (
            not record.information_complete and record.missing_information and not record.requires_human_triage
            and record.status in (S.TRIAGED, S.ASSIGNED)
        ):
            self._life.transition(record, S.WAITING_FOR_REQUESTOR, AUTOMATION, "mandatory information missing")

    def _ensure_planner_task(self, record: RequestRecord) -> None:
        if record.planner_task_id:
            return
        architect = self._architect(record.assigned_architect_id) if record.assigned_architect_id else None
        ref = self._planner.find_task(record.request_id)
        if ref is None:
            ref = self._planner.create_task(
                title=planner_title(record),
                description=planner_description(record, architect.name if architect else None, f"email {record.email_message_id}"),
                bucket=STATUS_TO_BUCKET.get(record.status) or PlannerBucket.NEW,
                priority=record.priority,
                assignee_email=architect.email if architect else None,
            )
        record.planner_task_id, record.planner_url = ref.task_id, ref.url
        self._repo.save_request(record)
        self._audit("planner", "create_task", request_id=record.request_id, output={"task_id": ref.task_id})

    def _notify_architect(self, record: RequestRecord) -> None:
        if not record.assigned_architect_id:
            return
        arch = self._architect(record.assigned_architect_id)
        subject, body = architect_assigned(record, arch.name)
        if self._comms.send(record, CommType.ARCHITECT_ASSIGNED, arch.email, subject, body, dedupe_key=arch.architect_id):
            self._audit("communications", "notify_architect", request_id=record.request_id, output={"architect_id": arch.architect_id})

    def _acknowledge_requester(self, record: RequestRecord) -> None:
        to = record.requestor_email
        subject, body = registered(record)
        if self._comms.send(record, CommType.REQUEST_REGISTERED, to, subject, body):
            self._audit("communications", "acknowledge_requester", request_id=record.request_id, output={"type": "REQUEST_REGISTERED"})
        # Only ask the requester for information when the model was confident enough to be trusted.
        if not record.information_complete and record.missing_information and not record.requires_human_triage:
            subject, body = missing_information(record, record.missing_information)
            if self._comms.send(record, CommType.MISSING_INFORMATION, to, subject, body, dedupe_key="initial"):
                self._audit("communications", "request_missing_information", request_id=record.request_id,
                            output={"items": len(record.missing_information)})

    def _sync_bucket(self, record: RequestRecord) -> None:
        """Best-effort Planner bucket sync after a status change; failures are audited, not fatal."""
        bucket = STATUS_TO_BUCKET.get(record.status)
        if not record.planner_task_id or bucket is None:
            return
        try:
            self._planner.update_task(record.planner_task_id, bucket=bucket)
        except IntegrationError as exc:
            self._audit(exc.component, "sync_bucket", request_id=record.request_id, result="ERROR", error=str(exc)[:500])

    # ================================================================== human override
    def override_assignment(self, request_id: str, new_architect_id: str, actor: Actor, reason: str | None = None) -> RequestRecord:
        """Authorized CIS Planning personnel change the assignee. History is append-only."""
        if actor.kind.value != "HUMAN":
            raise PermissionError("assignment override requires a human actor")
        record = self._repo.get_request(request_id)
        if record is None:
            raise KeyError(request_id)
        new = self._architect(new_architect_id)
        if not new.enabled:
            raise ValueError(f"architect {new_architect_id} is disabled")
        previous = record.assigned_architect_id
        if previous == new_architect_id:
            return record
        now = self._now()
        record.assigned_architect_id = new_architect_id
        record.assignment_date = now
        record.assignment_reason = f"Manual override by {actor.name}" + (f": {reason}" if reason else "")
        record.requires_human_triage = False
        record.human_triage_reasons = []
        self._repo.save_request(record)
        if record.status == S.TRIAGED:
            self._life.transition(record, S.ASSIGNED, actor, "assigned by human")
        self._repo.add_assignment_history({
            "RequestID": request_id, "PreviousArchitect": previous, "NewArchitect": new_architect_id,
            "Timestamp": now.isoformat(), "Actor": f"HUMAN:{actor.name}", "Reason": reason, "Rule": "manual_override",
        })
        self._audit("assignment", "manual_override", actor=actor, request_id=request_id,
                    output={"previous": previous, "new": new_architect_id, "reason": reason})
        try:
            if record.planner_task_id:
                self._planner.update_task(record.planner_task_id, assignee_email=new.email,
                                          bucket=STATUS_TO_BUCKET.get(record.status))
            subject, body = architect_assigned(record, new.name)
            self._comms.send(record, CommType.ARCHITECT_ASSIGNED, new.email, subject, body, dedupe_key=new.architect_id)
        except IntegrationError as exc:
            # The system of record is already updated; surface the sync failure without losing the override.
            self._audit(exc.component, "override_sync", actor=actor, request_id=request_id, result="ERROR", error=str(exc)[:500])
            raise
        return record

    # ================================================================== audit
    def _audit(self, component: str, action: str, *, request_id: str | None = None, actor: Actor = AUTOMATION,
               input_ref: str | None = None, output=None, confidence: float | None = None,
               result: str = "OK", error: str | None = None) -> None:
        self._repo.add_audit({
            "RequestID": request_id, "Timestamp": self._now().isoformat(), "Component": component,
            "Action": action, "AIorHuman": actor.kind.value, "InputReference": input_ref, "Output": output,
            "Confidence": confidence, "Result": result, "Error": error,
        })
