"""MCP server: lets any MCP-capable agent ask for a permit and wait for the answer.

The tools are cooperative: an agent that calls use_permit before acting gets a
reliable answer, but nothing stops an agent that never asks. For enforcement in
Claude Code, use the PreToolUse hook as well (see README).
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Optional

from .config import Config
from .store import Store


def _parse(action: str) -> Any:
    try:
        return json.loads(action)
    except json.JSONDecodeError:
        return action


def tools(cfg: Config, store: Store, notify: Optional[Callable] = None, sleep: Callable[[float], None] = time.sleep):
    """The tool functions, separate from the MCP wiring so they can be tested directly."""

    def request_permit(action: str, summary: str, agent: str = "mcp-agent", wait_seconds: int = 0) -> dict:
        """Ask a human to approve one exact action before you perform it.

        action: the exact action, ideally as JSON (for example the tool name and arguments
                you will call). The approval only covers this exact value.
        summary: one or two plain sentences for the human: what, to whom, why.
        wait_seconds: how long to wait for the decision (0 returns at once, max 3600).
        """
        p, created = store.request(_parse(action), summary, agent=agent, request_ttl=cfg.request_ttl)
        if created and notify:
            try:
                notify(p)
            except Exception as e:
                store.audit.append("notify_error", id=p.id, error=str(e)[:200])
        deadline = time.time() + max(0, min(int(wait_seconds), 3600))
        while p.status == "pending" and time.time() < deadline:
            sleep(2)
            p = store.get(p.id)
        return {"id": p.id, "status": p.status, "fingerprint": p.short, "created": created}

    def check_permit(permit_id: int) -> dict:
        """Status of a permit: pending, approved, denied, expired or used."""
        p = store.get(int(permit_id))
        if p is None:
            return {"id": permit_id, "status": "unknown"}
        return {"id": p.id, "status": p.status, "uses_left": p.uses_left, "fingerprint": p.short}

    def use_permit(action: str, agent: str = "mcp-agent") -> dict:
        """Consume one approval for this exact action right before performing it.

        Returns allowed=true only if an unexpired approval exists for the identical action.
        If allowed is false, do not perform the action.
        """
        p = store.consume(_parse(action), agent=agent)
        return {"allowed": p is not None, "id": p.id if p else None}

    return request_permit, check_permit, use_permit


def build(cfg: Config, store: Store, notify: Optional[Callable] = None):
    try:  # mcp >= 2
        from mcp.server.mcpserver import MCPServer as Server
    except ImportError:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP as Server

    server = Server("agent-permit")
    for fn in tools(cfg, store, notify):
        server.tool()(fn)
    return server
