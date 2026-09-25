"""Change-compliance checks (CHG-02, CHG-05, CHG-06, CHG-09).

Matching a GitHub production deployment to a ServiceNow change uses, in order:
  1. deployment.payload.change_request (written by the deploy workflow), or
  2. a ServiceNow change whose correlation_display / description contains the workflow run URL/ID.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .base import Finding


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00").replace(" ", "T", 1)
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def business_days_between(start: datetime, end: datetime) -> int:
    d, n = start.date(), 0
    while d < end.date():
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def _run_ref(dep: dict) -> str:
    payload = dep.get("payload") or {}
    return str(payload.get("run_id") or payload.get("run_url") or "")


def reconcile_deployments(repo: str, deployments: list[dict], changes: list[dict], approvals: list[dict]) -> list[Finding]:
    """Every successful production deployment must map to a change that was approved before it ran."""
    by_number = {c["number"]: c for c in changes}
    approved_at: dict[str, datetime] = {}
    for a in approvals:
        if a.get("state") == "approved":
            t = _dt(a.get("sys_updated_on"))
            cid = a.get("document_id")
            if t and cid and (cid not in approved_at or t > approved_at[cid]):
                approved_at[cid] = t
    out: list[Finding] = []
    for d in deployments:
        statuses = [s.get("state") for s in d.get("statuses", [])]
        if "success" not in statuses:
            continue
        payload = d.get("payload") or {}
        chg = by_number.get(payload.get("change_request", ""))
        if chg is None:
            ref = _run_ref(d)
            chg = next((c for c in changes if ref and ref in (c.get("correlation_display", "") + c.get("description", ""))), None)
        subject = f"{repo}#deploy-{d['id']}"
        base = {"deployment_id": d["id"], "sha": d.get("sha"), "created_at": d.get("created_at"), "repo": repo}
        if chg is None:
            out.append(Finding("change.deploy_without_change", ["CHG-05", "CHG-06"], "FAIL", subject,
                               "Successful production deployment has no matching ServiceNow change", "critical", base))
            continue
        base["change"] = chg["number"]
        if chg.get("approval") != "approved":
            out.append(Finding("change.not_approved", ["CHG-06"], "FAIL", subject,
                               f"Change {chg['number']} approval state is '{chg.get('approval')}'", "high", base))
            continue
        t_dep, t_app = _dt(d.get("created_at")), approved_at.get(chg.get("sys_id", ""))
        if chg.get("type") != "emergency" and t_app and t_dep and t_app > t_dep:
            out.append(Finding("change.approved_after_deploy", ["CHG-06"], "FAIL", subject,
                               f"Change {chg['number']} approved after the deployment started (not an emergency change)", "high", base))
            continue
        out.append(Finding("change.reconciled", ["CHG-06"], "PASS", subject, f"Matched to {chg['number']}", "info", base))
    return out


def unmatched_changes(changes: list[dict], matched_numbers: set[str]) -> list[Finding]:
    out = []
    for c in changes:
        if c.get("type") in ("standard", "normal") and c["number"] not in matched_numbers and c.get("state") in ("3", "closed", "Closed"):
            if c.get("close_code") not in ("unsuccessful", "Unsuccessful", "cancelled"):
                out.append(Finding("change.without_deploy", ["CHG-06"], "INFO", c["number"],
                                   "Closed successful change with no matching production deployment in scope repos", "low",
                                   {"change": c["number"]}))
    return out


def pr_segregation(repo: str, prs: list[dict]) -> list[Finding]:
    """Merged PR must have an APPROVED review by someone other than the author and the last committer."""
    out = []
    for pr in prs:
        author = (pr.get("author") or {}).get("login")
        last = ((pr.get("commits") or {}).get("nodes") or [{}])[-1].get("commit", {})
        last_pushers = {((last.get("author") or {}).get("user") or {}).get("login"),
                        ((last.get("committer") or {}).get("user") or {}).get("login")} - {None, "web-flow"}
        approvers = {((r.get("author") or {}).get("login")) for r in (pr.get("reviews") or {}).get("nodes", [])} - {None}
        independent = approvers - {author} - last_pushers
        subject = f"{repo}#{pr['number']}"
        data = {"url": pr.get("url"), "author": author, "approvers": sorted(approvers), "merged_at": pr.get("mergedAt")}
        if not independent:
            out.append(Finding("pr.no_independent_approval", ["CHG-02"], "FAIL", subject,
                               "Merged without approval from someone other than the author/last pusher", "critical", data))
        elif any(a.endswith("[bot]") for a in independent) and len(independent) == 1:
            out.append(Finding("pr.bot_only_approval", ["CHG-02"], "FAIL", subject,
                               "Only approval came from a bot account", "critical", data))
        else:
            out.append(Finding("pr.independent_approval", ["CHG-02"], "PASS", subject, "Independent approval present", "info", data))
    return out


def change_sod(changes: list[dict], approvals: list[dict]) -> list[Finding]:
    by_sys = {c.get("sys_id"): c for c in changes}
    out = []
    for a in approvals:
        c = by_sys.get(a.get("document_id"))
        if c and a.get("state") == "approved" and a.get("approver") and a.get("approver") == c.get("requested_by"):
            out.append(Finding("change.self_approved", ["CHG-06", "CHG-02"], "FAIL", c["number"],
                               "Change approved by its requester", "critical", {"approver": a.get("approver")}))
    return out


def emergency_retro(changes: list[dict], approvals: list[dict], max_bd: int = 2, today: date | None = None) -> list[Finding]:
    appr: dict[str, list[datetime]] = {}
    for a in approvals:
        if a.get("state") == "approved" and _dt(a.get("sys_updated_on")):
            appr.setdefault(a["document_id"], []).append(_dt(a["sys_updated_on"]))
    now = datetime.combine(today or date.today(), datetime.min.time(), tzinfo=timezone.utc)
    out = []
    for c in changes:
        if c.get("type") != "emergency":
            continue
        impl = _dt(c.get("end_date") or c.get("start_date") or c.get("sys_created_on"))
        got = sorted(appr.get(c.get("sys_id", ""), []))
        if impl is None:
            continue
        if not got:
            late = business_days_between(impl, now) > max_bd
            out.append(Finding("change.emergency_retro", ["CHG-09"], "FAIL" if late else "INFO", c["number"],
                               "Emergency change has no retrospective approval" + (f" after {max_bd} business days" if late else " yet (within SLA)"),
                               "high" if late else "low"))
            continue
        bd = business_days_between(impl, got[-1])
        status = "PASS" if bd <= max_bd else "FAIL"
        out.append(Finding("change.emergency_retro", ["CHG-09"], status, c["number"],
                           f"Retrospective approval after {bd} business day(s)", "info" if status == "PASS" else "medium"))
    return out
