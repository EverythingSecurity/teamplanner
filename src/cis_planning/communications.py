"""Controlled requester/architect communications (agent spec section 15).

Templates are fixed text with named fields. The model never writes an email; the only model-derived
text that reaches a requester is the list of missing-information items, and it is sanitised here.
Nothing in these templates states or implies a security approval.
"""

from __future__ import annotations

import re
from enum import Enum

from .models import RequestRecord, is_valid_email, utcnow
from .ports import IntegrationError, Mailer
from .repository import Repository

_URL_RE = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
MAX_MISSING_ITEMS = 10
MAX_ITEM_CHARS = 300


class CommType(str, Enum):
    REQUEST_REGISTERED = "REQUEST_REGISTERED"
    MISSING_INFORMATION = "MISSING_INFORMATION"
    ARCHITECT_ASSIGNED = "ARCHITECT_ASSIGNED"
    ARCHITECT_UPDATE = "ARCHITECT_UPDATE"


def sanitize_item(text: str) -> str:
    text = _CTRL_RE.sub(" ", text)
    text = _URL_RE.sub("[link removed]", text)
    text = " ".join(text.split())
    return text[:MAX_ITEM_CHARS]


def sanitize_items(items: list[str]) -> list[str]:
    cleaned = [sanitize_item(i) for i in items]
    return [i for i in cleaned if i][:MAX_MISSING_ITEMS]


def _or(value: str, fallback: str) -> str:
    return value.strip() if value and value.strip() else fallback


def _status_label(record: RequestRecord) -> str:
    return record.status.value.replace("_", " ").title()


def _first_name(record: RequestRecord) -> str:
    return _or(record.requestor_name, "Requestor")


def registered(record: RequestRecord) -> tuple[str, str]:
    subject = f"CIS Request {record.request_id} Registered - {_or(record.project_name, 'Project not specified')}"
    body = (
        f"Dear {_first_name(record)},\n\n"
        "Your request has been registered with CIS Planning.\n\n"
        f"Reference: {record.request_id}\n"
        f"Project: {_or(record.project_name, 'To be confirmed')}\n"
        f"Request Type: {_or(record.request_type, 'To be confirmed')}\n"
        f"Current Status: {_status_label(record)}\n\n"
        "The CIS Planning team will review the requirement and progress the security assessment.\n\n"
        f"Please quote {record.request_id} in future correspondence regarding this request.\n\n"
        "Regards,\nCIS Planning\n"
    )
    return subject, body


def missing_information(record: RequestRecord, items: list[str]) -> tuple[str, str]:
    bullets = "\n".join(f"- {i}" for i in items)
    subject = f"Information Required - CIS Request {record.request_id}"
    body = (
        f"Dear {_first_name(record)},\n\n"
        f"CIS Planning has reviewed your request {record.request_id}.\n\n"
        "To proceed with the security assessment, please provide the following information:\n\n"
        f"{bullets}\n\n"
        "Regards,\nCIS Planning\n"
    )
    return subject, body


def _internal_summary(record: RequestRecord, planner_url: str | None) -> str:
    skills = ", ".join(s.value for s in record.required_skills) or "None identified"
    return (
        f"Request ID: {record.request_id}\n"
        f"Requester: {_or(record.requestor_name, 'Unknown')} <{record.requestor_email}>\n"
        f"Project: {_or(record.project_name, 'Not specified')}\n"
        f"Request Type: {_or(record.request_type, 'Not specified')}\n"
        f"Priority (recommended): {record.priority.value}   Complexity (recommended): {record.complexity.value}\n"
        f"Required Skills: {skills}\n"
        f"Summary: {_or(record.summary, 'None')}\n"
        f"Planner: {planner_url or 'n/a'}\n"
    )


def architect_assigned(record: RequestRecord, architect_name: str) -> tuple[str, str]:
    subject = f"CIS Request {record.request_id} assigned to you - {_or(record.project_name, 'Project not specified')}"
    body = (
        f"Hello {architect_name},\n\n"
        "A CIS Planning request has been assigned to you. Triage values are AI recommendations for routing "
        "and are not a security decision.\n\n"
        f"{_internal_summary(record, record.planner_url)}\n"
        f"Assignment reason: {record.assignment_reason}\n\n"
        "Regards,\nCIS Planning\n"
    )
    return subject, body


def architect_update(record: RequestRecord, architect_name: str, note: str) -> tuple[str, str]:
    subject = f"Update on CIS Request {record.request_id}"
    body = (
        f"Hello {architect_name},\n\n{note}\n\n"
        f"{_internal_summary(record, record.planner_url)}\nRegards,\nCIS Planning\n"
    )
    return subject, body


class CommunicationService:
    """Sends at most once per (request, type, recipient, dedupe key) and logs every send."""

    def __init__(self, mailer: Mailer, repo: Repository, now=utcnow):
        self._mailer = mailer
        self._repo = repo
        self._now = now

    def send(
        self, record: RequestRecord, comm_type: CommType, recipient: str, subject: str, body: str,
        dedupe_key: str = "",
    ) -> bool:
        if not is_valid_email(recipient):
            raise IntegrationError("communications", f"no valid recipient for {comm_type.value}")
        if self._repo.has_communication(record.request_id, comm_type.value, recipient, dedupe_key):
            return False
        ref = self._mailer.send([recipient], subject, body)
        self._repo.add_communication({
            "RequestID": record.request_id,
            "CommunicationType": comm_type.value,
            "Recipient": recipient,
            "Timestamp": self._now().isoformat(),
            "MessageID": ref,
            "TemplateType": comm_type.value,
            "DedupeKey": f"{comm_type.value}|{recipient.lower()}|{dedupe_key}",
        })
        return True
