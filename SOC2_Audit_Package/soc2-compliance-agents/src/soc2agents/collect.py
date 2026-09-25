"""Deterministic phase: pull data, run checks, store raw evidence. No LLM involved.

Each collect_* function returns (findings, extras) and writes evidence with generated_by="deterministic".
This phase alone satisfies the evidence-collection controls; the agent phase only adds triage.
"""
from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .checks import access, change, evidence_qa, scm
from .checks.base import Finding
from .clients.github import GitHubClient
from .clients.servicenow import ServiceNowClient
from .evidence.store import EvidenceMeta, EvidenceStore

log = logging.getLogger(__name__)


def period_bounds(period: str) -> tuple[str, str]:
    """'2026-Q3' -> ('2026-07-01', '2026-09-30'); '2026-08' -> month bounds."""
    y, p = period.split("-")
    year = int(y)
    if p.upper().startswith("Q"):
        q = int(p[1:])
        sm = 3 * (q - 1) + 1
        em = sm + 2
    else:
        sm = em = int(p)
    start = date(year, sm, 1)
    end = date(year + (em == 12), (em % 12) + 1, 1)
    return start.isoformat(), date.fromordinal(end.toordinal() - 1).isoformat()


@dataclass
class Context:
    gh: GitHubClient
    store: EvidenceStore
    baseline: dict
    period: str
    sn: ServiceNowClient | None = None
    findings: list[Finding] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)

    def save(self, control: str, name: str, payload: Any, source: str, query: str, count: int | None) -> str:
        return self.store.put_artifact(name, payload, EvidenceMeta(control, self.period, source, "soc2agents", query, count))


def _repos(ctx: Context) -> list[str]:
    if "repos" not in ctx.extras:
        ctx.extras["repos"] = ctx.gh.scoped_repos(ctx.baseline["github"]["scope_property"])
    return ctx.extras["repos"]


# ---------------------------------------------------------------- drift (daily)
def collect_drift(ctx: Context) -> list[Finding]:
    f: list[Finding] = []
    rulesets = ctx.gh.org_rulesets()
    ctx.save("CHG-01", "org_rulesets", rulesets, "github", f"GET /orgs/{ctx.gh.org}/rulesets/{{id}}", len(rulesets))
    f += scm.check_default_branch_ruleset(rulesets, ctx.baseline)

    policy = ctx.gh.actions_permissions()
    ctx.save("CHG-11", "actions_policy", policy, "github", f"GET /orgs/{ctx.gh.org}/actions/permissions(+/workflow)", None)
    f.append(scm.check_actions_policy(policy, ctx.baseline))

    repos = _repos(ctx)
    ctx.save("GOV-08", "in_scope_repositories", repos, "github", "GET /orgs/{org}/properties/values soc2_scope=true", len(repos))
    envs = {}
    branch = ctx.baseline["github"]["default_branch"]
    for r in repos:
        f.append(scm.check_repo_effective_rules(r, ctx.gh.branch_rules(r, branch)))
        envs[r] = ctx.gh.environment(r, ctx.baseline["github"]["production_environment"]["name"])
        f.append(scm.check_production_environment(r, envs[r], ctx.baseline))
    ctx.save("CHG-05", "production_environments", envs, "github", "GET /repos/{o}/{r}/environments/production (+rules)", len(envs))
    ctx.save("MON-06", "drift_results", [x.to_dict() for x in f], "soc2agents", "checks.scm.*", len(f))
    ctx.findings += f
    return f


