"""Runtime configuration, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    foundry_project_endpoint: str = ""
    foundry_model_deployment: str = "gpt-5-mini"
    foundry_agent_name: str = "cis-planning-triage"
    # Reasoning models reject a temperature; leave unset unless the model supports it.
    foundry_temperature: float | None = None

    confidence_threshold: float = 0.7
    max_body_chars: int = 20000
    assignment_min_coverage: float = 1.0

    db_path: str = "./cis_planning.db"
    architect_matrix_path: str = "./config/architects.json"

    graph_mailbox: str = ""
    graph_mail_folder: str = "inbox"
    planner_plan_id: str = ""
    planner_task_url_template: str = "https://planner.cloud.microsoft/webui/plan/{plan_id}/view/board/task/{task_id}"

    @classmethod
    def from_env(cls) -> "Settings":
        temp = os.environ.get("FOUNDRY_TEMPERATURE")
        return cls(
            foundry_project_endpoint=os.environ.get("FOUNDRY_PROJECT_ENDPOINT", ""),
            foundry_model_deployment=os.environ.get("FOUNDRY_MODEL_DEPLOYMENT", "gpt-5-mini"),
            foundry_agent_name=os.environ.get("FOUNDRY_AGENT_NAME", "cis-planning-triage"),
            foundry_temperature=float(temp) if temp else None,
            confidence_threshold=_float("CIS_CONFIDENCE_THRESHOLD", 0.7),
            max_body_chars=_int("CIS_MAX_BODY_CHARS", 20000),
            assignment_min_coverage=_float("CIS_ASSIGNMENT_MIN_COVERAGE", 1.0),
            db_path=os.environ.get("CIS_DB_PATH", "./cis_planning.db"),
            architect_matrix_path=os.environ.get("CIS_ARCHITECT_MATRIX_PATH", "./config/architects.json"),
            graph_mailbox=os.environ.get("GRAPH_MAILBOX", ""),
            graph_mail_folder=os.environ.get("GRAPH_MAIL_FOLDER", "inbox"),
            planner_plan_id=os.environ.get("PLANNER_PLAN_ID", ""),
        )

    def require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            env = ", ".join(n.upper() for n in missing)
            raise SystemExit(f"Missing required configuration: {env}")
