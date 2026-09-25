"""End-to-end runs against the fake APIs, with and without the (mocked) model."""
import json

import pytest
from conftest import ROSTER, FakeServiceNow, github_transport

import soc2agents.runner as runner
from soc2agents.clients import github as ghmod
from soc2agents.clients import servicenow as snmod


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake = FakeServiceNow()
    real_gh, real_sn = ghmod.GitHubClient.__init__, snmod.ServiceNowClient.__init__

    def gh_init(self, org, token, api="https://api.github.com", transport=None):
        real_gh(self, org, token, api, transport=github_transport())

    def sn_init(self, instance, *a, transport=None, **kw):
        real_sn(self, instance, *a, transport=fake.transport(), **kw)

    monkeypatch.setattr(ghmod.GitHubClient, "__init__", gh_init)
    monkeypatch.setattr(snmod.ServiceNowClient, "__init__", sn_init)
    monkeypatch.setenv("GH_ORG", "acme")
    monkeypatch.setenv("SN_INSTANCE", "https://acme.service-now.com")
    monkeypatch.setenv("EVIDENCE_URI", f"file://{tmp_path}/ev")
    monkeypatch.setenv("DRY_RUN", "true")
    return tmp_path, fake


def _manifest(tmp_path):
    [m] = list((tmp_path / "ev" / "manifests").glob("*.json"))
    return json.loads(m.read_text())


def test_drift_no_llm_end_to_end(env, capsys):
    tmp_path, fake = env
    rc = runner.main(["run", "drift", "--no-llm", "--period", "2026-Q3"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1                                   # critical FAIL (legacy-billing has no ServiceNow gate)
    assert out["findings"]["FAIL"] == 2 and out["mode"] == "no-llm"
    controls = {a["control_id"] for a in _manifest(tmp_path)["artifacts"]}
    assert {"CHG-01", "CHG-11", "CHG-05", "GOV-08", "MON-06", "AUT-03"} <= controls
    assert fake.posts == []                          # dry run: nothing sent


def test_change_live_no_llm_opens_tickets(env, capsys, monkeypatch):
    tmp_path, fake = env
    rc = runner.main(["run", "change", "--no-llm", "--live", "--period", "2026-Q3", "--fail-on", "none"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    subjects = " ".join(p["short_description"] for p in fake.posts)
    assert "change.deploy_without_change" in subjects and "change.self_approved" in subjects
    assert "pr.no_independent_approval" in subjects and "pr.bot_only_approval" in subjects
    assert out["tickets"] == len(fake.posts)


def test_access_packet(env, capsys):
    tmp_path, _ = env
    roster = tmp_path / "roster.csv"
    roster.write_text(ROSTER)
    runner.main(["run", "access", "--no-llm", "--period", "2026-Q3", "--roster", str(roster), "--fail-on", "none"])
    [csv_key] = [a["key"] for a in _manifest(tmp_path)["artifacts"] if a["key"].endswith(".csv")]
    text = (tmp_path / "ev" / csv_key).read_text()
    assert "mallory" in text and "Remove" in text and "reviewer_decision" in text


def test_agent_phase_options_and_fallback(env, capsys, monkeypatch):
    """The model call is mocked: check the agent is locked down, then check fallback when it errors."""
    import claude_agent_sdk
    from claude_agent_sdk import ResultMessage
    seen = {}

    async def fake_query(prompt, options):
        seen["options"] = options
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2,
                            session_id="s", total_cost_usd=0.01,
                            structured_output={"overall_status": "red", "summary": "s", "priorities": []})

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    runner.main(["run", "drift", "--period", "2026-Q3", "--fail-on", "none"])
    o = seen["options"]
    assert o.tools == [] and o.setting_sources == []
    assert all(t.startswith("mcp__soc2__") for t in o.allowed_tools)
    assert "Bash" in o.disallowed_tools and o.output_format["type"] == "json_schema"
    assert json.loads(capsys.readouterr().out)["mode"] == "agent"

    async def broken_query(prompt, options):
        raise RuntimeError("model unavailable")
        yield  # pragma: no cover

    monkeypatch.setattr(claude_agent_sdk, "query", broken_query)
    runner.main(["run", "drift", "--period", "2026-Q3", "--fail-on", "none"])
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "fallback" and out["tickets"] == 2 and out["report"].endswith(".md")
