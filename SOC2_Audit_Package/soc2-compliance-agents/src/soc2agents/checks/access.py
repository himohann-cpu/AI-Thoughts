"""Access-review preparation (IAM-03, IAM-04, IAM-05, IAM-07).

Builds the quarterly review packet for GitHub and ServiceNow and flags anomalies.
The packet's `suggested_action` is advisory; system owners make and sign the decision (PRC-02).

Identity roster: until the IdP/HRIS adapter is chosen (REQ-001), the roster is a CSV export
with columns: email, github_login, status (active|terminated|leave), termination_date, department.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

from .base import Finding


def load_roster(csv_text: str) -> dict[str, dict]:
    """Index roster rows by lower-cased github_login and email."""
    idx: dict[str, dict] = {}
    for row in csv.DictReader(io.StringIO(csv_text)):
        row = {k.strip(): (v or "").strip() for k, v in row.items()}
        for key in (row.get("github_login", ""), row.get("email", "")):
            if key:
                idx[key.lower()] = row
    return idx


def github_packet(members: list[dict], admins: list[dict], outside: list[dict], roster: dict[str, dict],
                  max_owners: int = 3) -> tuple[list[dict], list[Finding]]:
    admin_logins = {a["login"] for a in admins}
    rows, findings = [], []
    for m in members:
        login = m["login"]
        person = roster.get(login.lower())
        role = "owner" if login in admin_logins else "member"
        flags, action = [], "Keep"
        if person is None:
            flags.append("not in identity roster")
            action = "Review"
        elif person.get("status") == "terminated":
            flags.append(f"terminated {person.get('termination_date', '')}".strip())
            action = "Remove"
        if login.endswith("[bot]") or m.get("type") == "Bot":
            flags.append("machine account – confirm owner")
            action = "Review"
        rows.append({"system": "GitHub", "account": login, "role": role, "email": (person or {}).get("email", ""),
                     "roster_status": (person or {}).get("status", "unknown"), "flags": "; ".join(flags),
                     "suggested_action": action})
        if "terminated" in " ".join(flags):
            findings.append(Finding("access.leaver_active", ["IAM-03"], "FAIL", f"GitHub:{login}",
                                    f"Terminated user still a GitHub org {role}", "critical", {"termination_date": person.get("termination_date")}))
        elif "not in identity roster" in flags:
            findings.append(Finding("access.unknown_identity", ["IAM-04", "IAM-01"], "FAIL", f"GitHub:{login}",
                                    "GitHub account not matched to a person in the identity roster", "medium"))
    for o in outside:
        rows.append({"system": "GitHub", "account": o["login"], "role": "outside collaborator", "email": "",
                     "roster_status": "external", "flags": "outside collaborator", "suggested_action": "Review"})
        findings.append(Finding("access.outside_collaborator", ["IAM-04"], "INFO", f"GitHub:{o['login']}",
                                "Outside collaborator – confirm business need and repos", "low"))
    if len(admin_logins) > max_owners:
        findings.append(Finding("access.too_many_owners", ["IAM-05"], "FAIL", "GitHub:org-owners",
                                f"{len(admin_logins)} organization owners (max {max_owners})", "medium",
                                {"owners": sorted(admin_logins)}))
    return rows, findings


def servicenow_packet(role_rows: list[dict], users: dict[str, dict], roster: dict[str, dict],
                      inactive_days: int = 90, now: datetime | None = None) -> tuple[list[dict], list[Finding]]:
    """role_rows: sys_user_has_role rows (user, role name); users: sys_id -> sys_user row."""
    now = now or datetime.now(timezone.utc)
    rows, findings = [], []
    for rr in role_rows:
        u = users.get(rr.get("user", ""), {})
        email = (u.get("email") or "").lower()
        person = roster.get(email)
        flags, action = [], "Keep"
        last = u.get("last_login_time")
        if last:
            try:
                lt = datetime.fromisoformat(last.replace(" ", "T")).replace(tzinfo=timezone.utc)
                if now - lt > timedelta(days=inactive_days):
                    flags.append(f"no login for > {inactive_days} days")
                    action = "Review"
            except ValueError:
                pass
        if person and person.get("status") == "terminated":
            flags.append("terminated")
            action = "Remove"
            if str(u.get("active")).lower() == "true":
                findings.append(Finding("access.leaver_active", ["IAM-03"], "FAIL", f"ServiceNow:{u.get('user_name')}",
                                        f"Terminated user active with role {rr.get('role')}", "critical"))
        elif person is None and str(u.get("web_service_access_only")).lower() != "true":
            flags.append("not in identity roster")
            action = "Review"
        rows.append({"system": "ServiceNow", "account": u.get("user_name", rr.get("user")), "role": rr.get("role"),
                     "email": email, "roster_status": (person or {}).get("status", "unknown"),
                     "flags": "; ".join(flags), "suggested_action": action})
    return rows, findings
