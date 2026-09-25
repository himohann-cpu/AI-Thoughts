"""Control-drift checks for GitHub and GitHub Actions (CHG-01, CHG-02, CHG-05, CHG-11, MON-06)."""
from __future__ import annotations

from .base import Finding


def check_default_branch_ruleset(ruleset_list: list[dict], baseline: dict) -> list[Finding]:
    b = baseline["github"]["default_branch_ruleset"]
    ctl = ["CHG-01", "CHG-02", "CHG-03", "CHG-04", "MON-06"]
    rs = next((r for r in ruleset_list if r.get("name") == b["name"]), None)
    if rs is None:
        return [Finding("ruleset.present", ctl, "FAIL", b["name"], "Required organization ruleset not found", "critical")]
    out: list[Finding] = []
    problems = []
    if rs.get("enforcement") != b["enforcement"]:
        problems.append(f"enforcement is '{rs.get('enforcement')}', expected '{b['enforcement']}'")
    if rs.get("bypass_actors"):
        actors = [f"{a.get('actor_type')}:{a.get('actor_id')}" for a in rs["bypass_actors"]]
        problems.append(f"bypass list not empty: {actors}")
    rules = {r["type"]: r.get("parameters", {}) for r in rs.get("rules", [])}
    for req in b["required_rules"]:
        if req not in rules:
            problems.append(f"missing rule '{req}'")
    pr = rules.get("pull_request", {})
    exp = b["pull_request"]
    if pr:
        if pr.get("required_approving_review_count", 0) < exp["required_approving_review_count_min"]:
            problems.append("required_approving_review_count below minimum")
        for k in ("dismiss_stale_reviews_on_push", "require_code_owner_review", "require_last_push_approval"):
            if pr.get(k) is not exp[k]:
                problems.append(f"pull_request.{k} is {pr.get(k)}, expected {exp[k]}")
    checks = {c["context"] for c in rules.get("required_status_checks", {}).get("required_status_checks", [])}
    missing = [c for c in b["required_status_checks"] if c not in checks]
    if missing:
        problems.append(f"required status checks missing: {missing}")
    cond = rs.get("conditions", {}).get("repository_property", {}).get("include", [])
    if not any(c.get("name") == baseline["github"]["scope_property"] and "true" in c.get("property_values", []) for c in cond):
        problems.append("ruleset does not target repositories with soc2_scope=true")
    if problems:
        out.append(Finding("ruleset.config", ctl, "FAIL", b["name"], "; ".join(problems), "high",
                           {"ruleset_id": rs.get("id"), "problems": problems}))
    else:
        out.append(Finding("ruleset.config", ctl, "PASS", b["name"], "Ruleset matches baseline", "info",
                           {"ruleset_id": rs.get("id")}))
    return out


def check_repo_effective_rules(repo: str, branch_rules: list[dict]) -> Finding:
    types = {r.get("type") for r in branch_rules}
    need = {"pull_request", "required_status_checks", "non_fast_forward", "deletion"}
    missing = sorted(need - types)
    if missing:
        return Finding("repo.effective_rules", ["CHG-01", "GOV-08"], "FAIL", repo,
                       f"Default branch is missing effective rules {missing} (is soc2_scope set?)", "high",
                       {"effective_rule_types": sorted(t for t in types if t)})
    return Finding("repo.effective_rules", ["CHG-01"], "PASS", repo, "Effective rules present", "info")


def check_actions_policy(policy: dict, baseline: dict) -> Finding:
    b = baseline["github"]["actions"]
    p, w = policy.get("permissions", {}), policy.get("workflow", {})
    problems = []
    if p.get("allowed_actions") not in b["allowed_actions"]:
        problems.append(f"allowed_actions is '{p.get('allowed_actions')}'")
    if b["sha_pinning_required"] and p.get("sha_pinning_required") is not True:
        problems.append("SHA pinning is not required")
    if w.get("default_workflow_permissions") != b["default_workflow_permissions"]:
        problems.append(f"default GITHUB_TOKEN permissions are '{w.get('default_workflow_permissions')}'")
    if w.get("can_approve_pull_request_reviews") is not b["can_approve_pull_request_reviews"]:
        problems.append("GitHub Actions is allowed to approve pull requests (defeats CHG-02)")
    ctl = ["CHG-11", "CHG-02", "MON-06"]
    if problems:
        sev = "critical" if any("approve" in x for x in problems) else "high"
        return Finding("actions.policy", ctl, "FAIL", "org-actions-policy", "; ".join(problems), sev, {"problems": problems})
    return Finding("actions.policy", ctl, "PASS", "org-actions-policy", "Actions policy matches baseline", "info")


def check_production_environment(repo: str, env: dict | None, baseline: dict) -> Finding:
    b = baseline["github"]["production_environment"]
    ctl = ["CHG-05", "CHG-06", "MON-06"]
    if env is None:
        return Finding("env.production", ctl, "FAIL", repo, f"No '{b['name']}' environment defined", "high")
    problems = []
    rules = env.get("custom_rules", {}).get("custom_deployment_protection_rules", [])
    sn = [r for r in rules if b["servicenow_app_slug_contains"] in (r.get("app", {}).get("slug", "")).lower()]
    if not sn or not any(r.get("enabled", True) for r in sn):
        problems.append("ServiceNow deployment protection rule missing or disabled")
    if env.get("can_admins_bypass") is not b["can_admins_bypass"]:
        problems.append("administrators can bypass protection rules")
    if b["require_branch_policy"] and not env.get("deployment_branch_policy"):
        problems.append("no deployment branch policy (any branch can deploy)")
    for pr in env.get("protection_rules", []):
        if pr.get("type") == "required_reviewers" and pr.get("prevent_self_review") is False:
            problems.append("required reviewers allow self-review")
    if problems:
        return Finding("env.production", ctl, "FAIL", repo, "; ".join(problems), "critical" if any("ServiceNow" in x for x in problems) else "high",
                       {"problems": problems})
    return Finding("env.production", ctl, "PASS", repo, "Production environment matches baseline", "info")
