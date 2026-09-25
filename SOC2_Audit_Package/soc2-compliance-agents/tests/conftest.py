"""Fake GitHub and ServiceNow APIs (httpx.MockTransport) and shared fixtures."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ORG = "acme"


@pytest.fixture
def baseline():
    return yaml.safe_load((ROOT / "config" / "baseline.yaml").read_text())


def good_ruleset(**over):
    rs = {
        "id": 42, "name": "soc2-default-branch-protection", "enforcement": "active", "bypass_actors": [],
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"]},
                       "repository_property": {"include": [{"name": "soc2_scope", "property_values": ["true"]}]}},
        "rules": [
            {"type": "deletion"}, {"type": "non_fast_forward"},
            {"type": "pull_request", "parameters": {"required_approving_review_count": 1, "dismiss_stale_reviews_on_push": True,
                                                    "require_code_owner_review": True, "require_last_push_approval": True}},
            {"type": "required_status_checks", "parameters": {"required_status_checks": [
                {"context": "ci / test"}, {"context": "ci / dependency-review"}, {"context": "CodeQL"}]}},
            {"type": "code_scanning", "parameters": {}},
        ],
    }
    rs.update(over)
    return rs


GOOD_ENV = {"name": "production", "can_admins_bypass": False,
            "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
            "protection_rules": [{"type": "required_reviewers", "prevent_self_review": True}]}
GOOD_ENV_RULES = {"total_count": 1, "custom_deployment_protection_rules": [
    {"id": 1, "enabled": True, "app": {"slug": "servicenow-devops-change"}}]}
BAD_ENV = {"name": "production", "can_admins_bypass": True, "deployment_branch_policy": None, "protection_rules": []}

PRS = {"payments-api": [
    {"number": 101, "url": "https://github.com/acme/payments-api/pull/101", "mergedAt": "2026-08-02T10:00:00Z",
     "author": {"login": "alice"}, "commits": {"nodes": [{"commit": {"author": {"user": {"login": "alice"}},
                                                                     "committer": {"user": {"login": "web-flow"}}}}]},
     "reviews": {"nodes": [{"author": {"login": "bob"}}]}},
    {"number": 102, "url": "https://github.com/acme/payments-api/pull/102", "mergedAt": "2026-08-03T10:00:00Z",
     "author": {"login": "carol"}, "commits": {"nodes": [{"commit": {"author": {"user": {"login": "carol"}}}}]},
     "reviews": {"nodes": [{"author": {"login": "carol"}}]}},
], "legacy-billing": [
    {"number": 7, "url": "u", "mergedAt": "2026-08-04T10:00:00Z", "author": {"login": "dave"},
     "commits": {"nodes": [{"commit": {"author": {"user": {"login": "dave"}}}}]},
     "reviews": {"nodes": [{"author": {"login": "renovate[bot]"}}]}},
]}

DEPLOYS = {"payments-api": [
    {"id": 1, "sha": "abc", "created_at": "2026-08-02T12:00:00Z", "payload": {"change_request": "CHG0001"}},
    {"id": 2, "sha": "def", "created_at": "2026-08-05T12:00:00Z", "payload": {}},
], "legacy-billing": []}
DEPLOY_STATUSES = {1: [{"state": "success"}], 2: [{"state": "success"}]}


def github_transport(ruleset=None, actions=None, envs=None):
    ruleset = ruleset or good_ruleset()
    actions = actions or {"permissions": {"allowed_actions": "selected", "sha_pinning_required": True},
                          "workflow": {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False}}
    envs = envs if envs is not None else {"payments-api": (GOOD_ENV, GOOD_ENV_RULES), "legacy-billing": (BAD_ENV, {"custom_deployment_protection_rules": []})}

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if req.method == "POST" and p == "/graphql":
            q = json.loads(req.content)["variables"]["q"]
            repo = q.split(f"repo:{ORG}/")[1].split()[0]
            return httpx.Response(200, json={"data": {"search": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                                                 "nodes": PRS.get(repo, [])}}})
        if req.method != "GET":
            return httpx.Response(405)
        routes = {
            f"/orgs/{ORG}/properties/values": [
                {"repository_name": "payments-api", "properties": [{"property_name": "soc2_scope", "value": "true"}]},
                {"repository_name": "legacy-billing", "properties": [{"property_name": "soc2_scope", "value": "true"}]},
                {"repository_name": "docs-site", "properties": [{"property_name": "soc2_scope", "value": "false"}]}],
            f"/orgs/{ORG}/rulesets": [{"id": ruleset["id"], "name": ruleset["name"]}],
            f"/orgs/{ORG}/rulesets/{ruleset['id']}": ruleset,
            f"/orgs/{ORG}/actions/permissions": actions["permissions"],
            f"/orgs/{ORG}/actions/permissions/workflow": actions["workflow"],
            f"/repos/{ORG}/payments-api/rules/branches/main": [{"type": t} for t in
                                                               ("pull_request", "required_status_checks", "non_fast_forward", "deletion")],
            f"/repos/{ORG}/legacy-billing/rules/branches/main": [{"type": "deletion"}],
            f"/orgs/{ORG}/members": [{"login": "alice"}, {"login": "bob"}, {"login": "mallory"}, {"login": "zed"}],
            f"/orgs/{ORG}/outside_collaborators": [{"login": "contractor1"}],
        }
        if p == f"/orgs/{ORG}/members" and req.url.params.get("role") == "admin":
            return httpx.Response(200, json=[{"login": "alice"}])
        for repo, (env, rules) in envs.items():
            routes[f"/repos/{ORG}/{repo}/environments/production"] = env
            routes[f"/repos/{ORG}/{repo}/environments/production/deployment_protection_rules"] = rules
        for repo, deps in DEPLOYS.items():
            routes[f"/repos/{ORG}/{repo}/deployments"] = deps
            for d in deps:
                routes[f"/repos/{ORG}/{repo}/deployments/{d['id']}/statuses"] = DEPLOY_STATUSES[d["id"]]
        if p in routes and routes[p] is not None:
            return httpx.Response(200, json=routes[p])
        return httpx.Response(404, json={"message": "Not Found"})

    return httpx.MockTransport(handler)


class FakeServiceNow:
    def __init__(self):
        self.tables = {
            "change_request": [
                {"sys_id": "c1", "number": "CHG0001", "type": "standard", "state": "3", "approval": "approved",
                 "requested_by": "u-alice", "close_code": "successful", "correlation_display": "", "description": "",
                 "sys_created_on": "2026-08-02 11:50:00", "start_date": "2026-08-02 12:00:00", "end_date": "2026-08-02 12:10:00"},
                {"sys_id": "c2", "number": "CHG0002", "type": "emergency", "state": "3", "approval": "approved",
                 "requested_by": "u-bob", "close_code": "successful", "correlation_display": "", "description": "",
                 "sys_created_on": "2026-08-06 09:00:00", "start_date": "2026-08-06 09:00:00", "end_date": "2026-08-06 09:30:00"},
                {"sys_id": "c3", "number": "CHG0003", "type": "normal", "state": "3", "approval": "approved",
                 "requested_by": "u-carol", "close_code": "successful", "correlation_display": "", "description": "",
                 "sys_created_on": "2026-08-07 09:00:00"},
            ],
            "sysapproval_approver": [
                {"document_id": "c1", "approver": "u-policy", "state": "approved", "sys_updated_on": "2026-08-02 11:55:00"},
                {"document_id": "c2", "approver": "u-cab", "state": "approved", "sys_updated_on": "2026-08-12 09:00:00"},
                {"document_id": "c3", "approver": "u-carol", "state": "approved", "sys_updated_on": "2026-08-07 10:00:00"},
            ],
            "sys_user_has_role": [{"user": "u-alice", "role.name": "admin"}, {"user": "u-old", "role.name": "change_manager"}],
            "sys_user": [
                {"sys_id": "u-alice", "user_name": "alice", "email": "alice@acme.com", "active": "true",
                 "last_login_time": "2026-09-20 10:00:00", "web_service_access_only": "false"},
                {"sys_id": "u-old", "user_name": "oldie", "email": "oldie@acme.com", "active": "true",
                 "last_login_time": "2026-01-01 10:00:00", "web_service_access_only": "false"}],
            "incident": [],
        }
        self.posts, self.patches = [], []

    def transport(self):
        def handler(req: httpx.Request) -> httpx.Response:
            table = req.url.path.split("/api/now/table/")[1].split("/")[0]
            if req.method == "GET":
                q = req.url.params.get("sysparm_query", "")
                rows = self.tables.get(table, [])
                if table == "incident" and "correlation_id=" in q:
                    cid = q.split("correlation_id=")[1].split("^")[0]
                    rows = [r for r in rows if r.get("correlation_id") == cid and r.get("active", "true") == "true"]
                return httpx.Response(200, json={"result": rows})
            if req.method == "POST":
                body = json.loads(req.content)
                rec = dict(body, sys_id=f"i{len(self.posts) + 1}", number=f"INC{len(self.posts) + 1:07d}", active="true")
                self.tables[table].append(rec)
                self.posts.append(rec)
                return httpx.Response(201, json={"result": rec})
            if req.method == "PATCH":
                self.patches.append(json.loads(req.content))
                return httpx.Response(200, json={"result": {"sys_id": req.url.path.rsplit("/", 1)[1], "number": "INC0000001"}})
            return httpx.Response(405)
        return httpx.MockTransport(handler)


ROSTER = """email,github_login,status,termination_date,department
alice@acme.com,alice,active,,Engineering
bob@acme.com,bob,active,,Engineering
mallory@acme.com,mallory,terminated,2026-07-15,Engineering
oldie@acme.com,,terminated,2026-06-30,IT
"""
