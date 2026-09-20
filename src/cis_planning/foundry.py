"""Azure AI Foundry integration: define/deploy the triage agent and call it.

Uses the Foundry Agent Service (azure-ai-projects 2.x): a versioned *prompt agent* whose response
format is a strict JSON schema, so the controlled taxonomy and enums are enforced by the service
and validated again by pydantic on our side.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .config import Settings
from .models import EmailMessage, TriageResult
from .ports import TriageInvalidOutput, TriageUnavailable
from .taxonomy import Complexity, IntakeClassification, Priority, RiskIndicator, SecurityDomain

AGENT_DESCRIPTION = (
    "CIS Planning Control Tower triage: classifies intake email, identifies required security skills "
    "and missing information. Recommends only; never approves or assigns."
)
SCHEMA_NAME = "cis_triage_result"


def _enum(e) -> dict[str, Any]:
    return {"type": "string", "enum": [m.value for m in e]}


def triage_json_schema() -> dict[str, Any]:
    """Strict-mode schema: every property required, no additional properties, nullable via type union."""
    s = {"type": "string"}
    b = {"type": "boolean"}
    props: dict[str, Any] = {
        "request_id": {"type": ["string", "null"]},
        "is_actionable": b,
        "intake_classification": _enum(IntakeClassification),
        "is_new_request": b,
        "existing_request_id": {"type": ["string", "null"]},
        "request_type": s,
        "project_name": s,
        "requestor_name": s,
        "requestor_email": s,
        "business_unit": s,
        "short_summary": s,
        "detailed_summary": s,
        "required_skills": {"type": "array", "items": _enum(SecurityDomain)},
        "security_domains": {"type": "array", "items": _enum(SecurityDomain)},
        "priority": _enum(Priority),
        "complexity": _enum(Complexity),
        "risk_indicator": _enum(RiskIndicator),
        "information_complete": b,
        "missing_information": {"type": "array", "items": s},
        "recommended_next_action": s,
        "requires_human_triage": b,
        "confidence": {"type": "number"},
        "reasoning_summary": s,
    }
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def load_instructions() -> str:
    """Instructions ship beside the package in the repo root (instructions/triage_agent.md)."""
    candidates = [
        Path(__file__).resolve().parents[2] / "instructions" / "triage_agent.md",
        Path.cwd() / "instructions" / "triage_agent.md",
    ]
    for p in candidates:
        if p.is_file():
            return p.read_text(encoding="utf-8")
    raise FileNotFoundError("instructions/triage_agent.md not found")


def build_definition(settings: Settings, instructions: str | None = None):
    from azure.ai.projects.models import (
        PromptAgentDefinition,
        PromptAgentDefinitionTextOptions,
        TextResponseFormatJsonSchema,
    )

    kwargs: dict[str, Any] = {}
    if settings.foundry_temperature is not None:
        kwargs["temperature"] = settings.foundry_temperature
    return PromptAgentDefinition(
        model=settings.foundry_model_deployment,
        instructions=instructions if instructions is not None else load_instructions(),
        text=PromptAgentDefinitionTextOptions(
            format=TextResponseFormatJsonSchema(
                name=SCHEMA_NAME,
                description="Structured CIS triage record",
                schema=triage_json_schema(),
                strict=True,
            )
        ),
        **kwargs,
    )


def definition_fingerprint(definition) -> str:
    payload = json.dumps(definition.as_dict(), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class DeployResult:
    agent_name: str
    version: str
    created: bool
    fingerprint: str


def _latest_version(project, agent_name: str):
    from azure.core.exceptions import ResourceNotFoundError

    try:
        return project.agents.get(agent_name=agent_name).versions.latest
    except ResourceNotFoundError:
        return None


def deploy_triage_agent(project, settings: Settings, *, force: bool = False) -> DeployResult:
    """Create a new agent version only when the definition changed (idempotent deploys)."""
    definition = build_definition(settings)
    fp = definition_fingerprint(definition)
    latest = _latest_version(project, settings.foundry_agent_name)
    if latest is not None and not force and (latest.metadata or {}).get("definition_sha256") == fp:
        return DeployResult(settings.foundry_agent_name, str(latest.version), False, fp)
    created = project.agents.create_version(
        agent_name=settings.foundry_agent_name,
        definition=definition,
        description=AGENT_DESCRIPTION,
        metadata={"definition_sha256": fp, "managed_by": "cis-planning-agent"},
    )
    return DeployResult(settings.foundry_agent_name, str(created.version), True, fp)


def build_input(msg: EmailMessage, correlation_context: dict, max_body_chars: int) -> str:
    """The email is serialised as JSON string values so it cannot masquerade as instructions."""
    body = msg.body
    truncated = len(body) > max_body_chars
    payload = {
        "message": {
            "message_id": msg.message_id,
            "conversation_id": msg.conversation_id,
            "received_at": msg.received_at.isoformat(),
            "sender_name": msg.sender_name,
            "sender_email": msg.sender_email,
            "to": msg.to,
            "cc": msg.cc,
            "subject": msg.subject,
            "body": body[:max_body_chars],
            "body_truncated": truncated,
            "attachments": [a.model_dump() for a in msg.attachments],
            "sensitivity": msg.sensitivity,
            "importance": msg.importance,
        },
        "correlation": correlation_context,
    }
    return json.dumps(payload, ensure_ascii=False)


class FoundryTriageClient:
    """Calls the deployed prompt agent once per email (stateless: no conversation is created)."""

    def __init__(self, project, settings: Settings, *, retries: int = 1):
        self._openai = project.get_openai_client(agent_name=settings.foundry_agent_name)
        self._max_body = settings.max_body_chars
        self._retries = retries

    def triage(self, msg: EmailMessage, correlation_context: dict) -> TriageResult:
        request_input = build_input(msg, correlation_context, self._max_body)
        last_error = "no attempt made"
        for _ in range(self._retries + 1):
            try:
                response = self._openai.responses.create(input=request_input)
            except Exception as exc:  # network, auth, throttling, service errors
                raise TriageUnavailable(f"{type(exc).__name__}: {str(exc)[:300]}") from exc
            text = (getattr(response, "output_text", "") or "").strip()
            try:
                return TriageResult.model_validate_json(text)
            except ValidationError as exc:
                last_error = f"invalid triage JSON ({exc.error_count()} validation errors)"
        raise TriageInvalidOutput(last_error)


def open_project_client(settings: Settings):
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    settings.require("foundry_project_endpoint")
    return AIProjectClient(endpoint=settings.foundry_project_endpoint, credential=DefaultAzureCredential())
