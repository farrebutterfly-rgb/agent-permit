import io
import json
import time

import pytest

from agent_permit import config, fingerprint
from agent_permit.hook import evaluate, main as hook_main
from agent_permit.server import tools
from agent_permit.store import Store
from agent_permit.telegram import TelegramChannel


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "permits.db"))


@pytest.fixture
def cfg(tmp_path):
    return config.load({"AGENT_PERMIT_HOME": str(tmp_path)})


def bash(cmd, session="sess-1234"):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": session}


# ------------------------------------------------------------------ store


def test_fingerprint_is_key_order_independent():
    assert fingerprint({"a": 1, "b": [1, 2]}) == fingerprint({"b": [1, 2], "a": 1})
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})


def test_request_is_deduplicated_while_open(store):
    p1, c1 = store.request({"x": 1}, "do x")
    p2, c2 = store.request({"x": 1}, "do x again")
    assert c1 and not c2 and p1.id == p2.id


def test_approval_covers_exact_action_once(store):
    p, _ = store.request({"cmd": "git push origin main"}, "push")
    store.decide(p.id, True, by="test")
    assert store.consume({"cmd": "git push --force origin main"}) is None
    assert store.consume({"cmd": "git push origin main"}).status == "used"
    assert store.consume({"cmd": "git push origin main"}) is None


def test_multiple_uses(store):
    p, _ = store.request({"x": 1}, "x")
    store.decide(p.id, True, by="test", uses=2)
    assert store.consume({"x": 1}).uses_left == 1
    assert store.consume({"x": 1}).status == "used"
    assert store.consume({"x": 1}) is None


def test_denied_is_never_usable(store):
    p, _ = store.request({"x": 1}, "x")
    store.decide(p.id, False, by="test")
    assert store.consume({"x": 1}) is None
    with pytest.raises(ValueError):
        store.decide(p.id, True, by="test")


def test_grant_expires(store):
    p, _ = store.request({"x": 1}, "x")
    store.decide(p.id, True, by="test", grant_ttl=0.05)
    time.sleep(0.1)
    assert store.consume({"x": 1}) is None
    assert store.get(p.id).status == "expired"


def test_request_expires_and_can_be_asked_again(store):
    p, _ = store.request({"x": 1}, "x", request_ttl=0.05)
    time.sleep(0.1)
    assert store.get(p.id).status == "expired"
    with pytest.raises(ValueError):
        store.decide(p.id, True, by="test")
    p2, created = store.request({"x": 1}, "x")
    assert created and p2.id != p.id


def test_approval_needs_positive_ttl(store):
    p, _ = store.request({"x": 1}, "x")
    with pytest.raises(ValueError):
        store.decide(p.id, True, by="test", grant_ttl=0)


def test_unknown_permit(store):
    with pytest.raises(KeyError):
        store.decide(999, True, by="test")


# ------------------------------------------------------------------ audit


def test_audit_chain_verifies(store):
    p, _ = store.request({"x": 1}, "x")
    store.decide(p.id, True, by="test")
    store.consume({"x": 1})
    ok, n, _ = store.audit.verify()
    assert ok and n == 3


def test_audit_detects_edit(store):
    p, _ = store.request({"x": 1}, "x")
    store.decide(p.id, False, by="test")
    lines = open(store.audit.path).read().splitlines()
    e = json.loads(lines[1])
    e["event"] = "approved"
    lines[1] = json.dumps(e)
    open(store.audit.path, "w").write("\n".join(lines) + "\n")
    ok, n, msg = store.audit.verify()
    assert not ok and n == 2 and "hash" in msg


def test_audit_detects_deleted_line(store):
    for i in range(3):
        store.request({"x": i}, "x")
    lines = open(store.audit.path).read().splitlines()
    open(store.audit.path, "w").write("\n".join([lines[0], lines[2]]) + "\n")
    ok, _, msg = store.audit.verify()
    assert not ok and "chain broken" in msg


# ------------------------------------------------------------------- hook


def test_unmatched_tool_passes_without_permit(cfg, store):
    assert evaluate(bash("ls -la"), cfg, store) == (0, "")
    assert evaluate({"tool_name": "Read", "tool_input": {"file_path": "/x"}}, cfg, store) == (0, "")
    assert store.list() == []


@pytest.mark.parametrize(
    "cmd",
    [
        "git push origin main",
        "cd repo && git push",
        "gh pr create --fill",
        "npm publish",
        "curl -X POST https://example.com/api -d '{}'",
        "terraform apply -auto-approve",
        "kubectl delete pod x",
        "vercel deploy --prod",
    ],
)
def test_risky_commands_are_blocked_and_requested(cfg, store, cmd):
    sent = []
    code, msg = evaluate(bash(cmd), cfg, store, notify=sent.append)
    assert code == 2 and "Permit #" in msg
    assert len(sent) == 1 and sent[0].status == "pending"


