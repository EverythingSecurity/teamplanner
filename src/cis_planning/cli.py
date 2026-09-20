"""Command line entry points.

  cis-planning deploy-agent [--dry-run] [--force]   create/update the triage agent in Foundry
  cis-planning smoke-test samples/emails/x.json     call the deployed agent on one sample email
  cis-planning run [--once] [--interval 60]         poll the mailbox and process new requests
  cis-planning retry-exceptions                     re-drive requests parked in AUTOMATION_EXCEPTION
"""

from __future__ import annotations

import argparse
import json
import logging
import time

from .config import Settings
from .foundry import (
    FoundryTriageClient, build_definition, definition_fingerprint, deploy_triage_agent, open_project_client,
)
from .models import EmailMessage


def _deploy(args, settings: Settings) -> None:
    if args.dry_run:
        d = build_definition(settings)
        print(json.dumps(d.as_dict(), indent=2))
        print(f"\nfingerprint: {definition_fingerprint(d)}")
        return
    with open_project_client(settings) as project:
        res = deploy_triage_agent(project, settings, force=args.force)
    verb = "Created" if res.created else "Unchanged (definition identical); current"
    print(f"{verb} agent '{res.agent_name}' version {res.version}")


def _smoke(args, settings: Settings) -> None:
    with open(args.file, encoding="utf-8") as fh:
        msg = EmailMessage.model_validate(json.load(fh))
    with open_project_client(settings) as project:
        client = FoundryTriageClient(project, settings)
        result = client.triage(msg, {"matched_by": None, "existing_request": None, "referenced_request_ids": []})
    print(result.model_dump_json(indent=2))
    problems = result.consistency_problems()
    if problems:
        print("\nconsistency problems:", problems)


def _build_pipeline(settings: Settings, project):
    from .graph import GraphClient, GraphMailbox, GraphPlanner
    from .pipeline import Pipeline
    from .ports import StaticArchitectDirectory
    from .repository import SqliteRepository, load_architects

    settings.require("graph_mailbox", "planner_plan_id")
    graph = GraphClient()
    mailbox = GraphMailbox(graph, settings.graph_mailbox, settings.graph_mail_folder)
    pipeline = Pipeline(
        repo=SqliteRepository(settings.db_path),
        triage=FoundryTriageClient(project, settings),
        planner=GraphPlanner(graph, settings.planner_plan_id, settings.planner_task_url_template),
        mailer=mailbox,
        architects=StaticArchitectDirectory(load_architects(settings.architect_matrix_path)),
        settings=settings,
        own_addresses={settings.graph_mailbox},
    )
    return pipeline, mailbox


def _run(args, settings: Settings) -> None:
    log = logging.getLogger("cis_planning")
    with open_project_client(settings) as project:
        pipeline, mailbox = _build_pipeline(settings, project)
        while True:
            for msg in mailbox.fetch_unread():
                outcome = pipeline.process_message(msg)
                log.info("message %s -> %s %s", msg.dedupe_key[:24], outcome.result, outcome.request_id or "")
                if outcome.completed:
                    mailbox.mark_read(msg)
            if args.once:
                return
            time.sleep(args.interval)


def _retry(args, settings: Settings) -> None:
    with open_project_client(settings) as project:
        pipeline, _ = _build_pipeline(settings, project)
        for outcome in pipeline.retry_exceptions():
            print(outcome.result, outcome.request_id, outcome.detail)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(prog="cis-planning")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("deploy-agent")
    d.add_argument("--dry-run", action="store_true", help="print the agent definition without calling Azure")
    d.add_argument("--force", action="store_true", help="create a new version even if unchanged")
    d.set_defaults(fn=_deploy)

    s = sub.add_parser("smoke-test")
    s.add_argument("file")
    s.set_defaults(fn=_smoke)

    r = sub.add_parser("run")
    r.add_argument("--once", action="store_true")
    r.add_argument("--interval", type=int, default=60)
    r.set_defaults(fn=_run)

    e = sub.add_parser("retry-exceptions")
    e.set_defaults(fn=_retry)

    args = p.parse_args(argv)
    args.fn(args, Settings.from_env())


if __name__ == "__main__":
    main()
