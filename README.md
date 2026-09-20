# CIS Planning Control Tower

Intake, triage, routing and tracking for CIS Planning requests, built on **Azure AI Foundry**.

The spec (`agent.md`) asks for a modular solution with AI doing understanding and code doing decisions. That maps to:

| Spec module | Where | AI or deterministic |
|---|---|---|
| AI Triage | Foundry prompt agent `cis-planning-triage` ([instructions](instructions/triage_agent.md), [foundry.py](src/cis_planning/foundry.py)) | AI, strict JSON-schema output |
| Email intake, Outlook send | [graph.py](src/cis_planning/graph.py) `GraphMailbox` | code |
| Correlation / duplicates | [correlation.py](src/cis_planning/correlation.py) | code |
| Request ID, records, audit, status history, comms log | [repository.py](src/cis_planning/repository.py) | code |
| Skill matching + architect assignment | [assignment.py](src/cis_planning/assignment.py) | code, explainable `AssignmentReason` |
| Lifecycle / state machine | [lifecycle.py](src/cis_planning/lifecycle.py) | code |
| Planner | [graph.py](src/cis_planning/graph.py) `GraphPlanner` | code |
| Requester / architect comms | [communications.py](src/cis_planning/communications.py) | fixed templates |
| Orchestration (MVP flow) | [pipeline.py](src/cis_planning/pipeline.py) | code |

Only one thing is a Foundry agent: the triage step. Request IDs, assignment, state changes and email are never model decisions.

## Deploy the agent to Foundry

Prerequisites: a Foundry project with a model deployment, `az login`, and the **Foundry User** role on the project.

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env            # set FOUNDRY_PROJECT_ENDPOINT and FOUNDRY_MODEL_DEPLOYMENT
set -a; source .env; set +a

.venv/bin/cis-planning deploy-agent --dry-run     # inspect the definition, no Azure call
.venv/bin/cis-planning deploy-agent               # create the agent version
.venv/bin/cis-planning smoke-test samples/emails/new_request_api_gateway.json
```

`deploy-agent` is idempotent: it stores a SHA-256 of the definition in the agent version metadata and only creates a new version when the instructions, model or schema changed (`--force` overrides).

Try the other two samples in `samples/emails/` too. The vague request should come back low-confidence or with missing information, and the prompt-injection sample should come back `requires_human_triage: true` with an untouched priority and no approval language. These are the behaviours to check on your real model deployment before trusting it.

## Run the workflow

```bash
cp config/architects.example.json config/architects.json   # your Architect Capability Matrix
# set GRAPH_MAILBOX and PLANNER_PLAN_ID in .env
.venv/bin/cis-planning run --once          # process unread mail once
.venv/bin/cis-planning run --interval 60   # poll
.venv/bin/cis-planning retry-exceptions    # re-drive AUTOMATION_EXCEPTION requests
```

The Planner plan needs buckets named exactly: New, Triaged, Assigned, In Assessment, Waiting for Requestor, Waiting for CIS, Review, Completed, On Hold.

Graph permissions for the runtime identity (managed identity in Azure): Mail.ReadWrite and Mail.Send scoped to the CIS mailbox (Exchange application access policy), Planner task read/write, and User.ReadBasic.All. Confirm Planner application-permission support in your tenant. The runtime identity also needs Foundry User on the project to call the agent.

## Tests

```bash
.venv/bin/python -m pytest -q     # 55 tests, no network
```

Covered: the end-to-end MVP flow, idempotent reprocessing, Planner/mail/Foundry outages with retry and no duplicate tasks or emails, low confidence and no-architect routing to human triage, correlation precedence, prompt-injection-shaped identity spoofing, override history, and the rule that neither AI nor automation can complete a request.

## Behaviour worth knowing

- **Email is untrusted input.** It is passed to the model as JSON string values, the instructions tell the model to ignore embedded instructions, and the schema limits what it can say. Beyond that, the code does not trust the model: the requester address is only accepted if it is a To/Cc/From participant, missing-information items are stripped of links before being emailed, and acknowledgements go to a verified address.
- **Request ID is allocated after triage**, not before. This avoids burning IDs on noise and satisfies "no record until duplicate checks finish". If triage fails, an ID is allocated immediately so the message is never lost (`AUTOMATION_EXCEPTION`, retried when the unread email is polled again).
- **Retry safety.** Each step checks the record or communication log first. A failed step parks the request in `AUTOMATION_EXCEPTION` with `failed_step`, `last_error` and `resume_status`; the source email stays unread until handling completes.
- **Assignment** requires one architect to cover *all* required skills (`CIS_ASSIGNMENT_MIN_COVERAGE=1.0`). Anything else goes to human triage. Order: explicit rule, continuity with related project requests, primary skills, secondary skills, utilisation, active projects, architect ID. Workload is derived from the request store.
- **Low confidence is never silently dropped**, including for messages classified as NOISE or INFORMATIONAL.

## Assumptions and gaps

Not verified against live services (no Azure tenant was available here): the Foundry calls, Graph mail/Planner calls and the strict-schema acceptance by your chosen model. The unit tests use fakes and `httpx.MockTransport`. Run the smoke test and a `--once` pass against a test mailbox and test plan first.

Choices I made where the spec is silent:

- Request ID format `CIS-YYYY-NNNNN`; `risk_indicator` uses LOW/MEDIUM/HIGH/CRITICAL; `request_type` is free text (the spec defines no list).
- The default model deployment is `gpt-5-mini`; no temperature is set because reasoning models reject it.
- The Planner task URL is a configurable template (`Settings.planner_task_url_template`); check it against your tenant.
- SQLite is the system of record for local runs and pilots. Production needs a `Repository` implementation on your approved store (Dataverse, Azure SQL, Cosmos DB) with an atomic ID sequence.

Not built yet (spec's secondary capabilities): SLA and escalation timers, KPI reporting, completed/closed/SLA notifications, Planner-to-CIS status sync when someone drags a card, attachment content analysis, and the conversational assistant from section 26. The state machine and audit/status-history data needed for KPIs are already captured.
