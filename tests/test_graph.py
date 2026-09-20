from __future__ import annotations

import json

import httpx

from cis_planning.graph import GraphClient, GraphMailbox, GraphPlanner
from cis_planning.taxonomy import PlannerBucket, Priority


def graph_with(handler):
    return GraphClient(token_provider=lambda: "tok", transport=httpx.MockTransport(handler))


def test_planner_create_task_resolves_bucket_and_assignee_and_sets_description():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path, json.loads(req.content) if req.content else None, req.headers.get("If-Match")))
        p = req.url.path
        if p.endswith("/buckets"):
            return httpx.Response(200, json={"value": [{"id": "b1", "name": "Assigned"}]})
        if p.startswith("/v1.0/users/"):
            return httpx.Response(200, json={"id": "uid-1"})
        if p == "/v1.0/planner/tasks" and req.method == "POST":
            return httpx.Response(201, json={"id": "task-1"})
        if p.endswith("/details") and req.method == "GET":
            return httpx.Response(200, json={"@odata.etag": 'W/"e1"'})
        return httpx.Response(204)

    planner = GraphPlanner(graph_with(handler), "plan-1", "https://p/{plan_id}/{task_id}")
    ref = planner.create_task(title="CIS-2026-00001 | P | T", description="d", bucket=PlannerBucket.ASSIGNED,
                              priority=Priority.HIGH, assignee_email="ada@contoso.com")
    assert ref.task_id == "task-1" and ref.url == "https://p/plan-1/task-1"
    create = next(s for s in seen if s[0] == "POST")
    assert create[2]["bucketId"] == "b1" and create[2]["priority"] == 3 and "uid-1" in create[2]["assignments"]
    patch = next(s for s in seen if s[0] == "PATCH")
    assert patch[3] == 'W/"e1"' and patch[2] == {"description": "d"}


def test_planner_find_task_pages_and_matches_on_request_id_prefix():
    def handler(req: httpx.Request) -> httpx.Response:
        if "skiptoken" in str(req.url):
            return httpx.Response(200, json={"value": [{"id": "t2", "title": "CIS-2026-00002 | X | Y"}]})
        return httpx.Response(200, json={"value": [{"id": "t1", "title": "CIS-2026-00001 | X | Y"}],
                                         "@odata.nextLink": "https://graph.microsoft.com/v1.0/next?skiptoken=1"})

    planner = GraphPlanner(graph_with(handler), "plan-1", "u/{task_id}")
    assert planner.find_task("CIS-2026-00002").task_id == "t2"
    assert planner.find_task("CIS-2026-00009") is None


def test_mailbox_maps_graph_messages_and_requests_text_bodies():
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["prefer"] = req.headers.get("Prefer")
        return httpx.Response(200, json={"value": [{
            "id": "AAMk1", "internetMessageId": "<a@b>", "conversationId": "cv1",
            "receivedDateTime": "2026-09-18T09:30:00Z", "subject": "Hi",
            "from": {"emailAddress": {"name": "Priya", "address": "Priya@Contoso.com"}},
            "toRecipients": [{"emailAddress": {"address": "cis@contoso.com"}}], "ccRecipients": [],
            "body": {"contentType": "text", "content": "hello"}, "importance": "high", "sensitivity": "normal",
            "attachments": [{"name": "a.pdf", "contentType": "application/pdf", "size": 10}],
        }]})

    (m,) = GraphMailbox(graph_with(handler), "cis@contoso.com").fetch_unread()
    assert captured["prefer"] == 'outlook.body-content-type="text"'
    assert m.sender_email == "priya@contoso.com" and m.dedupe_key == "<a@b>" and m.attachments[0].name == "a.pdf"
    assert m.conversation_id == "cv1" and m.body == "hello"
