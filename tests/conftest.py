from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cis_planning.config import Settings
from cis_planning.models import Architect, EmailMessage, TriageResult
from cis_planning.pipeline import Pipeline
from cis_planning.ports import IntegrationError, PlannerTaskRef, StaticArchitectDirectory
from cis_planning.repository import SqliteRepository
from cis_planning.taxonomy import IntakeClassification, SecurityDomain as D


def make_msg(**kw) -> EmailMessage:
    base = dict(
        message_id="m1", internet_message_id="<m1@x>", conversation_id="c1",
        received_at=datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc),
        sender_name="Priya Nair", sender_email="Priya.Nair@contoso.com",
        to=["cis-planning@contoso.com"], subject="API gateway review", body="Please review our API.",
    )
    base.update(kw)
    return EmailMessage(**base)


def make_tri(**kw) -> TriageResult:
    base = dict(
        is_actionable=True, intake_classification=IntakeClassification.NEW_REQUEST, is_new_request=True,
        request_type="Architecture Review", project_name="Customer Portal", requestor_name="Priya Nair",
        requestor_email="priya.nair@contoso.com", short_summary="API gateway review",
        detailed_summary="Review the API gateway design.", required_skills=[D.API_SECURITY, D.CLOUD_SECURITY],
        security_domains=[D.API_SECURITY, D.CLOUD_SECURITY], information_complete=True, confidence=0.92,
        reasoning_summary="Explicit request for API/cloud review.",
    )
    base.update(kw)
    return TriageResult(**base)


ARCHITECTS = [
    Architect(architect_id="A001", name="Ada", email="ada@contoso.com",
              primary_skills=[D.API_SECURITY, D.CLOUD_SECURITY], secondary_skills=[D.DEVSECOPS], maximum_capacity=3),
    Architect(architect_id="A002", name="Ben", email="ben@contoso.com",
              primary_skills=[D.NETWORK_SECURITY], secondary_skills=[D.CLOUD_SECURITY], maximum_capacity=3),
]


class FakeTriage:
    def __init__(self, *results):
        self.queue = list(results)
        self.calls: list[tuple[EmailMessage, dict]] = []

    def triage(self, msg, ctx):
        self.calls.append((msg, ctx))
        item = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        if isinstance(item, Exception):
            raise item
        return item


class FakePlanner:
    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.fail_next_create = False
        self.updates: list[tuple[str, dict]] = []

    def find_task(self, request_id):
        for tid, t in self.tasks.items():
            if t["title"].startswith(f"{request_id} |"):
                return PlannerTaskRef(tid, f"https://planner/{tid}")
        return None

    def create_task(self, *, title, description, bucket, priority, assignee_email):
        if self.fail_next_create:
            self.fail_next_create = False
            raise IntegrationError("planner", "planner unavailable")
        tid = f"T{len(self.tasks) + 1}"
        self.tasks[tid] = dict(title=title, description=description, bucket=bucket, assignee=assignee_email)
        return PlannerTaskRef(tid, f"https://planner/{tid}")

    def update_task(self, task_id, **kw):
        self.updates.append((task_id, kw))


class FakeMailer:
    def __init__(self):
        self.sent: list[tuple[list[str], str, str]] = []
        self.fail = False

    def send(self, to, subject, body):
        if self.fail:
            raise IntegrationError("mail", "send failed")
        self.sent.append((to, subject, body))
        return f"ref-{len(self.sent)}"


@pytest.fixture
def repo():
    fixed = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)
    return SqliteRepository(":memory:", now=lambda: fixed)


@pytest.fixture
def planner():
    return FakePlanner()


@pytest.fixture
def mailer():
    return FakeMailer()


@pytest.fixture
def build(repo, planner, mailer):
    def _build(*results, architects=ARCHITECTS, own=("cis-planning@contoso.com",)):
        triage = FakeTriage(*results)
        pipe = Pipeline(
            repo=repo, triage=triage, planner=planner, mailer=mailer,
            architects=StaticArchitectDirectory(architects),
            settings=Settings(confidence_threshold=0.7), own_addresses=set(own),
        )
        return pipe, triage
    return _build
