"""Request correlation and duplicate detection. Runs before AI triage and before any record is created."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import EmailMessage, RequestRecord
from .repository import Repository

REQUEST_ID_RE = re.compile(r"\bCIS-\d{4}-\d{5}\b", re.IGNORECASE)


@dataclass
class Correlation:
    """Outcome of deterministic correlation.

    existing      - the one request this message belongs to (strong evidence), if any
    matched_by    - "request_id_in_subject" | "conversation_id" | None
    referenced_ids- known request IDs mentioned only in the body (weak evidence, passed to triage as a hint)
    ambiguous_ids - several requests match with equal strength; a human must decide
    """

    existing: RequestRecord | None = None
    matched_by: str | None = None
    referenced_ids: list[str] = field(default_factory=list)
    ambiguous_ids: list[str] = field(default_factory=list)

    @property
    def ambiguous(self) -> bool:
        return bool(self.ambiguous_ids)

    def context(self) -> dict:
        ex = self.existing
        return {
            "matched_by": self.matched_by,
            "existing_request": None if ex is None else {
                "request_id": ex.request_id,
                "project_name": ex.project_name,
                "request_type": ex.request_type,
                "status": ex.status.value,
            },
            "referenced_request_ids": self.referenced_ids,
        }


def _ids(text: str) -> list[str]:
    seen: list[str] = []
    for m in REQUEST_ID_RE.findall(text or ""):
        rid = m.upper()
        if rid not in seen:
            seen.append(rid)
    return seen


def correlate(msg: EmailMessage, repo: Repository) -> Correlation:
    subject_known = [r for r in (repo.get_request(i) for i in _ids(msg.subject)) if r]
    body_known_ids = [
        r.request_id for r in (repo.get_request(i) for i in _ids(msg.body)) if r
    ]
    subject_ids = {r.request_id for r in subject_known}
    referenced = [i for i in body_known_ids if i not in subject_ids]

    if len(subject_known) == 1:
        return Correlation(subject_known[0], "request_id_in_subject", referenced)
    if len(subject_known) > 1:
        return Correlation(referenced_ids=referenced, ambiguous_ids=[r.request_id for r in subject_known])

    if msg.conversation_id:
        by_conv = repo.find_by_conversation(msg.conversation_id)
        if len(by_conv) == 1:
            return Correlation(by_conv[0], "conversation_id", referenced)
        if len(by_conv) > 1:
            return Correlation(referenced_ids=referenced, ambiguous_ids=[r.request_id for r in by_conv])

    return Correlation(referenced_ids=referenced)