def test_mcp_write_tools_are_matched(cfg, store):
    ev = {"tool_name": "mcp__slack__send_message", "tool_input": {"channel": "x", "text": "hi"}}
    assert evaluate(ev, cfg, store)[0] == 2
    ev = {"tool_name": "mcp__slack__read_channel", "tool_input": {"channel": "x"}}
    assert evaluate(ev, cfg, store)[0] == 0


def test_retry_after_approval_passes_once(cfg, store):
    sent = []
    code, _ = evaluate(bash("git push origin main"), cfg, store, notify=sent.append)
    assert code == 2
    code, _ = evaluate(bash("git push origin main"), cfg, store, notify=sent.append)
    assert code == 2 and len(sent) == 1  # asked once, not twice
    store.decide(sent[0].id, True, by="test")
    assert evaluate(bash("git push --force origin main"), cfg, store, notify=sent.append)[0] == 2
    assert evaluate(bash("git push origin main"), cfg, store)[0] == 0
    assert evaluate(bash("git push origin main"), cfg, store, notify=sent.append)[0] == 2


def test_block_holds_when_notification_fails(cfg, store):
    def boom(p):
        raise OSError("offline")

    assert evaluate(bash("git push"), cfg, store, notify=boom)[0] == 2
    assert any(e["event"] == "notify_error" for e in store.audit.entries())


def test_hook_main_reads_stdin_and_fails_closed(cfg, store):
    assert hook_main(cfg, store, stdin=io.StringIO(json.dumps(bash("git push")))) == 2
    assert hook_main(cfg, store, stdin=io.StringIO("not json")) == 2
    assert hook_main(cfg, store, stdin=io.StringIO(json.dumps(bash("echo hi")))) == 0


def test_custom_rules_file(tmp_path, store):
    (tmp_path / "rules.json").write_text(json.dumps([{"tool": "^Write$", "input": "\\.env", "label": "secrets file"}]))
    cfg = config.load({"AGENT_PERMIT_HOME": str(tmp_path)})
    assert evaluate({"tool_name": "Write", "tool_input": {"file_path": "/app/.env"}}, cfg, store)[0] == 2
    assert evaluate(bash("git push"), cfg, store)[0] == 0


# ------------------------------------------------------------------ server


def test_mcp_tools_flow(cfg, store):
    request_permit, check_permit, use_permit, _ = tools(cfg, store, sleep=lambda s: None)
    action = json.dumps({"tool": "send_email", "to": "a@example.com"})
    r = request_permit(action, "Send the offer to a@example.com")
    assert r["status"] == "pending" and r["created"]
    assert use_permit(action) == {"allowed": False, "id": None}
    store.decide(r["id"], True, by="test")
    assert check_permit(r["id"])["status"] == "approved"
    assert use_permit(action)["allowed"] is True
    assert use_permit(action)["allowed"] is False
    assert check_permit(12345)["status"] == "unknown"


def test_mcp_wait_returns_on_decision(cfg, store):
    calls = []

    def sleep(s):
        calls.append(s)
        if len(calls) == 2:
            store.decide(1, False, by="test")

    request_permit, _, _, _ = tools(cfg, store, sleep=sleep)
    r = request_permit("delete the bucket", "Delete bucket logs-old", wait_seconds=60)
    assert r["status"] == "denied" and len(calls) == 2


def test_mcp_server_builds(cfg, store):
    pytest.importorskip("mcp")
    from agent_permit.server import build

    assert build(cfg, store) is not None


# ---------------------------------------------------------------- telegram


class FakeApi:
    def __init__(self):
        self.calls = []

    def __call__(self, method, params):
        self.calls.append((method, params))
        return []


def press(pid, verb="y", chat=42, user=7, update_id=1):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "cb1",
            "data": f"{verb}:{pid}",
            "from": {"id": user},
            "message": {"message_id": 5, "chat": {"id": chat}, "text": "Permit"},
        },
    }


