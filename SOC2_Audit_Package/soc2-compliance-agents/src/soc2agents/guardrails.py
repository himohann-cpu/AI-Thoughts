"""Hooks that enforce the tool allowlist and write a tamper-evident audit trail of every tool call."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from claude_agent_sdk import HookMatcher

from .tools import RunState


def _h(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def build_hooks(state: RunState, allowed: list[str]) -> dict:
    allowed_set = set(allowed)

    async def pre_tool(input_data, tool_use_id, context):
        name = input_data.get("tool_name", "")
        decision = "allow" if name in allowed_set else "deny"
        state.audit.append({"ts": datetime.now(timezone.utc).isoformat(), "event": "pre", "tool": name,
                            "decision": decision, "input_sha256": _h(input_data.get("tool_input")), "tool_use_id": tool_use_id})
        if decision == "deny":
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": f"{name} is not in this agent's allowlist"}}
        return {}

    async def post_tool(input_data, tool_use_id, context):
        state.audit.append({"ts": datetime.now(timezone.utc).isoformat(), "event": "post", "tool": input_data.get("tool_name"),
                            "output_sha256": _h(input_data.get("tool_response")), "tool_use_id": tool_use_id})
        return {}

    return {"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool])],
            "PostToolUse": [HookMatcher(matcher=None, hooks=[post_tool])]}
