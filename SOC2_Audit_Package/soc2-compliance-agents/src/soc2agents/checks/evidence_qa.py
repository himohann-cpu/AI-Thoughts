"""Evidence QA (GOV-05, PRC-03 §5): completeness, integrity and dating of stored evidence."""
from __future__ import annotations

import hashlib
import json

from ..evidence.store import EvidenceStore
from .base import Finding


def qa_period(store: EvidenceStore, period: str, expectations: dict[str, int]) -> list[Finding]:
    out: list[Finding] = []
    for control, minimum in expectations.items():
        keys = [k for k in store.list(f"evidence/{control}/{period}/") if not k.endswith(".meta.json")]
        status = "PASS" if len(keys) >= minimum else "FAIL"
        out.append(Finding("evidence.coverage", [control, "GOV-05"], status, f"{control}/{period}",
                           f"{len(keys)} artifact(s) stored; expected at least {minimum}",
                           "info" if status == "PASS" else "medium", {"count": len(keys), "expected": minimum}))
        for k in keys:
            try:
                meta = json.loads(store.read(k + ".meta.json"))
            except FileNotFoundError:
                out.append(Finding("evidence.metadata_missing", [control], "FAIL", k, "Artifact has no metadata sidecar", "medium"))
                continue
            digest = hashlib.sha256(store.read(k)).hexdigest()
            if digest != meta.get("sha256"):
                out.append(Finding("evidence.hash_mismatch", [control], "FAIL", k,
                                   "SHA-256 does not match metadata – possible tampering", "critical"))
            if not meta.get("collected_at_utc") or not meta.get("source_system") or not meta.get("query"):
                out.append(Finding("evidence.incomplete_metadata", [control], "FAIL", k,
                                   "Metadata missing timestamp, source or query (IPE)", "medium"))
    return out


def map_pbc(store: EvidenceStore, pbc_rows: list[dict], period: str) -> list[dict]:
    """Map PBC requests (from the workbook's PBC Evidence List) to stored evidence keys."""
    mapped = []
    for row in pbc_rows:
        control = row.get("Control ID", "").strip()
        keys = [k for k in store.list(f"evidence/{control}/{period}/") if not k.endswith(".meta.json")]
        mapped.append({"request_id": row.get("Request ID"), "control_id": control,
                       "evidence_requested": row.get("Evidence Requested"), "candidate_keys": keys[:25],
                       "candidate_count": len(keys), "status": "Candidates found" if keys else "No evidence stored"})
    return mapped
