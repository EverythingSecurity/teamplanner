"""Controlled vocabularies. Changing the security taxonomy is an administrator decision (agent spec section 6)."""

from __future__ import annotations

from enum import Enum


class SecurityDomain(str, Enum):
    SECURITY_ARCHITECTURE = "Security Architecture"
    NETWORK_SECURITY = "Network Security"
    CLOUD_SECURITY = "Cloud Security"
    INFRASTRUCTURE_SECURITY = "Infrastructure Security"
    APPLICATION_SECURITY = "Application Security"
    API_SECURITY = "API Security"
    IAM = "Identity and Access Management"
    PAM = "Privileged Access Management"
    DATA_SECURITY = "Data Security"
    AI_SECURITY = "AI Security"
    M365_SECURITY = "M365 Security"
    DEVSECOPS = "DevSecOps"
    ENDPOINT_SECURITY = "Endpoint Security"
    VULNERABILITY_MANAGEMENT = "Vulnerability Management"
    PENETRATION_TESTING = "Penetration Testing"
    SECURITY_MONITORING = "Security Monitoring"
    CRYPTO_PKI = "Cryptography and PKI"
    FIREWALL_CONNECTIVITY = "Firewall and Connectivity"
    REGULATORY_COMPLIANCE = "Regulatory and Compliance"
    SECURITY_EXCEPTION = "Security Exception"
    ARCHITECTURE_GOVERNANCE = "Architecture Governance"


class IntakeClassification(str, Enum):
    NEW_REQUEST = "NEW_REQUEST"
    EXISTING_REQUEST_UPDATE = "EXISTING_REQUEST_UPDATE"
    REQUESTER_CLARIFICATION = "REQUESTER_CLARIFICATION"
    INFORMATIONAL = "INFORMATIONAL"
    NON_CIS_OPERATIONAL = "NON_CIS_OPERATIONAL"
    DUPLICATE = "DUPLICATE"
    NOISE = "NOISE"
    REQUIRES_HUMAN_TRIAGE = "REQUIRES_HUMAN_TRIAGE"


class Priority(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Complexity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class RiskIndicator(str, Enum):
    # The spec does not enumerate risk values; this mirrors the priority scale.
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class RequestStatus(str, Enum):
    RECEIVED = "RECEIVED"
    TRIAGED = "TRIAGED"
    ASSIGNED = "ASSIGNED"
    IN_ASSESSMENT = "IN_ASSESSMENT"
    WAITING_FOR_REQUESTOR = "WAITING_FOR_REQUESTOR"
    WAITING_FOR_CIS = "WAITING_FOR_CIS"
    REVIEW = "REVIEW"
    COMPLETED = "COMPLETED"
    CLOSED = "CLOSED"
    ON_HOLD = "ON_HOLD"
    CANCELLED = "CANCELLED"
    AUTOMATION_EXCEPTION = "AUTOMATION_EXCEPTION"


class ActorKind(str, Enum):
    AI = "AI"
    AUTOMATION = "AUTOMATION"
    HUMAN = "HUMAN"


class Availability(str, Enum):
    AVAILABLE = "AVAILABLE"
    LIMITED = "LIMITED"
    UNAVAILABLE = "UNAVAILABLE"


class PlannerBucket(str, Enum):
    NEW = "New"
    TRIAGED = "Triaged"
    ASSIGNED = "Assigned"
    IN_ASSESSMENT = "In Assessment"
    WAITING_FOR_REQUESTOR = "Waiting for Requestor"
    WAITING_FOR_CIS = "Waiting for CIS"
    REVIEW = "Review"
    COMPLETED = "Completed"
    ON_HOLD = "On Hold"


STATUS_TO_BUCKET: dict[RequestStatus, PlannerBucket | None] = {
    RequestStatus.RECEIVED: PlannerBucket.NEW,
    RequestStatus.TRIAGED: PlannerBucket.TRIAGED,
    RequestStatus.ASSIGNED: PlannerBucket.ASSIGNED,
    RequestStatus.IN_ASSESSMENT: PlannerBucket.IN_ASSESSMENT,
    RequestStatus.WAITING_FOR_REQUESTOR: PlannerBucket.WAITING_FOR_REQUESTOR,
    RequestStatus.WAITING_FOR_CIS: PlannerBucket.WAITING_FOR_CIS,
    RequestStatus.REVIEW: PlannerBucket.REVIEW,
    RequestStatus.COMPLETED: PlannerBucket.COMPLETED,
    RequestStatus.CLOSED: PlannerBucket.COMPLETED,
    RequestStatus.ON_HOLD: PlannerBucket.ON_HOLD,
    # A task stays where it is when automation fails or the request is cancelled.
    RequestStatus.CANCELLED: None,
    RequestStatus.AUTOMATION_EXCEPTION: None,
}

TERMINAL_STATUSES = frozenset({RequestStatus.CLOSED, RequestStatus.CANCELLED})
INACTIVE_STATUSES = frozenset(
    {RequestStatus.COMPLETED, RequestStatus.CLOSED, RequestStatus.CANCELLED}
)
