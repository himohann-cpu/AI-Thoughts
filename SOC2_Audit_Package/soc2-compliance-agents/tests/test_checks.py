from datetime import date, datetime, timezone

from conftest import BAD_ENV, GOOD_ENV, GOOD_ENV_RULES, PRS, ROSTER, FakeServiceNow, good_ruleset

from soc2agents.checks import access, change, scm


# ---------------- drift ----------------
def test_compliant_ruleset_passes(baseline):
    [f] = scm.check_default_branch_ruleset([good_ruleset()], baseline)
    assert f.status == "PASS"


def test_ruleset_bypass_and_weak_pr_rule_fail(baseline):
    rs = good_ruleset(bypass_actors=[{"actor_type": "OrganizationAdmin", "actor_id": 1}])
    rs["rules"][2]["parameters"]["require_last_push_approval"] = False
    [f] = scm.check_default_branch_ruleset([rs], baseline)
    assert f.status == "FAIL" and "bypass list not empty" in f.detail and "require_last_push_approval" in f.detail


def test_missing_ruleset_is_critical(baseline):
    [f] = scm.check_default_branch_ruleset([], baseline)
    assert f.status == "FAIL" and f.severity == "critical"


def test_actions_can_approve_prs_is_critical(baseline):
    f = scm.check_actions_policy({"permissions": {"allowed_actions": "all", "sha_pinning_required": False},
                                  "workflow": {"default_workflow_permissions": "write", "can_approve_pull_request_reviews": True}}, baseline)
    assert f.status == "FAIL" and f.severity == "critical" and len(f.data["problems"]) == 4


def test_production_environment(baseline):
    good = scm.check_production_environment("svc", dict(GOOD_ENV, custom_rules=GOOD_ENV_RULES), baseline)
    bad = scm.check_production_environment("svc", dict(BAD_ENV, custom_rules={"custom_deployment_protection_rules": []}), baseline)
    none = scm.check_production_environment("svc", None, baseline)
    assert good.status == "PASS"
    assert bad.status == "FAIL" and bad.severity == "critical" and len(bad.data["problems"]) == 3
    assert none.status == "FAIL"


def test_finding_id_is_stable():
    a = scm.check_repo_effective_rules("r", [])
    b = scm.check_repo_effective_rules("r", [])
    assert a.finding_id == b.finding_id


# ---------------- change ----------------
def test_pr_segregation():
    res = {f.subject: f for f in change.pr_segregation("payments-api", PRS["payments-api"]) + change.pr_segregation("legacy-billing", PRS["legacy-billing"])}
    assert res["payments-api#101"].status == "PASS"
    assert res["payments-api#102"].check == "pr.no_independent_approval"
    assert res["legacy-billing#7"].check == "pr.bot_only_approval"


def test_reconcile_and_sod():
    sn = FakeServiceNow().tables
    deps = [{"id": 1, "sha": "a", "created_at": "2026-08-02T12:00:00Z", "payload": {"change_request": "CHG0001"}, "statuses": [{"state": "success"}]},
            {"id": 2, "sha": "b", "created_at": "2026-08-05T12:00:00Z", "payload": {}, "statuses": [{"state": "success"}]},
            {"id": 3, "sha": "c", "created_at": "2026-08-05T13:00:00Z", "payload": {}, "statuses": [{"state": "failure"}]}]
    res = change.reconcile_deployments("payments-api", deps, sn["change_request"], sn["sysapproval_approver"])
    checks = sorted(f.check for f in res)
    assert checks == ["change.deploy_without_change", "change.reconciled"]
    sod = change.change_sod(sn["change_request"], sn["sysapproval_approver"])
    assert [f.subject for f in sod] == ["CHG0003"]


def test_approved_after_deploy():
    chg = [{"sys_id": "x", "number": "CHG9", "type": "normal", "approval": "approved"}]
    appr = [{"document_id": "x", "state": "approved", "sys_updated_on": "2026-08-02 13:00:00"}]
    deps = [{"id": 9, "created_at": "2026-08-02T12:00:00Z", "payload": {"change_request": "CHG9"}, "statuses": [{"state": "success"}]}]
    [f] = change.reconcile_deployments("r", deps, chg, appr)
    assert f.check == "change.approved_after_deploy"


def test_emergency_retro_approval_business_days():
    sn = FakeServiceNow().tables
    res = change.emergency_retro(sn["change_request"], sn["sysapproval_approver"], 2, today=date(2026, 9, 1))
    [f] = res
    # implemented Thu 6 Aug, approved Wed 12 Aug = 4 business days -> FAIL
    assert f.status == "FAIL" and "4 business" in f.detail
    assert change.business_days_between(datetime(2026, 8, 7, tzinfo=timezone.utc), datetime(2026, 8, 10, tzinfo=timezone.utc)) == 1


# ---------------- access ----------------
def test_github_access_packet():
    roster = access.load_roster(ROSTER)
    rows, findings = access.github_packet([{"login": "alice"}, {"login": "mallory"}, {"login": "zed"}],
                                          [{"login": "alice"}, {"login": "bob"}, {"login": "x"}, {"login": "y"}],
                                          [{"login": "ext"}], roster, max_owners=3)
    checks = sorted(f.check for f in findings)
    assert checks == ["access.leaver_active", "access.outside_collaborator", "access.too_many_owners", "access.unknown_identity"]
    assert {r["account"]: r["suggested_action"] for r in rows}["mallory"] == "Remove"


def test_servicenow_packet_flags_inactive_and_leaver():
    roster = access.load_roster(ROSTER)
    sn = FakeServiceNow().tables
    users = {u["sys_id"]: u for u in sn["sys_user"]}
    rr = [dict(x, role=x["role.name"]) for x in sn["sys_user_has_role"]]
    rows, findings = access.servicenow_packet(rr, users, roster, 90, now=datetime(2026, 9, 24, tzinfo=timezone.utc))
    assert [f.subject for f in findings] == ["ServiceNow:oldie"]
    oldie = next(r for r in rows if r["account"] == "oldie")
    assert "terminated" in oldie["flags"] and "no login" in oldie["flags"] and oldie["suggested_action"] == "Remove"