def test_telegram_notify_has_buttons(store):
    api = FakeApi()
    ch = TelegramChannel(store, api, chat_id=42)
    p, _ = store.request({"x": 1}, "Push the release")
    ch.notify(p)
    method, params = api.calls[0]
    assert method == "sendMessage" and params["chat_id"] == 42
    buttons = params["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == [f"y:{p.id}", f"n:{p.id}"]


def test_telegram_approve_and_deny(store):
    ch = TelegramChannel(store, FakeApi(), chat_id=42, allowed_user_ids=[7])
    p1, _ = store.request({"x": 1}, "a")
    p2, _ = store.request({"x": 2}, "b")
    assert ch.handle(press(p1.id, "y")) == f"#{p1.id} approved"
    assert ch.handle(press(p2.id, "n")) == f"#{p2.id} denied"
    assert store.get(p1.id).decided_by == "telegram:7"


def test_telegram_rejects_other_chats_and_users(store):
    ch = TelegramChannel(store, FakeApi(), chat_id=42, allowed_user_ids=[7])
    p, _ = store.request({"x": 1}, "a")
    assert ch.handle(press(p.id, chat=99)) == "rejected"
    assert ch.handle(press(p.id, user=8)) == "rejected"
    assert store.get(p.id).status == "pending"
    assert sum(e["event"] == "rejected_callback" for e in store.audit.entries()) == 2


def test_telegram_double_press_does_not_flip(store):
    ch = TelegramChannel(store, FakeApi(), chat_id=42)
    p, _ = store.request({"x": 1}, "a")
    ch.handle(press(p.id, "y"))
    assert "not pending" in ch.handle(press(p.id, "n"))
    assert store.get(p.id).status == "approved"


def test_telegram_bad_data(store):
    ch = TelegramChannel(store, FakeApi(), chat_id=42)
    upd = press(1)
    upd["callback_query"]["data"] = "rm -rf"
    assert ch.handle(upd) == "bad data"


def test_telegram_poll_advances_offset(store):
    p, _ = store.request({"x": 1}, "a")

    class Api(FakeApi):
        def __call__(self, method, params):
            super().__call__(method, params)
            return [press(p.id, update_id=10)] if method == "getUpdates" else True

    ch = TelegramChannel(store, Api(), chat_id=42)
    assert ch.poll_once() == 1 and ch.offset == 11
    assert store.get(p.id).status == "approved"


# ------------------------------------------------------------------ freeze


def test_freeze_blocks_every_tool_call_even_without_rule(cfg, store):
    assert evaluate({"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}}, cfg, store)[0] == 0
    store.freeze("suspected leak via mail", by="test")
    code, msg = evaluate({"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}}, cfg, store)
    assert code == 2 and "FROZEN" in msg and "suspected leak" in msg
    code, msg = evaluate(bash("git push origin main"), cfg, store)
    assert code == 2 and "FROZEN" in msg
    store.unfreeze(by="test")
    assert evaluate({"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}}, cfg, store)[0] == 0


def test_freeze_beats_an_existing_approval(store):
    p, _ = store.request({"x": 1}, "x")
    store.decide(p.id, True, by="test")
    store.freeze("stop everything", by="test")
    assert store.consume({"x": 1}) is None
    store.unfreeze(by="test")
    assert store.consume({"x": 1}).status == "used"


def test_freeze_is_in_the_audit_chain(store):
    store.freeze("drill", by="test")
    store.consume({"x": 1})
    store.unfreeze(by="test")
    events = [e["event"] for e in store.audit.entries()]
    assert events[-3:] == ["freeze", "frozen_refusal", "unfreeze"]
    assert store.audit.verify()[0]


def test_freeze_survives_a_new_store_instance(tmp_path):
    path = str(tmp_path / "p.db")
    Store(path).freeze("persisted", by="test")
    assert Store(path).frozen()["reason"] == "persisted"


def test_mcp_tools_refuse_while_frozen(cfg, store):
    request_permit, check_permit, use_permit, freeze_status = tools(cfg, store, sleep=lambda s: None)
    assert freeze_status() == {"frozen": False}
    store.freeze("incident", by="test")
    r = request_permit(json.dumps({"tool": "send_email"}), "Send it")
    assert r["status"] == "frozen" and r["id"] is None and store.list() == []
    assert use_permit(json.dumps({"tool": "send_email"}))["allowed"] is False
    assert freeze_status()["frozen"] is True and freeze_status()["reason"] == "incident"


def message(text, chat=42, user=7, update_id=9):
    return {"update_id": update_id, "message": {"message_id": 6, "chat": {"id": chat}, "from": {"id": user}, "text": text}}


def test_telegram_freeze_and_unfreeze_commands(store):
    api = FakeApi()
    ch = TelegramChannel(store, api, chat_id=42, allowed_user_ids=[7])
    assert ch.handle(message("/freeze agent mailed the wrong client")).startswith("FROZEN")
    assert store.frozen()["reason"] == "agent mailed the wrong client" and store.frozen()["by"] == "telegram:7"
    assert ch.handle(message("/unfreeze")).startswith("released")
    assert store.frozen() is None
    assert api.calls[-1][0] == "sendMessage"


def test_telegram_freeze_rejects_strangers(store):
    api = FakeApi()
    ch = TelegramChannel(store, api, chat_id=42, allowed_user_ids=[7])
    assert ch.handle(message("/freeze now", user=8)) == "rejected"
    assert ch.handle(message("/freeze now", chat=43)) == "rejected"
    assert ch.handle(message("hello")) is None
    assert store.frozen() is None
