"""Write-once evidence store.

Every artifact is written with a metadata sidecar (control, period, source, query, record
count, collector, UTC time) and its SHA-256 is appended to a per-run manifest. Existing
objects are never overwritten. Backends: local directory (tests, dry runs) or S3 with
Object Lock (production; the bucket's retention policy enforces immutability).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class AlreadyExists(RuntimeError):
    pass


@dataclass
class EvidenceMeta:
    control_id: str
    period: str
    source_system: str
    collector: str
    query: str
    record_count: int | None
    generated_by: str = "deterministic"   # or "agent" for LLM-authored summaries
    collected_at_utc: str = ""
    sha256: str = ""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EvidenceStore:
    def __init__(self, uri: str, run_id: str):
        self.uri = uri
        self.run_id = run_id
        p = urlparse(uri)
        self.scheme = p.scheme or "file"
        if self.scheme == "file":
            self.root = Path((p.netloc + p.path) or ".").resolve()
            self.root.mkdir(parents=True, exist_ok=True)
        elif self.scheme == "s3":
            import boto3  # optional dependency
            self.s3 = boto3.client("s3")
            self.bucket, self.prefix = p.netloc, p.path.lstrip("/")
        else:
            raise ValueError(f"Unsupported evidence URI {uri}")
        self.written: list[dict] = []

    # ---- backend primitives ---------------------------------------------
    def _exists(self, key: str) -> bool:
        if self.scheme == "file":
            return (self.root / key).exists()
        try:
            self.s3.head_object(Bucket=self.bucket, Key=f"{self.prefix}/{key}")
            return True
        except self.s3.exceptions.ClientError:
            return False

    def _put(self, key: str, data: bytes, content_type: str) -> None:
        if self._exists(key):
            raise AlreadyExists(key)
        if self.scheme == "file":
            path = self.root / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o444)
        else:
            self.s3.put_object(Bucket=self.bucket, Key=f"{self.prefix}/{key}", Body=data,
                               ContentType=content_type, ChecksumAlgorithm="SHA256")

    def read(self, key: str) -> bytes:
        if self.scheme == "file":
            return (self.root / key).read_bytes()
        return self.s3.get_object(Bucket=self.bucket, Key=f"{self.prefix}/{key}")["Body"].read()

    def list(self, prefix: str = "") -> list[str]:
        if self.scheme == "file":
            base = self.root / prefix
            if not base.exists():
                return []
            return sorted(str(p.relative_to(self.root)) for p in base.rglob("*") if p.is_file())
        keys, token = [], None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": f"{self.prefix}/{prefix}"}
            if token:
                kw["ContinuationToken"] = token
            resp = self.s3.list_objects_v2(**kw)
            keys += [o["Key"][len(self.prefix) + 1:] for o in resp.get("Contents", [])]
            if not resp.get("IsTruncated"):
                return sorted(keys)
            token = resp["NextContinuationToken"]

    # ---- public API ------------------------------------------------------
    def put_artifact(self, name: str, payload: Any, meta: EvidenceMeta, ext: str = "json") -> str:
        if ext == "json":
            data = json.dumps(payload, indent=2, sort_keys=True, default=str).encode()
            ctype = "application/json"
        else:
            data = payload.encode() if isinstance(payload, str) else payload
            ctype = "text/markdown" if ext == "md" else "application/octet-stream"
        meta.collected_at_utc = meta.collected_at_utc or utcnow()
        meta.sha256 = hashlib.sha256(data).hexdigest()
        stamp = meta.collected_at_utc.replace(":", "").replace("-", "")
        run = hashlib.sha256(self.run_id.encode()).hexdigest()[:6]
        key = f"evidence/{meta.control_id}/{meta.period}/{meta.source_system}/{name}_{stamp}_{run}.{ext}"
        self._put(key, data, ctype)
        self._put(key + ".meta.json", json.dumps(asdict(meta), indent=2).encode(), "application/json")
        self.written.append({"key": key, **asdict(meta)})
        return key

    def write_manifest(self) -> str | None:
        if not self.written:
            return None
        key = f"manifests/{self.run_id}.json"
        body = {"run_id": self.run_id, "created_at_utc": utcnow(), "artifacts": self.written}
        self._put(key, json.dumps(body, indent=2).encode(), "application/json")
        return key
