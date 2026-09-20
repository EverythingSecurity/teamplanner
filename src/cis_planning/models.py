"""Data models: intake message, AI triage output, system-of-record entities."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .taxonomy import (
    ActorKind,
    Availability,
    Complexity,
    IntakeClassification,
    Priority,
    RequestStatus,
    RiskIndicator,
    SecurityDomain,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


_EMAIL_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")


def is_valid_email(value: str | None) -> bool:
    return bool(value and _EMAIL_RE.match(value.strip()))


# --------------------------------------------------------------------------- intake


class AttachmentMeta(BaseModel):
    name: str
    content_type: str | None = None
    size: int | None = None


class EmailMessage(BaseModel):
    message_id: str
    internet_message_id: str | None = None
    conversation_id: str | None = None
    received_at: datetime
    sender_name: str = ""
    sender_email: str = ""
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    subject: str = ""
    body: str = ""
    attachments: list[AttachmentMeta] = Field(default_factory=list)
    sensitivity: str | None = None
    importance: str | None = None

    @field_validator("sender_email")
    @classmethod
    def _lower_sender(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("to", "cc")
    @classmethod
    def _lower_list(cls, v: list[str]) -> list[str]:
        return [x.strip().lower() for x in v]

    @property
    def dedupe_key(self) -> str:
        # Graph message ids change when a message is moved between folders; the
        # RFC 5322 Message-ID does not.
        return self.internet_message_id or self.message_id

    @property
    def participants(self) -> set[str]:
        return {self.sender_email, *self.to, *self.cc} - {""}


# ------------------------------------------------------------------------ AI output


class TriageResult(BaseModel):
    """Mirror of the JSON contract in agent spec section 7. Validated after every model call."""

    request_id: str | None = None
    is_actionable: bool
    intake_classification: IntakeClassification
    is_new_request: bool
    existing_request_id: str | None = None
    request_type: str = ""
    project_name: str = ""
    requestor_name: str = ""
    requestor_email: str = ""
    business_unit: str = ""
    short_summary: str = ""
    detailed_summary: str = ""
    required_skills: list[SecurityDomain] = Field(default_factory=list)
    security_domains: list[SecurityDomain] = Field(default_factory=list)
    priority: Priority = Priority.MEDIUM
    complexity: Complexity = Complexity.MEDIUM
    risk_indicator: RiskIndicator = RiskIndicator.MEDIUM
    information_complete: bool = True
    missing_information: list[str] = Field(default_factory=list)
    recommended_next_action: str = ""
    requires_human_triage: bool = False
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning_summary: str = ""

    def consistency_problems(self) -> list[str]:
        """Contradictions inside the model output. Any hit routes the message to a human."""
        c = IntakeClassification
        problems: list[str] = []
        if (self.intake_classification == c.NEW_REQUEST) != self.is_new_request:
            problems.append("is_new_request contradicts intake_classification")
        if self.intake_classification == c.NEW_REQUEST and not self.is_actionable:
            problems.append("NEW_REQUEST marked not actionable")
        if (
            self.intake_classification in (c.EXISTING_REQUEST_UPDATE, c.REQUESTER_CLARIFICATION, c.DUPLICATE)
            and not self.existing_request_id
        ):
            problems.append("update/duplicate classification without existing_request_id")
        if not self.information_complete and not self.missing_information:
            problems.append("information_complete is false but missing_information is empty")
        return problems


# ------------------------------------------------------------------- system of record


class Actor(BaseModel):
    name: str
    kind: ActorKind

    @classmethod
    def automation(cls, component: str) -> "Actor":
        return cls(name=component, kind=ActorKind.AUTOMATION)

    @classmethod
    def ai(cls, component: str) -> "Actor":
        return cls(name=component, kind=ActorKind.AI)

    @classmethod
    def human(cls, name: str) -> "Actor":
        return cls(name=name, kind=ActorKind.HUMAN)


class RequestRecord(BaseModel):
    """CIS_Requests (agent spec section 16), plus recovery fields."""

    request_id: str
    email_message_id: str
    email_dedupe_key: str
    conversation_id: str | None = None
    received_date: datetime
    requestor_name: str = ""
    requestor_email: str = ""
    business_unit: str = ""
    project_name: str = ""
    request_type: str = ""
    summary: str = ""
    detailed_summary: str = ""
    priority: Priority = Priority.MEDIUM
    complexity: Complexity = Complexity.MEDIUM
    risk_indicator: RiskIndicator = RiskIndicator.MEDIUM
    status: RequestStatus = RequestStatus.RECEIVED
    required_skills: list[SecurityDomain] = Field(default_factory=list)
    security_domains: list[SecurityDomain] = Field(default_factory=list)
    assigned_architect_id: str | None = None
    assignment_date: datetime | None = None
    assignment_reason: str | None = None
    planner_task_id: str | None = None
    planner_url: str | None = None
    information_complete: bool = True
    missing_information: list[str] = Field(default_factory=list)
    created_date: datetime = Field(default_factory=utcnow)
    modified_date: datetime = Field(default_factory=utcnow)
    assessment_start_date: datetime | None = None
    completion_date: datetime | None = None
    closure_date: datetime | None = None
    classification_confidence: float | None = None
    requires_human_triage: bool = False
    human_triage_reasons: list[str] = Field(default_factory=list)
    triage: dict[str, Any] | None = None  # validated AI output, kept for audit
    # recovery
    resume_status: RequestStatus | None = None
    failed_step: str | None = None
    last_error: str | None = None

    def flag_for_human(self, reason: str) -> None:
        self.requires_human_triage = True
        if reason not in self.human_triage_reasons:
            self.human_triage_reasons.append(reason)


class Architect(BaseModel):
    """One row of the administrator-maintained Architect Capability Matrix."""

    architect_id: str
    name: str
    email: str
    primary_skills: list[SecurityDomain] = Field(default_factory=list)
    secondary_skills: list[SecurityDomain] = Field(default_factory=list)
    availability: Availability = Availability.AVAILABLE
    maximum_capacity: int = Field(default=5, ge=0)
    enabled: bool = True
    last_updated: datetime | None = None


class ArchitectLoad(BaseModel):
    """Workload is derived from the request store, never trusted from a stored counter."""

    active_request_count: int = 0
    active_project_count: int = 0

    def current_capacity(self, maximum: int) -> int:
        return max(0, maximum - self.active_request_count)
