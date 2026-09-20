"""Integration ports. Real adapters live in graph.py / foundry.py; tests use in-memory fakes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import Architect, EmailMessage, TriageResult
from .taxonomy import PlannerBucket, Priority


class IntegrationError(Exception):
    """An external system failed. The pipeline moves the request to AUTOMATION_EXCEPTION."""

    def __init__(self, component: str, message: str):
        super().__init__(f"{component}: {message}")
        self.component = component


class TriageUnavailable(IntegrationError):
    def __init__(self, message: str):
        super().__init__("foundry", message)


class TriageInvalidOutput(IntegrationError):
    def __init__(self, message: str):
        super().__init__("foundry", message)


class TriageClient(Protocol):
    def triage(self, msg: EmailMessage, correlation_context: dict) -> TriageResult: ...


@dataclass
class PlannerTaskRef:
    task_id: str
    url: str | None = None


class PlannerClient(Protocol):
    def find_task(self, request_id: str) -> PlannerTaskRef | None:
        """Look a task up by the Request ID in its title (crash recovery, avoids duplicates)."""

    def create_task(
        self, *, title: str, description: str, bucket: PlannerBucket, priority: Priority, assignee_email: str | None
    ) -> PlannerTaskRef: ...

    def update_task(
        self, task_id: str, *, bucket: PlannerBucket | None = None, description: str | None = None,
        assignee_email: str | None = None,
    ) -> None: ...


class Mailer(Protocol):
    def send(self, to: list[str], subject: str, body: str) -> str:
        """Send a plain-text message and return a message reference for the communication log."""


class ArchitectDirectory(Protocol):
    def list_architects(self) -> list[Architect]: ...


class StaticArchitectDirectory:
    def __init__(self, architects: list[Architect]):
        self._architects = list(architects)

    def list_architects(self) -> list[Architect]:
        return list(self._architects)
