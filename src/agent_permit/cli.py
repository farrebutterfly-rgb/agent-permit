"""Command line: agent-permit hook | mcp | telegram | list | approve | deny | verify."""

from __future__ import annotations

import argparse
import sys
import time
from typing import List, Optional

from . import __version__, config
from .store import Store


def _notifier(cfg: config.Config, store: Store):
    if not cfg.telegram_enabled:
        return None
    from .telegram import TelegramChannel, http_api

    ch = TelegramChannel(store, http_api(cfg.telegram_token, timeout=15), cfg.telegram_chat, cfg.telegram_users, cfg.grant_ttl)
    return ch.notify


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="agent-permit", description="Human approval for exact agent actions.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hook", help="Claude Code PreToolUse hook (reads the event on stdin)")
    sub.add_parser("mcp", help="run the MCP server on stdio")
    sub.add_parser("telegram", help="poll Telegram for button presses")
    ls = sub.add_parser("list", help="list recent permits")
    ls.add_argument("--status")
    for name in ("approve", "deny"):
        p = sub.add_parser(name, help=f"{name} a pending permit from this machine")
        p.add_argument("id", type=int)
        if name == "approve":
            p.add_argument("--ttl", type=float, help="seconds the approval is valid")
            p.add_argument("--uses", type=int, default=1)
    sub.add_parser("verify", help="check that the audit log chain is intact")
    a = ap.parse_args(argv)

    cfg = config.load()
    store = Store(cfg.db_path, cfg.audit_path)

    if a.cmd == "hook":
        from .hook import main as hook_main

        return hook_main(cfg, store, _notifier(cfg, store))
    if a.cmd == "mcp":
        from .server import build

        build(cfg, store, _notifier(cfg, store)).run()
        return 0
    if a.cmd == "telegram":
        if not cfg.telegram_enabled:
            print("set AGENT_PERMIT_TELEGRAM_TOKEN and AGENT_PERMIT_TELEGRAM_CHAT", file=sys.stderr)
            return 2
        from .telegram import TelegramChannel, http_api

        TelegramChannel(store, http_api(cfg.telegram_token), cfg.telegram_chat, cfg.telegram_users, cfg.grant_ttl).run()
        return 0
    if a.cmd == "list":
        for p in store.list(a.status):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(p.created_at))
            print(f"#{p.id:<5} {p.status:<9} {when}  {p.agent:<22} {p.summary[:80]}")
        return 0
    if a.cmd in ("approve", "deny"):
        try:
            p = store.decide(a.id, a.cmd == "approve", by="cli", grant_ttl=getattr(a, "ttl", None) or cfg.grant_ttl,
                             uses=getattr(a, "uses", 1))
        except (KeyError, ValueError) as e:
            print(e, file=sys.stderr)
            return 1
        print(f"#{p.id} {p.status}")
        return 0
    if a.cmd == "verify":
        ok, n, msg = store.audit.verify()
        print(msg)
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
