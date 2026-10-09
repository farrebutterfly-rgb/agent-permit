"""Claude Code PreToolUse hook.

Reads the hook event as JSON on stdin. If no rule matches, it exits 0 and Claude
Code's normal permission flow decides. If a rule matches, the call goes through only
when an approval exists for this exact tool and input; one use is consumed. Otherwise
a permit is requested (once per distinct action), the phone is notified, and the hook
exits 2 so Claude Code blocks the call and shows the reason to the agent.

While a freeze is active (``agent-permit freeze on``), every tool call is blocked,
whether or not a rule matches, until a human switches it off.
"""

from __future__ import annotations

import json
import sys
from typing import Callable, Optional, TextIO, Tuple

from .config import Config
from .store import Store, canonical

Notify = Callable[[object], None]


def evaluate(event: dict, cfg: Config, store: Store, notify: Optional[Notify] = None) -> Tuple[int, str]:
    tool = event.get("tool_name", "")
    tool_input = event.get("tool_input", {})
    fz = store.frozen()
    if fz:   # emergency stop: every tool call, rule or no rule, until a human unfreezes
        return 2, (
            f"Blocked by agent-permit: FROZEN by {fz['by']} ({fz['reason']}). Every tool call is blocked "
            "until a human runs `agent-permit freeze off`. Do nothing else and wait."
        )
    text = canonical(tool_input)
    rule = next((r for r in cfg.rules if r.matches(tool, text)), None)
    if rule is None:
        return 0, ""
    action = {"tool": tool, "input": tool_input}
    agent = f"claude-code:{event.get('session_id', '?')[:8]}"
    used = store.consume(action, agent=agent)
    if used:
        return 0, f"agent-permit: permit #{used.id} used for {rule.label}"
    summary = f"{rule.label}: {tool} {_preview(tool_input)}"
    p, created = store.request(action, summary, agent=agent, request_ttl=cfg.request_ttl)
    if created and notify:
        try:
            notify(p)
        except Exception as e:  # the block holds even if the phone cannot be reached
            store.audit.append("notify_error", id=p.id, error=str(e)[:200])
    state = "is waiting for approval" if p.status == "pending" else f"is {p.status}"
    return 2, (
        f"Blocked by agent-permit ({rule.label}). Permit #{p.id} {state}. "
        "It covers only this exact call. Ask the user to approve it, then retry the identical call. "
        "Do not change the command to get around the block."
    )


def _preview(tool_input: object, n: int = 300) -> str:
    if isinstance(tool_input, dict) and "command" in tool_input:
        s = str(tool_input["command"])
    else:
        s = canonical(tool_input)
    return s if len(s) <= n else s[: n - 3] + "..."


def main(cfg: Config, store: Store, notify: Optional[Notify] = None, stdin: TextIO = sys.stdin) -> int:
    try:
        event = json.load(stdin)
    except json.JSONDecodeError:
        print("agent-permit: hook input is not JSON, blocking to be safe", file=sys.stderr)
        return 2
    code, msg = evaluate(event, cfg, store, notify)
    if msg:
        print(msg, file=sys.stderr)
    return code
