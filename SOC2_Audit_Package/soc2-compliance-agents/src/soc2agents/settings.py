"""Runtime settings, read from environment variables (set by the GitHub Actions workflows)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"Missing required environment variable {name}")
    return val


@dataclass(frozen=True)
class Settings:
    github_org: str
    github_token: str | None
    github_api: str = "https://api.github.com"
    servicenow_instance: str | None = None      # e.g. https://acme.service-now.com
    servicenow_user: str | None = None
    servicenow_password: str | None = None      # or OAuth token via SN_TOKEN
    servicenow_token: str | None = None
    evidence_uri: str = "file://./evidence-out"  # file://path or s3://bucket/prefix
    dry_run: bool = True                         # default safe: never writes tickets unless explicitly disabled
    model: str | None = None                     # pin in CI (AGENT_MODEL) for change control
    max_budget_usd: float = 2.0
    config_dir: Path = field(default_factory=lambda: REPO_ROOT / "config")
    prompts_dir: Path = field(default_factory=lambda: REPO_ROOT / "prompts")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            github_org=_env("GH_ORG", required=True),
            github_token=_env("GH_TOKEN"),
            github_api=_env("GH_API", "https://api.github.com"),
            servicenow_instance=_env("SN_INSTANCE"),
            servicenow_user=_env("SN_USER"),
            servicenow_password=_env("SN_PASSWORD"),
            servicenow_token=_env("SN_TOKEN"),
            evidence_uri=_env("EVIDENCE_URI", "file://./evidence-out"),
            dry_run=_env("DRY_RUN", "true").lower() != "false",
            model=_env("AGENT_MODEL"),
            max_budget_usd=float(_env("AGENT_MAX_BUDGET_USD", "2.0")),
        )
