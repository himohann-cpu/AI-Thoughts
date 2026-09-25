"""CLI entry point.

  soc2agents run drift    [--period 2026-Q3]
  soc2agents run change   --period 2026-Q3
  soc2agents run access   --period 2026-Q3 --roster roster.csv
  soc2agents run evidence --period 2026-Q3 [--pbc pbc.csv]

Flags: --no-llm (deterministic only; also the automatic fallback if the model call fails),
       --live (send tickets; otherwise dry run), --fail-on {critical,high,none}.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

import yaml

from .checks.base import Finding, summarize
from .clients.github import GitHubClient
from .clients.servicenow import ServiceNowClient
from .collect import Context, collect_access, collect_change, collect_drift, collect_evidence
from .evidence.store import EvidenceMeta, EvidenceStore
from .settings import Settings
from .tools import RunState, build_server, qualified

log = logging.getLogger("soc2agents")

SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


@dataclass(frozen=True)
class AgentSpec:
    name: str
    prompt: str
    tools: frozenset[str]
    report_control: str
    group_key: str
    collect: Callable[..., list[Finding]]
    max_turns: int = 30


AGENTS = {
    "drift": AgentSpec("drift", "drift.md", frozenset({"list_findings", "get_finding", "list_evidence", "propose_ticket", "submit_report"}),
                       "MON-06", "drift", lambda ctx, a: collect_drift(ctx)),
    "change": AgentSpec("change", "change.md", frozenset({"list_findings", "get_finding", "list_evidence", "propose_ticket", "submit_report"}),
                        "CHG-06", "change", lambda ctx, a: collect_change(ctx)),
    "access": AgentSpec("access", "access.md", frozenset({"list_findings", "get_finding", "get_access_rows", "propose_ticket", "submit_report"}),
                        "IAM-04", "access", lambda ctx, a: collect_access(ctx, Path(a.roster).read_text())),
    "evidence": AgentSpec("evidence", "evidence.md", frozenset({"list_findings", "get_finding", "list_evidence", "get_pbc_mapping",
                                                                "propose_ticket", "submit_report"}),
                          "GOV-05", "evidence", lambda ctx, a: collect_evidence(ctx, Path(a.pbc).read_text() if a.pbc else None)),
}

OUTPUT_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "overall_status": {"type": "string", "enum": ["green", "amber", "red"]},
            "summary": {"type": "string"},
            "priorities": {"type": "array", "items": {"type": "object", "properties": {
                "finding_ids": {"type": "array", "items": {"type": "string"}},
                "severity": {"type": "string"}, "root_cause": {"type": "string"}, "human_action": {"type": "string"}},
                "required": ["finding_ids", "severity", "human_action"]}},
        },
        "required": ["overall_status", "summary", "priorities"],
    },
}


def current_quarter(d: date | None = None) -> str:
    d = d or date.today()
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def deterministic_report(spec: AgentSpec, ctx: Context) -> str:
    fails = sorted([f for f in ctx.findings if f.status in ("FAIL", "ERROR")], key=lambda f: SEV_ORDER[f.severity])
    lines = [f"# {spec.name} run – {ctx.period}", "", f"Counts: {summarize(ctx.findings)}", "", "## Action required", ""]
    lines += [f"- **{f.severity.upper()}** `{f.check}` {f.subject} ({', '.join(f.control_ids)}): {f.detail}" for f in fails] or ["None."]
    return "\n".join(lines)


def open_deterministic_tickets(ctx: Context, state: RunState) -> None:
    """Fallback when no LLM: one ticket per FAIL finding (still idempotent via correlation id)."""
    from .clients.servicenow import CORRELATION_PREFIX
    for f in [x for x in ctx.findings if x.status == "FAIL"]:
        corr = CORRELATION_PREFIX + f.finding_id
        fields = {"short_description": f"[SOC2][{state.agent}] {f.check} – {f.subject}"[:160],
                  "description": f"{f.detail}\nControls: {', '.join(f.control_ids)}\nRun: {ctx.store.run_id}",
                  "urgency": "1" if f.severity == "critical" else "2", "impact": "2",
                  "assignment_group": state.assignment_group, "category": state.category}
        rec = {"correlation_id": corr, "finding_ids": [f.finding_id]}
        if state.dry_run or ctx.sn is None:
            rec["action"] = "dry-run (not sent)"
        else:
            rec["action"] = ctx.sn.upsert_ticket(corr, fields)["action"]
        state.tickets.append(rec)


async def run_agent(spec: AgentSpec, ctx: Context, state: RunState, settings: Settings) -> dict:
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    from .guardrails import build_hooks

    server = build_server(ctx, state, set(spec.tools))
    allowed = qualified(set(spec.tools))
    prompt_dir = settings.prompts_dir
    system_prompt = (prompt_dir / "_common.md").read_text() + "\n\n" + (prompt_dir / spec.prompt).read_text()
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        tools=[],                       # no built-in tools: no shell, no file system, no web
        mcp_servers={"soc2": server},
        allowed_tools=allowed,
        disallowed_tools=["Bash", "Write", "Edit", "Read", "WebFetch", "WebSearch", "Agent"],
        hooks=build_hooks(state, allowed),
        setting_sources=[],             # ignore any local settings/CLAUDE.md on the runner
        max_turns=spec.max_turns,
        max_budget_usd=settings.max_budget_usd,
        model=settings.model,
        output_format=OUTPUT_SCHEMA,
    )
    task = (f"Period: {ctx.period}. Run id: {ctx.store.run_id}. Dry run: {state.dry_run}. "
            f"There are {len(ctx.findings)} findings ({summarize(ctx.findings)}). Triage them, open tickets for FAIL "
            f"findings grouped by root cause, submit the report, then return the structured summary.")
    result: dict = {}
    async for msg in query(prompt=task, options=options):
        if isinstance(msg, ResultMessage):
            result = {"structured": msg.structured_output, "cost_usd": msg.total_cost_usd, "turns": msg.num_turns,
                      "is_error": msg.is_error, "subtype": msg.subtype}
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="soc2agents")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("agent", choices=sorted(AGENTS))
    r.add_argument("--period", default=None, help="e.g. 2026-Q3 or 2026-08 (default: current quarter)")
    r.add_argument("--roster", help="identity roster CSV (access agent)")
    r.add_argument("--pbc", help="PBC list CSV exported from the workbook (evidence agent)")
    r.add_argument("--no-llm", action="store_true")
    r.add_argument("--live", action="store_true", help="send ServiceNow tickets (default is dry run)")
    r.add_argument("--fail-on", choices=["critical", "high", "none"], default="critical")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    settings = Settings.from_env()
    spec = AGENTS[args.agent]
    if spec.name == "access" and not args.roster:
        ap.error("--roster is required for the access agent")
    baseline = yaml.safe_load((settings.config_dir / "baseline.yaml").read_text())
    run_id = f"{spec.name}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    store = EvidenceStore(settings.evidence_uri, run_id)
    gh = GitHubClient(settings.github_org, settings.github_token, settings.github_api)
    sn = None
    if settings.servicenow_instance:
        sn = ServiceNowClient(settings.servicenow_instance, settings.servicenow_user, settings.servicenow_password,
                              settings.servicenow_token, ticket_table=baseline["servicenow"]["ticket_table"])
    ctx = Context(gh=gh, store=store, baseline=baseline, period=args.period or current_quarter(), sn=sn)
    dry_run = settings.dry_run and not args.live
    state = RunState(spec.name, dry_run, spec.report_control, baseline["servicenow"]["assignment_groups"][spec.group_key],
                     baseline["servicenow"].get("category", "compliance"))

    # Phase 1 – deterministic (always runs; this is the control evidence)
    spec.collect(ctx, args)

    # Phase 2 – agent triage (advisory). Falls back to deterministic tickets/report on any failure.
    agent_result: dict = {"mode": "no-llm"}
    if not args.no_llm:
        try:
            agent_result = asyncio.run(run_agent(spec, ctx, state, settings))
            agent_result["mode"] = "agent"
        except Exception as exc:  # model outage, budget, auth – controls must not depend on the LLM
            log.error("Agent phase failed (%s); falling back to deterministic mode", exc)
            agent_result = {"mode": "fallback", "error": str(exc)}
    if agent_result["mode"] != "agent" or agent_result.get("is_error"):
        if not state.tickets:
            open_deterministic_tickets(ctx, state)
        if not state.report_key:
            state.report_key = store.put_artifact(f"{spec.name}_report", deterministic_report(spec, ctx),
                                                  EvidenceMeta(spec.report_control, ctx.period, "soc2agents", run_id,
                                                               "deterministic report", len(ctx.findings)), ext="md")

    # Audit trail of the agent's tool use + run manifest
    run_record = {"run_id": run_id, "agent": spec.name, "period": ctx.period, "dry_run": dry_run,
                  "model": settings.model, "findings": summarize(ctx.findings), "tickets": state.tickets,
                  "report": state.report_key, "agent_result": agent_result, "tool_audit": state.audit}
    store.put_artifact(f"{spec.name}_run_record", run_record,
                       EvidenceMeta("AUT-03", ctx.period, "soc2agents", run_id, "agent run record + tool audit", len(state.audit)))
    manifest = store.write_manifest()
    print(json.dumps({k: run_record[k] for k in ("run_id", "agent", "period", "dry_run", "findings")} |
                     {"tickets": len(state.tickets), "report": state.report_key, "manifest": manifest,
                      "mode": agent_result.get("mode")}, indent=2))

    worst = min((SEV_ORDER[f.severity] for f in ctx.findings if f.status == "FAIL"), default=99)
    threshold = {"critical": 0, "high": 1, "none": -1}[args.fail_on]
    return 1 if worst <= threshold else 0


if __name__ == "__main__":
    sys.exit(main())