# ---------------------------------------------------------------- change (monthly / quarterly)
def collect_change(ctx: Context) -> list[Finding]:
    start, end = period_bounds(ctx.period)
    f: list[Finding] = []
    changes: list[dict] = []
    approvals: list[dict] = []
    if ctx.sn:
        q = f"sys_created_onBETWEEN{start}@{end} 23:59:59"
        changes = ctx.sn.query("change_request", q, ["sys_id", "number", "type", "state", "approval", "requested_by",
                                                       "start_date", "end_date", "close_code", "correlation_display",
                                                       "description", "sys_created_on"])
        approvals = ctx.sn.query("sysapproval_approver", f"source_table=change_request^{q}",
                                 ["document_id", "approver", "state", "sys_updated_on"])
        ctx.save("CHG-06", "servicenow_changes", {"changes": changes, "approvals": approvals}, "servicenow",
                 f"change_request + sysapproval_approver where {q}", len(changes))
        f += change.change_sod(changes, approvals)
        f += change.emergency_retro(changes, approvals, ctx.baseline["servicenow"]["change"]["emergency_retro_approval_business_days"])
    all_prs, all_deps, matched = {}, {}, set()
    for r in _repos(ctx):
        prs = ctx.gh.merged_prs(r, start, end, ctx.baseline["github"]["default_branch"])
        all_prs[r] = prs
        f += change.pr_segregation(r, prs)
        deps = ctx.gh.deployments(r, ctx.baseline["github"]["production_environment"]["name"], since=start)
        all_deps[r] = deps
        if ctx.sn:
            rec = change.reconcile_deployments(r, deps, changes, approvals)
            matched |= {x.data.get("change") for x in rec if x.data.get("change")}
            f += rec
    if ctx.sn:
        f += change.unmatched_changes(changes, matched)
    ctx.save("CHG-02", "merged_pull_requests", all_prs, "github", "GraphQL search is:pr is:merged base:main merged:<period>",
             sum(len(v) for v in all_prs.values()))
    ctx.save("CHG-05", "production_deployments", all_deps, "github", "GET /repos/{o}/{r}/deployments?environment=production",
             sum(len(v) for v in all_deps.values()))
    ctx.save("CHG-06", "deploy_change_reconciliation", [x.to_dict() for x in f if x.check.startswith("change.")],
             "soc2agents", "checks.change.reconcile_deployments", None)
    ctx.findings += f
    return f


# ---------------------------------------------------------------- access review (quarterly)
def collect_access(ctx: Context, roster_csv: str) -> list[Finding]:
    roster = access.load_roster(roster_csv)
    members, admins, outside = ctx.gh.members("all"), ctx.gh.members("admin"), ctx.gh.outside_collaborators()
    rows, f = access.github_packet(members, admins, outside, roster, ctx.baseline["access_review"]["max_github_org_owners"])
    if ctx.sn:
        roles = ctx.baseline["servicenow"]["privileged_roles"]
        rr = ctx.sn.query("sys_user_has_role", "role.nameIN" + ",".join(roles) + "^state=active", ["user", "role", "role.name"])
        for x in rr:
            x["role"] = x.get("role.name") or x.get("role")
        ids = sorted({x["user"] for x in rr if x.get("user")})
        users = {u["sys_id"]: u for u in (ctx.sn.query("sys_user", "sys_idIN" + ",".join(ids),
                 ["sys_id", "user_name", "email", "active", "last_login_time", "web_service_access_only"]) if ids else [])}
        sn_rows, sn_f = access.servicenow_packet(rr, users, roster, ctx.baseline["access_review"]["inactive_days"])
        rows += sn_rows
        f += sn_f
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["system", "account", "role", "email", "roster_status", "flags", "suggested_action",
                                        "reviewer_decision", "reviewer", "review_date"])
    w.writeheader()
    for row in rows:
        w.writerow(row)
    ctx.store.put_artifact("access_review_packet", buf.getvalue(),
                           EvidenceMeta("IAM-04", ctx.period, "github+servicenow", "soc2agents",
                                        "org members/admins/outside_collaborators; sys_user_has_role privileged", len(rows)), ext="csv")
    ctx.extras["access_rows"] = rows
    ctx.findings += f
    return f


# ---------------------------------------------------------------- evidence QA + PBC
def collect_evidence(ctx: Context, pbc_csv: str | None = None) -> list[Finding]:
    f = evidence_qa.qa_period(ctx.store, ctx.period, ctx.baseline.get("evidence_expectations", {}))
    ctx.save("GOV-05", "evidence_qa_results", [x.to_dict() for x in f], "soc2agents", "checks.evidence_qa.qa_period", len(f))
    if pbc_csv:
        rows = list(csv.DictReader(io.StringIO(pbc_csv)))
        mapping = evidence_qa.map_pbc(ctx.store, rows, ctx.period)
        ctx.save("GOV-05", "pbc_mapping", mapping, "soc2agents", "checks.evidence_qa.map_pbc", len(mapping))
        ctx.extras["pbc_mapping"] = mapping
    ctx.findings += f
    return f
