# agent-permit

A human approves one exact agent action, from the phone, before it happens.

AI coding agents are good at doing things. Some things should not happen without a person saying yes: pushing to a shared branch, publishing a package, posting to an API, deleting infrastructure, sending a message. agent-permit puts a gate in front of those actions:

* **Claude Code hook.** A `PreToolUse` hook blocks matching tool calls until an approval exists for that exact call. The agent sees why it was blocked and that it should ask and retry.
* **MCP server.** Any MCP-capable agent can request a permit, wait for the decision and consume it right before acting.
* **Phone approval over Telegram.** Each request arrives as a message with Approve and Deny buttons. Long polling, so no public web server is needed.
* **Tamper-evident audit log.** Every request, decision and use is written to a hash-chained JSON Lines file. `agent-permit verify` reports any edited or deleted line.

## How a permit works

1. The agent tries an action that matches a rule, for example `git push origin main`.
2. The hook computes a fingerprint: the SHA-256 of the tool name and its exact input as canonical JSON.
3. If an unexpired approval exists for that fingerprint, one use is consumed and the call goes through.
4. If not, a permit is requested (once per distinct action), you get a message on the phone, and the call is blocked with exit code 2.
5. You press Approve. The approval is valid for 30 minutes and one use by default.
6. The agent retries the identical call and it goes through. `git push --force origin main` has a different fingerprint and is blocked again.

```
$ echo '{"tool_name":"Bash","tool_input":{"command":"git push origin main"}}' | agent-permit hook
Blocked by agent-permit (git push). Permit #1 is waiting for approval. It covers only this exact call.
Ask the user to approve it, then retry the identical call. Do not change the command to get around the block.
$ echo $?
2
```

## Install

```bash
pip install "agent-permit[mcp] @ git+https://github.com/farrebutterfly-rgb/agent-permit"
```

Python 3.10 or later. The core has no dependencies; the `mcp` extra adds the MCP server (works with `mcp` 1.x and 2.x).

## Claude Code

Add the hook to `.claude/settings.json` in a project, or to `~/.claude/settings.json` for all projects:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|mcp__.*",
        "hooks": [{ "type": "command", "command": "agent-permit hook" }]
      }
    ]
  }
}
```

Calls that match no rule pass through untouched, so Claude Code's own permission settings still apply to everything else. If the hook gets input it cannot parse, it blocks.

## Phone approval with Telegram

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Send the bot a message, then read your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`.
3. Set the environment, for the hook and the poller alike:

```bash
export AGENT_PERMIT_TELEGRAM_TOKEN=123456:ABC...
export AGENT_PERMIT_TELEGRAM_CHAT=111111111
export AGENT_PERMIT_TELEGRAM_USERS=111111111   # optional: only these user ids may press the buttons
```

4. Run the poller that receives the button presses, for example as a launchd or systemd service:

```bash
agent-permit telegram
```

Button presses from any other chat or user are rejected and logged. Without Telegram you can decide from the terminal with `agent-permit list`, `agent-permit approve 12` and `agent-permit deny 12`.

## MCP server

```json
{
  "mcpServers": {
    "agent-permit": { "command": "agent-permit", "args": ["mcp"] }
  }
}
```

Tools:

| Tool | What it does |
|---|---|
| `request_permit(action, summary, wait_seconds=0)` | Requests approval for the exact `action` (JSON or text) with a plain summary for the human. Can wait up to an hour for the decision. |
| `check_permit(permit_id)` | Returns pending, approved, denied, expired or used. |
| `use_permit(action)` | Consumes one approval for the identical action. Returns `allowed: false` if there is none. |

The MCP tools are cooperative: they give a reliable answer to an agent that asks, but they cannot stop an agent that never asks. Use the hook for enforcement.

## Rules

Without a rules file the hook gates `git push`, GitHub writes through `gh`, package publishing, HTTP calls with a body or a write method, `terraform apply` and `kubectl` changes, production deploys, and MCP tools whose names start with send, post, publish, create, delete, merge, deploy, transfer or pay.

To use your own rules, write `~/.agent-permit/rules.json` (or point `AGENT_PERMIT_RULES` at a file). Each rule has a regex for the tool name and one for the tool input as canonical JSON:

```json
[
  { "tool": "^Bash$", "input": "\\bgit\\s+push\\b", "label": "git push" },
  { "tool": "^(Write|Edit)$", "input": "\\.env", "label": "secrets file" },
  { "tool": "^mcp__gmail__send", "input": "", "label": "e-mail" }
]
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_PERMIT_HOME` | `~/.agent-permit` | Where `permits.db`, `audit.jsonl` and `rules.json` live |
| `AGENT_PERMIT_GRANT_TTL` | `1800` | Seconds an approval is valid |
| `AGENT_PERMIT_REQUEST_TTL` | `21600` | Seconds a request waits before it expires |
| `AGENT_PERMIT_TELEGRAM_TOKEN` | | Bot token |
| `AGENT_PERMIT_TELEGRAM_CHAT` | | Chat id that receives requests |
| `AGENT_PERMIT_TELEGRAM_USERS` | | Comma separated user ids allowed to decide |

## Security model and limits

* **It is a gate, not a sandbox.** The hook sees tool calls, not what a program does once it runs. An agent that writes a script which makes an HTTP request, then runs the script, is not caught by the HTTP rule. Combine it with network egress rules and least privilege credentials.
* **Rules are patterns.** Review them for your own tools. A command that is obfuscated enough will not match a regex.
* **An approval is used when the call is allowed, not when it succeeds.** If the action fails, ask again.
* **Telegram is trusted for the decision.** Use `AGENT_PERMIT_TELEGRAM_USERS` and a private chat. Never put secrets or client content in the summary, describe the action instead.
* **The audit log is tamper-evident, not tamper-proof.** Anyone with write access can rewrite the whole chain. Ship it to a separate system if you need that.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[mcp,dev]"
.venv/bin/python -m pytest
```

The tests cover fingerprint binding, single and multiple use, expiry, deduplication, the audit chain (edited and deleted lines), the hook with the default rules, failing closed, custom rules, the MCP tools, and the Telegram channel with a fake API, including button presses from the wrong chat or user.

## License

MIT, copyright HolgerAI AB.
