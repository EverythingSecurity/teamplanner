"""Request lifecycle: the only place status changes. Deterministic; AI output can never drive a transition."""

from __future__ import annotations

from .models import Actor, RequestRecord, utcnow
from .repository import Repository
from .taxonomy import ActorKind, RequestStatus

S = RequestStatus

_RESUMABLE = {
    S.RECEIVED, S.TRIAGED, S.ASSIGNED, S.IN_ASSESSMENT, S.WAITING_FOR_REQUESTOR,
    S.WAITING_FOR_CIS, S.REVIEW, S.ON_HOLD,
}

ALLOWED: dict[RequestStatus, frozenset[RequestStatus]] = {
    S.RECEIVED: frozenset({S.TRIAGED, S.CANCELLED, S.AUTOMATION_EXCEPTION}),
    S.TRIAGED: frozenset({S.ASSIGNED, S.WAITING_FOR_REQUESTOR, S.ON_HOLD, S.CANCELLED, S.AUTOMATION_EXCEPTION}),
    S.ASSIGNED: frozenset({S.IN_ASSESSMENT, S.WAITING_FOR_REQUESTOR, S.WAITING_FOR_CIS, S.ON_HOLD, S.CANCELLED, S.AUTOMATION_EXCEPTION}),
    S.IN_ASSESSMENT: frozenset({S.WAITING_FOR_REQUESTOR, S.WAITING_FOR_CIS, S.REVIEW, S.ON_HOLD, S.CANCELLED, S.AUTOMATION_EXCEPTION}),
    S.WAITING_FOR_REQUESTOR: frozenset({S.WAITING_FOR_CIS, S.ASSIGNED, S.IN_ASSESSMENT, S.TRIAGED, S.ON_HOLD, S.CANCELLED, S.AUTOMATION_EXCEPTION}),
    S.WAITING_FOR_CIS: frozenset({S.IN_ASSESSMENT, S.ASSIGNED, S.WAITING_FOR_REQUESTOR, S.REVIEW, S.ON_HOLD, S.CANCELLED, S.AUTOMATION_EXCEPTION}),
    S.REVIEW: frozenset({S.COMPLETED, S.IN_ASSESSMENT, S.WAITING_FOR_REQUESTOR, S.ON_HOLD, S.CANCELLED, S.AUTOMATION_EXCEPTION}),
    S.COMPLETED: frozenset({S.CLOSED, S.IN_ASSESSMENT, S.AUTOMATION_EXCEPTION}),
    S.ON_HOLD: frozenset({S.TRIAGED, S.ASSIGNED, S.IN_ASSESSMENT, S.WAITING_FOR_REQUESTOR, S.WAITING_FOR_CIS, S.CANCELLED, S.AUTOMATION_EXCEPTION}),
    S.AUTOMATION_EXCEPTION: frozenset(_RESUMABLE),
    S.CLOSED: frozenset(),
    S.CANCELLED: frozenset(),
}

# Completion is a human decision made through the defined completion process. An email that
# reads as positive or final must never be able to complete a request (agent spec 18, 25.6).
HUMAN_ONLY = frozenset({S.COMPLETED, S.CLOSED})


class InvalidTransition(Exception):
    pass


class HumanActionRequired(Exception):
    pass


class LifecycleService:
    def __init__(self, repo: Repository, now=utcnow):
        self._repo = repo
        self._now = now

    def transition(
        self,
        record: RequestRecord,
        new_status: RequestStatus,
        actor: Actor,
        reason: str | None = None,
    ) -> bool:
        """Apply a transition, persist the record and append status history. Returns False if no change."""
        prev = record.status
        if new_status == prev:
            return False
        if actor.kind == ActorKind.AI:
            raise HumanActionRequired("AI output cannot change request status")
        if new_status not in ALLOWED[prev]:
            raise InvalidTransition(f"{prev.value} -> {new_status.value} is not allowed")
        if new_status in HUMAN_ONLY and actor.kind != ActorKind.HUMAN:
            raise HumanActionRequired(f"{new_status.value} requires an authorized human decision")

        ts = self._now()
        if new_status == S.AUTOMATION_EXCEPTION:
            record.resume_status = prev
        elif prev == S.AUTOMATION_EXCEPTION:
            record.resume_status = None
            record.failed_step = None
            record.last_error = None
        if new_status == S.IN_ASSESSMENT and record.assessment_start_date is None:
            record.assessment_start_date = ts
        if new_status == S.COMPLETED:
            record.completion_date = ts
        if new_status == S.CLOSED:
            record.closure_date = ts

        record.status = new_status
        record.modified_date = ts
        self._repo.save_request(record)
        self._repo.add_status_history(
            record.request_id, prev, new_status, ts, f"{actor.kind.value}:{actor.name}", reason
        )
        return True
