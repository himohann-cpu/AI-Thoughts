"""ServiceNow Table API client with a narrow write surface.

Reads: only tables listed in `read_tables`.
Writes: only create/update on ONE configured ticket table, only allowlisted fields,
only records the agent itself created (correlation_id prefix), and never approvals,
changes, roles or users. This is what keeps the agents "read-only + recommend".
"""
from __future__ import annotations

from typing import Any

import httpx

CORRELATION_PREFIX = "soc2agent:"

DEFAULT_READ_TABLES = frozenset({
    "change_request", "sysapproval_approver", "sc_req_item", "sc_task", "incident",
    "sys_user_has_role", "sys_user", "sys_user_grmember", "task_sla",
})
DEFAULT_WRITE_FIELDS = frozenset({
    "short_description", "description", "work_notes", "assignment_group", "category",
    "subcategory", "urgency", "impact", "correlation_id", "correlation_display",
})
# Tables an agent may never write to, whatever the configuration says.
FORBIDDEN_WRITE_TABLES = frozenset({
    "change_request", "sysapproval_approver", "sys_user", "sys_user_has_role",
    "sys_user_grmember", "sys_properties", "sys_audit",
})


class WriteNotAllowed(RuntimeError):
    pass


class ServiceNowClient:
    def __init__(self, instance: str, user: str | None = None, password: str | None = None,
                 token: str | None = None, ticket_table: str = "incident",
                 read_tables: frozenset[str] = DEFAULT_READ_TABLES,
                 write_fields: frozenset[str] = DEFAULT_WRITE_FIELDS,
                 transport: httpx.BaseTransport | None = None):
        if ticket_table in FORBIDDEN_WRITE_TABLES:
            raise WriteNotAllowed(f"{ticket_table} can never be an agent ticket table")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        auth = None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif user:
            auth = (user, password or "")
        self._http = httpx.Client(base_url=instance.rstrip("/"), headers=headers, auth=auth,
                                  timeout=30, transport=transport)
        self.ticket_table = ticket_table
        self.read_tables = read_tables
        self.write_fields = write_fields

    # ---- reads -----------------------------------------------------------
    def query(self, table: str, query: str, fields: list[str] | None = None, limit: int = 1000) -> list[dict]:
        if table not in self.read_tables:
            raise PermissionError(f"Table {table} is not in the read allowlist")
        out, offset = [], 0
        while True:
            params: dict[str, Any] = {"sysparm_query": query, "sysparm_limit": limit, "sysparm_offset": offset,
                                      "sysparm_display_value": "false", "sysparm_exclude_reference_link": "true"}
            if fields:
                params["sysparm_fields"] = ",".join(fields)
            r = self._http.get(f"/api/now/table/{table}", params=params)
            r.raise_for_status()
            rows = r.json().get("result", [])
            out.extend(rows)
            if len(rows) < limit:
                return out
            offset += limit

    # ---- narrow writes ---------------------------------------------------
    def _clean(self, fields: dict) -> dict:
        bad = set(fields) - self.write_fields
        if bad:
            raise WriteNotAllowed(f"Fields not allowed: {sorted(bad)}")
        return fields

    def find_open_ticket(self, correlation_id: str) -> dict | None:
        rows = self._http.get(f"/api/now/table/{self.ticket_table}", params={
            "sysparm_query": f"correlation_id={correlation_id}^active=true", "sysparm_limit": 1}).json().get("result", [])
        return rows[0] if rows else None

    def upsert_ticket(self, correlation_id: str, fields: dict) -> dict:
        """Create a ticket, or add a work note to the open ticket with the same correlation id (idempotent)."""
        if not correlation_id.startswith(CORRELATION_PREFIX):
            raise WriteNotAllowed("correlation_id must start with the agent prefix")
        fields = self._clean(dict(fields, correlation_id=correlation_id))
        existing = self.find_open_ticket(correlation_id)
        if existing:
            note = fields.get("work_notes") or fields.get("description", "")
            r = self._http.patch(f"/api/now/table/{self.ticket_table}/{existing['sys_id']}",
                                 json={"work_notes": f"[soc2agent] still failing: {note}"[:4000]})
            r.raise_for_status()
            return {"action": "updated", **r.json()["result"]}
        r = self._http.post(f"/api/now/table/{self.ticket_table}", json=fields)
        r.raise_for_status()
        return {"action": "created", **r.json()["result"]}
