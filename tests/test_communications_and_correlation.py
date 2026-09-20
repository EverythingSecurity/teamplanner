from __future__ import annotations

import pytest

from cis_planning.communications import CommType, CommunicationService, missing_information, registered, sanitize_items
from cis_planning.correlation import correlate
from cis_planning.models import RequestRecord, utcnow
from cis_planning.ports import IntegrationError

from .conftest import FakeMailer, make_msg


def record(**kw):
    base = dict(request_id="CIS-2026-00001", email_message_id="m", email_dedupe_key="k", received_date=utcnow(),
                requestor_name="Priya", requestor_email="p@x.com", project_name="Portal", request_type="Review")
    base.update(kw)
    return RequestRecord(**base)


def test_registration_template_matches_spec_and_never_implies_approval():
    subject, body = registered(record())
    assert subject == "CIS Request CIS-2026-00001 Registered - Portal"
    for line in ("Reference: CIS-2026-00001", "Project: Portal", "Request Type: Review", "Current Status: Received"):
        assert line in body
    assert "approv" not in body.lower() and "Please quote CIS-2026-00001" in body


def test_unknown_values_are_not_invented():
    _, body = registered(record(project_name="", request_type="", requestor_name=""))
    assert "Project: To be confirmed" in body and "Dear Requestor" in body


def test_missing_info_template():
    subject, body = missing_information(record(), ["Go-live date"])
    assert subject == "Information Required - CIS Request CIS-2026-00001"
    assert "Regards,\nCIS Planning" in body and "- Go-live date" in body


def test_sanitize_strips_links_control_chars_and_caps():
    items = sanitize_items(["See https://evil.example/login now\x07", "  ", "a" * 500] + [f"i{n}" for n in range(20)])
    assert "https" not in items[0] and "\x07" not in items[0] and len(items[1]) == 300 and len(items) == 10


def test_send_is_deduplicated_and_logged(repo):
    mailer = FakeMailer()
    svc = CommunicationService(mailer, repo)
    r = record()
    assert svc.send(r, CommType.REQUEST_REGISTERED, "p@x.com", "s", "b") is True
    assert svc.send(r, CommType.REQUEST_REGISTERED, "P@X.com", "s", "b") is False
    assert svc.send(r, CommType.ARCHITECT_UPDATE, "p@x.com", "s", "b", dedupe_key="m2") is True
    assert len(mailer.sent) == 2 and len(repo.events("communication", r.request_id)) == 2


def test_send_rejects_invalid_recipient(repo):
    with pytest.raises(IntegrationError):
        CommunicationService(FakeMailer(), repo).send(record(), CommType.REQUEST_REGISTERED, "not-an-email", "s", "b")


# -------------------------------------------------------------- correlation
def test_correlation_precedence(repo):
    a, b = record(), record(request_id="CIS-2026-00002", email_dedupe_key="k2", conversation_id="conv-b")
    repo.save_request(a)
    repo.save_request(b)

    c = correlate(make_msg(subject="RE: [cis-2026-00001] hello"), repo)
    assert c.existing.request_id == "CIS-2026-00001" and c.matched_by == "request_id_in_subject"

    c = correlate(make_msg(subject="hello", conversation_id="conv-b"), repo)
    assert c.existing.request_id == "CIS-2026-00002" and c.matched_by == "conversation_id"

    # body-only mentions are a hint, not a match
    c = correlate(make_msg(subject="new thing", conversation_id="zzz", body="Similar to CIS-2026-00001"), repo)
    assert c.existing is None and c.referenced_ids == ["CIS-2026-00001"]

    # two known IDs in the subject: ambiguous, a human decides
    c = correlate(make_msg(subject="CIS-2026-00001 and CIS-2026-00002"), repo)
    assert c.existing is None and c.ambiguous and set(c.ambiguous_ids) == {"CIS-2026-00001", "CIS-2026-00002"}

    # unknown IDs never correlate
    assert correlate(make_msg(subject="CIS-2026-09999", conversation_id="none"), repo).existing is None
