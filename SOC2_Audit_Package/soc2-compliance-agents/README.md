# soc2-compliance-agents

Read-only AI agents that automate SOC 2 Type II evidence and monitoring for **GitHub Enterprise Cloud, GitHub Actions and ServiceNow**. They are built on the Claude Agent SDK and run on a schedule in GitHub Actions.

| Agent | Schedule | Controls | What it does |
|---|---|---|---|
| `drift` | Daily | CHG-01, CHG-02, CHG-05, CHG-11, MON-06 | Checks org rulesets, per-repo effective rules, the Actions policy and `production` environments (ServiceNow gate, admin bypass) against `config/baseline.yaml` |
| `change` | Monthly + quarterly | CHG-02, CHG-05, CHG-06, CHG-09 | Reconciles GitHub production deployments to ServiceNow changes; PR segregation of duties; self-approved changes; emergency retrospective approvals |
| `access` | Quarterly | IAM-03, IAM-04, IAM-05, IAM-07 | Builds the access-review packet (GitHub org and ServiceNow privileged roles) and flags leavers, unknown identities, inactive admins and excess owners |
| `evidence` | Weekdays / fieldwork | GOV-05 | QA of stored evidence (coverage, metadata, SHA-256 integrity); maps auditor PBC requests to candidate evidence |

## How it works

Every run has two phases:

1. **Deterministic phase (no LLM).** Pulls data through read-only APIs, runs the checks in `src/soc2agents/checks/`, and writes raw exports and results to the write-once evidence store. This phase *is* the control evidence, and it always runs.
2. **Agent phase (Claude, advisory).** The agent reads the findings through a small set of in-process tools, groups them by root cause, opens or updates ServiceNow tickets, and stores a Markdown report labelled *agent-generated*. If the model is unavailable or over budget, the runner falls back to one ticket per FAIL finding and a template report. **Controls never depend on the LLM.**

## Guardrails (read-only + recommend)

- **No built-in tools.** `tools=[]` means no shell, no file system and no web. `setting_sources=[]` means local settings are ignored. Only `mcp__soc2__*` tools are allowed, and a `PreToolUse` hook denies anything else.
- **GitHub client is GET-only.** It also rejects GraphQL mutations. Use a GitHub App with read-only permissions.
- **ServiceNow writes are narrow.** The agents can only create or add a work note on one ticket table, with allowlisted fields, and only on records carrying their own `correlation_id` prefix. Changes, approvals, users and roles can never be written, whatever the configuration says.
- **Tickets are checked before they're opened.** They're allowed only for FAIL findings from the current run, capped at 15 per run, and idempotent (the same failure updates the same open ticket). Ticket text always includes the deterministic facts, so the model can't misstate them.
- **Source data is marked untrusted.** It's wrapped in `<data trust="untrusted">` to resist prompt injection from PR titles, commits and ticket text.
- **Runs are bounded.** Each run has `max_turns`, a `max_budget_usd` limit, a pinned model (`AGENT_MODEL`) and structured output.
- **Every tool call is audited.** Calls are hashed and stored with the run record (`AUT-03`), and the evidence store keeps a SHA-256 manifest per run.

## Setup

1. **GitHub App (read-only).** Grant: Administration read, Members read, Metadata, Actions read, Deployments read, Environments read, Custom properties read, Security events read. Install it on the org. Save `COMPLIANCE_APP_ID` (variable) and `COMPLIANCE_APP_PRIVATE_KEY` (secret).
2. **ServiceNow integration user.** Set it to web-service access only. It needs read ACLs on `change_request`, `sysapproval_approver`, `sc_req_item`, `sc_task`, `incident`, `sys_user`, `sys_user_has_role`, `sys_user_grmember` and `task_sla`, and create/update on the ticket table only. Save `SN_INSTANCE` and `SN_AGENT_USER` (variables) and `SN_AGENT_PASSWORD` (secret). An OAuth token via `SN_TOKEN` is also supported.
3. **Evidence bucket.** Create an S3 bucket with Object Lock in compliance mode. `EVIDENCE_WRITER_ROLE_ARN` must be write-only (no delete) and trusted only for this repo's `compliance-agents` environment through GitHub OIDC. Save `EVIDENCE_URI` (for example `s3://acme-soc2-evidence/prod`) and `INPUTS_URI` (the location of `roster.csv` and `pbc.csv`).
4. **Model access.** Set `ANTHROPIC_API_KEY` as an environment secret, or use Amazon Bedrock or Google Vertex through OIDC. Also set `AGENT_MODEL` to a pinned model ID and `AGENT_MAX_BUDGET_USD` (for example `2`).
5. **Environment `compliance-agents`.** Hold all of the above here. Set deployment branches to `main` only, and add required reviewers if you want human approval before live runs.
6. **Pin actions.** Replace the tag references in `.github/workflows/` with full commit SHAs (for example with `pinact run`). Your org's SHA-pinning policy blocks tags.
7. **Identity roster.** Until the IdP adapter is chosen (see REQ-001), export `roster.csv` (`email,github_login,status,termination_date,department`) from the HRIS or IdP to `INPUTS_URI`.
8. **Dry run first.** Run each workflow manually with `live: false`, review the reports, then enable the schedules.

## Local use

```bash
pip install -e .[dev]
pytest -q                                   # 25 tests, fake GitHub/ServiceNow APIs
export GH_ORG=acme GH_TOKEN=... EVIDENCE_URI=file://./evidence-out
soc2agents run drift --no-llm               # deterministic only, dry run
soc2agents run change --period 2026-Q3      # with the agent (needs model credentials)
```

## Extending

When the remaining subsystems are chosen (REQ-001), add a client in `clients/`, checks in `checks/`, a `collect_*` function and a prompt, then register the agent in `runner.AGENTS`. Keep the same pattern: deterministic checks decide, and the agent explains.
