"""Read-only GitHub client (REST + GraphQL).

The client refuses any HTTP method other than GET (and POST to /graphql for queries only),
so an agent cannot change GitHub even if a tool is misused. Use a GitHub App installation
token with read-only permissions (administration:read, members:read, metadata, actions:read,
deployments:read, security_events:read, organization custom properties:read).
"""
from __future__ import annotations

import re
from typing import Any, Iterator

import httpx

_MUTATION = re.compile(r"^\s*mutation\b", re.IGNORECASE)


class ReadOnlyViolation(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, org: str, token: str | None, api: str = "https://api.github.com",
                 transport: httpx.BaseTransport | None = None):
        self.org = org
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(base_url=api, headers=headers, timeout=30, transport=transport)

    # ---- low level -------------------------------------------------------
    def get(self, path: str, params: dict | None = None) -> Any:
        r = self._http.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def paginate(self, path: str, params: dict | None = None) -> Iterator[Any]:
        params = dict(params or {}, per_page=100)
        url: str | None = path
        while url:
            r = self._http.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            items = data if isinstance(data, list) else next((v for v in data.values() if isinstance(v, list)), [])
            yield from items
            url = r.links.get("next", {}).get("url")
            params = None  # next link already carries the query string

    def graphql(self, query: str, variables: dict | None = None) -> Any:
        if _MUTATION.match(query):
            raise ReadOnlyViolation("GraphQL mutations are not permitted")
        r = self._http.post("/graphql", json={"query": query, "variables": variables or {}})
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            raise RuntimeError(f"GraphQL error: {body['errors']}")
        return body["data"]

    # ---- domain helpers --------------------------------------------------
    def scoped_repos(self, prop: str = "soc2_scope") -> list[str]:
        """Repositories whose custom property soc2_scope == true (the in-scope population)."""
        out = []
        for item in self.paginate(f"/orgs/{self.org}/properties/values"):
            props = {p["property_name"]: p["value"] for p in item.get("properties", [])}
            if str(props.get(prop, "")).lower() == "true":
                out.append(item["repository_name"])
        return sorted(out)

    def org_rulesets(self) -> list[dict]:
        summaries = self.get(f"/orgs/{self.org}/rulesets")
        return [self.get(f"/orgs/{self.org}/rulesets/{s['id']}") for s in summaries]

    def branch_rules(self, repo: str, branch: str = "main") -> list[dict]:
        return self.get(f"/repos/{self.org}/{repo}/rules/branches/{branch}")

    def actions_permissions(self) -> dict:
        return {
            "permissions": self.get(f"/orgs/{self.org}/actions/permissions"),
            "workflow": self.get(f"/orgs/{self.org}/actions/permissions/workflow"),
        }

    def environment(self, repo: str, env: str = "production") -> dict | None:
        try:
            e = self.get(f"/repos/{self.org}/{repo}/environments/{env}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        e["custom_rules"] = self.get(f"/repos/{self.org}/{repo}/environments/{env}/deployment_protection_rules")
        return e

    def deployments(self, repo: str, env: str = "production", since: str | None = None) -> list[dict]:
        out = []
        for d in self.paginate(f"/repos/{self.org}/{repo}/deployments", {"environment": env}):
            if since and d["created_at"] < since:
                break  # API returns newest first
            d["statuses"] = self.get(f"/repos/{self.org}/{repo}/deployments/{d['id']}/statuses")
            out.append(d)
        return out

    MERGED_PRS = """
    query($q: String!, $cursor: String) {
      search(query: $q, type: ISSUE, first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { ... on PullRequest {
          number url title mergedAt baseRefName
          author { login } mergedBy { login }
          mergeCommit { oid }
          commits(last: 1) { nodes { commit { oid author { user { login } } committer { user { login } } } } }
          reviews(states: APPROVED, first: 20) { nodes { author { login } submittedAt } }
        } }
      }
    }"""

    def merged_prs(self, repo: str, start: str, end: str, base: str = "main") -> list[dict]:
        q = f"repo:{self.org}/{repo} is:pr is:merged base:{base} merged:{start}..{end}"
        out, cursor = [], None
        while True:
            data = self.graphql(self.MERGED_PRS, {"q": q, "cursor": cursor})["search"]
            out.extend(n for n in data["nodes"] if n)
            if not data["pageInfo"]["hasNextPage"]:
                return out
            cursor = data["pageInfo"]["endCursor"]

    def members(self, role: str = "all") -> list[dict]:
        return list(self.paginate(f"/orgs/{self.org}/members", {"role": role}))

    def outside_collaborators(self) -> list[dict]:
        return list(self.paginate(f"/orgs/{self.org}/outside_collaborators"))
