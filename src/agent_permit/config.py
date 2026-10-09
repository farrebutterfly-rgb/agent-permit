"""Configuration from environment variables and an optional rules file."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Pattern

DEFAULT_RULES = [
    {"tool": r"^Bash$", "input": r"\bgit\s+push\b", "label": "git push"},
    {"tool": r"^Bash$", "input": r"\bgh\s+(pr\s+(create|merge)|release\s+create|repo\s+(create|delete))\b", "label": "GitHub write"},
    {"tool": r"^Bash$", "input": r"\b(npm|pnpm|yarn)\s+publish\b|\btwine\s+upload\b|\buv\s+publish\b", "label": "package publish"},
    {"tool": r"^Bash$", "input": r"\b(curl|wget|http)\b.*(-X\s*(POST|PUT|PATCH|DELETE)|--data|\s-d\s|--form|\s-F\s)", "label": "outbound write over HTTP"},
    {"tool": r"^Bash$", "input": r"\bterraform\s+(apply|destroy)\b|\bkubectl\s+(apply|delete|replace|patch)\b", "label": "infrastructure change"},
    {"tool": r"^Bash$", "input": r"\bvercel\b.*--prod\b|\bfly\s+deploy\b|\bgcloud\s+run\s+deploy\b", "label": "production deploy"},
    {"tool": r"^mcp__.*__(send|post|publish|create|delete|merge|deploy|transfer|pay)[a-z_]*$", "input": r"", "label": "MCP write"},
]


@dataclass
class Rule:
    tool: Pattern[str]
    input: Pattern[str]
    label: str

    def matches(self, tool_name: str, tool_input_text: str) -> bool:
        return bool(self.tool.search(tool_name)) and bool(self.input.search(tool_input_text))


@dataclass
class Config:
    home: str
    db_path: str
    audit_path: str
    grant_ttl: float = 30 * 60
    request_ttl: float = 6 * 3600
    telegram_token: Optional[str] = None
    telegram_chat: Optional[int] = None
    telegram_users: Optional[List[int]] = None
    rules: List[Rule] = field(default_factory=list)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat)


def compile_rules(raw: list) -> List[Rule]:
    return [Rule(re.compile(r["tool"]), re.compile(r.get("input", ""), re.IGNORECASE | re.DOTALL), r.get("label", r["tool"])) for r in raw]


def load(env: Optional[dict] = None) -> Config:
    env = os.environ if env is None else env
    home = os.path.expanduser(env.get("AGENT_PERMIT_HOME", "~/.agent-permit"))
    rules_path = env.get("AGENT_PERMIT_RULES", os.path.join(home, "rules.json"))
    raw = DEFAULT_RULES
    if os.path.exists(rules_path):
        with open(rules_path, encoding="utf-8") as f:
            raw = json.load(f)
    users = env.get("AGENT_PERMIT_TELEGRAM_USERS")
    chat = env.get("AGENT_PERMIT_TELEGRAM_CHAT")
    return Config(
        home=home,
        db_path=os.path.join(home, "permits.db"),
        audit_path=os.path.join(home, "audit.jsonl"),
        grant_ttl=float(env.get("AGENT_PERMIT_GRANT_TTL", 30 * 60)),
        request_ttl=float(env.get("AGENT_PERMIT_REQUEST_TTL", 6 * 3600)),
        telegram_token=env.get("AGENT_PERMIT_TELEGRAM_TOKEN") or None,
        telegram_chat=int(chat) if chat else None,
        telegram_users=[int(u) for u in users.split(",") if u.strip()] if users else None,
        rules=compile_rules(raw),
    )
