"""Microsoft Graph adapters: shared-mailbox intake, outbound mail, and Planner.

Auth is DefaultAzureCredential (managed identity in Azure, `az login` locally). Grant the identity
only what it needs and scope mailbox access to the CIS Planning mailbox (Exchange application
access policy / RBAC for Applications):
  Mail.ReadWrite, Mail.Send, Tasks.ReadWrite (Planner), User.ReadBasic.All (resolve assignees).
Verify Planner application-permission support and consent in your tenant before relying on it.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Any, Callable

import httpx

from .config import Settings
from .models import AttachmentMeta, EmailMessage
from .ports import IntegrationError, PlannerTaskRef
from .taxonomy import PlannerBucket, Priority

GRAPH = "https://graph.microsoft.com/v1.0"
_PLANNER_PRIORITY = {Priority.CRITICAL: 1, Priority.HIGH: 3, Priority.MEDIUM: 5, Priority.LOW: 9}


class GraphClient:
    def __init__(self, token_provider: Callable[[], str] | None = None, transport: httpx.BaseTransport | None = None):
        if token_provider is None:
            from azure.identity import DefaultAzureCredential

            cred = DefaultAzureCredential()
            token_provider = lambda: cred.get_token("https://graph.microsoft.com/.default").token  # noqa: E731
        self._token = token_provider
        self._http = httpx.Client(timeout=30, transport=transport)

    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None, **kw: Any) -> Any:
        if url.startswith("/"):
            url = GRAPH + url
        h = {"Authorization": f"Bearer {self._token()}", **(headers or {})}
        last: Exception | None = None
        for attempt in range(4):
            try:
                resp = self._http.request(method, url, headers=h, **kw)
            except httpx.HTTPError as exc:
                last = exc
                time.sleep(2**attempt)
                continue
            if resp.status_code in (429, 503, 504):
                time.sleep(min(int(resp.headers.get("Retry-After", 2**attempt)), 30))
                last = IntegrationError("graph", f"{resp.status_code} from {method} {url.split('?')[0]}")
                continue
            if resp.status_code >= 400:
                raise IntegrationError("graph", f"{resp.status_code} from {method} {url.split('?')[0]}: {resp.text[:200]}")
            return resp.json() if resp.content else None
        raise IntegrationError("graph", f"request failed after retries: {last}")

    def pages(self, url: str, **kw: Any):
        while url:
            data = self.request("GET", url, **kw)
            yield from data.get("value", [])
            url = data.get("@odata.nextLink")


class GraphMailbox:
    """Shared mailbox intake and outbound mail."""

    def __init__(self, graph: GraphClient, mailbox: str, folder: str = "inbox"):
        self._g, self._mb, self._folder = graph, mailbox, folder

    def fetch_unread(self, top: int = 25) -> list[EmailMessage]:
        url = (
            f"/users/{self._mb}/mailFolders/{self._folder}/messages"
            f"?$filter=isRead eq false&$top={top}"
            "&$select=id,internetMessageId,conversationId,receivedDateTime,from,toRecipients,ccRecipients,"
            "subject,body,hasAttachments,importance,sensitivity"
            "&$expand=attachments($select=name,contentType,size)"
        )
        # Ask Graph for text bodies so HTML never reaches the model.
        rows = list(self._g.pages(url, headers={"Prefer": 'outlook.body-content-type="text"'}))
        msgs = [self._to_message(r) for r in rows]
        return sorted(msgs, key=lambda m: m.received_at)

    def mark_read(self, msg: EmailMessage) -> None:
        self._g.request("PATCH", f"/users/{self._mb}/messages/{msg.message_id}", json={"isRead": True})

    def send(self, to: list[str], subject: str, body: str) -> str:
        self._g.request("POST", f"/users/{self._mb}/sendMail", json={
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": a}} for a in to],
            },
            "saveToSentItems": True,
        })
        return f"sent-{uuid.uuid4()}"  # sendMail returns 202 with no message id

    @staticmethod
    def _addr(entry: dict[str, Any] | None) -> tuple[str, str]:
        ea = (entry or {}).get("emailAddress", {})
        return ea.get("name", "") or "", ea.get("address", "") or ""

    def _to_message(self, r: dict[str, Any]) -> EmailMessage:
        name, addr = self._addr(r.get("from"))
        return EmailMessage(
            message_id=r["id"],
            internet_message_id=r.get("internetMessageId"),
            conversation_id=r.get("conversationId"),
            received_at=datetime.fromisoformat(r["receivedDateTime"].replace("Z", "+00:00")),
            sender_name=name,
            sender_email=addr,
            to=[self._addr(x)[1] for x in r.get("toRecipients", [])],
            cc=[self._addr(x)[1] for x in r.get("ccRecipients", [])],
            subject=r.get("subject") or "",
            body=(r.get("body") or {}).get("content", ""),
            attachments=[
                AttachmentMeta(name=a.get("name", ""), content_type=a.get("contentType"), size=a.get("size"))
                for a in r.get("attachments", [])
            ],
            sensitivity=r.get("sensitivity"),
            importance=r.get("importance"),
        )


class GraphPlanner:
    """Planner is the execution surface; the system of record stays authoritative."""

    def __init__(self, graph: GraphClient, plan_id: str, task_url_template: str):
        self._g, self._plan, self._url = graph, plan_id, task_url_template
        self._buckets: dict[str, str] | None = None
        self._users: dict[str, str] = {}

    def _bucket_id(self, bucket: PlannerBucket) -> str:
        if self._buckets is None:
            self._buckets = {b["name"]: b["id"] for b in self._g.pages(f"/planner/plans/{self._plan}/buckets")}
        try:
            return self._buckets[bucket.value]
        except KeyError:
            raise IntegrationError("planner", f"bucket '{bucket.value}' not found in plan") from None

    def _user_id(self, email: str) -> str:
        if email not in self._users:
            self._users[email] = self._g.request("GET", f"/users/{email}?$select=id")["id"]
        return self._users[email]

    def _ref(self, task_id: str) -> PlannerTaskRef:
        return PlannerTaskRef(task_id, self._url.format(plan_id=self._plan, task_id=task_id))

    def find_task(self, request_id: str) -> PlannerTaskRef | None:
        for t in self._g.pages(f"/planner/plans/{self._plan}/tasks"):
            if str(t.get("title", "")).startswith(f"{request_id} |"):
                return self._ref(t["id"])
        return None

    def create_task(self, *, title, description, bucket, priority, assignee_email) -> PlannerTaskRef:
        body: dict[str, Any] = {
            "planId": self._plan, "bucketId": self._bucket_id(bucket), "title": title,
            "priority": _PLANNER_PRIORITY[priority],
        }
        if assignee_email:
            body["assignments"] = {self._user_id(assignee_email): {
                "@odata.type": "#microsoft.graph.plannerAssignment", "orderHint": " !"}}
        task = self._g.request("POST", "/planner/tasks", json=body)
        self._set_description(task["id"], description)
        return self._ref(task["id"])

    def _set_description(self, task_id: str, description: str) -> None:
        details = self._g.request("GET", f"/planner/tasks/{task_id}/details")
        self._g.request("PATCH", f"/planner/tasks/{task_id}/details",
                        headers={"If-Match": details["@odata.etag"]},
                        json={"description": description})

    def update_task(self, task_id, *, bucket=None, description=None, assignee_email=None) -> None:
        if bucket is not None or assignee_email is not None:
            task = self._g.request("GET", f"/planner/tasks/{task_id}")
            patch: dict[str, Any] = {}
            if bucket is not None:
                patch["bucketId"] = self._bucket_id(bucket)
            if assignee_email is not None:
                assignments: dict[str, Any] = {u: None for u in task.get("assignments", {})}  # null removes
                assignments[self._user_id(assignee_email)] = {
                    "@odata.type": "#microsoft.graph.plannerAssignment", "orderHint": " !"}
                patch["assignments"] = assignments
            self._g.request("PATCH", f"/planner/tasks/{task_id}", headers={"If-Match": task["@odata.etag"]}, json=patch)
        if description is not None:
            self._set_description(task_id, description)
