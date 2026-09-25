from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Status = Literal["PASS", "FAIL", "ERROR", "INFO"]


@dataclass
class Finding:
    """One deterministic check outcome. The LLM may explain or prioritise it, never flip it."""
    check: str
    control_ids: list[str]
    status: Status
    subject: str                 # e.g. repo name, change number, user login
    detail: str
    severity: Literal["critical", "high", "medium", "low", "info"] = "medium"
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def finding_id(self) -> str:
        """Stable id so the same failure maps to the same ServiceNow ticket across runs."""
        raw = f"{self.check}|{self.subject}|{','.join(sorted(self.control_ids))}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["finding_id"] = self.finding_id
        return d


def summarize(findings: list[Finding]) -> dict[str, int]:
    out: dict[str, int] = {}
    for f in findings:
        out[f.status] = out.get(f.status, 0) + 1
    return out
