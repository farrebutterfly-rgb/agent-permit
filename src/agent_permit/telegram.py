"""Telegram channel: a message with Approve and Deny buttons, decided by long polling.

Long polling means no public web server is needed: the poller calls getUpdates and
the phone talks to Telegram. Only button presses and the commands /freeze and
/unfreeze from the configured chat and, if set, the configured user ids are accepted.
Everything else is logged and ignored.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterable, Optional

from .store import Permit, Store

Api = Callable[[str, dict], Any]


def http_api(token: str, timeout: float = 70) -> Api:
    base = f"https://api.telegram.org/bot{token}/"

    def call(method: str, params: dict) -> Any:
        data = urllib.parse.urlencode(
            {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in params.items()}
        ).encode()
        with urllib.request.urlopen(base + method, data=data, timeout=timeout) as r:
            body = json.load(r)
        if not body.get("ok"):
            raise RuntimeError(f"telegram {method}: {body.get('description')}")
        return body["result"]

    return call


def _fmt_ttl(seconds: float) -> str:
    return f"{int(seconds // 60)} min" if seconds < 3600 else f"{seconds / 3600:g} h"


class TelegramChannel:
    def __init__(
        self,
        store: Store,
        api: Api,
        chat_id: int,
        allowed_user_ids: Optional[Iterable[int]] = None,
        grant_ttl: float = 30 * 60,
    ) -> None:
        self.store = store
        self.api = api
        self.chat_id = int(chat_id)
        self.allowed = {int(u) for u in allowed_user_ids} if allowed_user_ids else None
        self.grant_ttl = grant_ttl
        self.offset = 0

    def notify(self, p: Permit) -> None:
        text = (
            f"Permit #{p.id} from {p.agent}\n\n{p.summary}\n\n"
            f"Action {p.short}. Approval covers this exact action once, for {_fmt_ttl(self.grant_ttl)}."
        )
        self.api(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": text,
                "reply_markup": {
                    "inline_keyboard": [[
                        {"text": "Approve", "callback_data": f"y:{p.id}"},
                        {"text": "Deny", "callback_data": f"n:{p.id}"},
                    ]]
                },
            },
        )

    def handle(self, update: dict) -> Optional[str]:
        """Apply one update. Returns a short description of what happened, for logs and tests."""
        cq = update.get("callback_query")
        if not cq:
            return self._command(update.get("message") or {})
        msg = cq.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        user = (cq.get("from") or {}).get("id")
        if chat != self.chat_id or (self.allowed is not None and user not in self.allowed):
            self.store.audit.append("rejected_callback", chat=chat, user=user)
            self.api("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "Not allowed"})
            return "rejected"
        try:
            verb, pid = cq.get("data", "").split(":", 1)
            pid = int(pid)
            if verb not in ("y", "n"):
                raise ValueError(verb)
        except ValueError:
            self.api("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "Unknown button"})
            return "bad data"
        try:
            p = self.store.decide(pid, verb == "y", by=f"telegram:{user}", grant_ttl=self.grant_ttl)
            result = f"#{pid} {p.status}"
        except (KeyError, ValueError) as e:
            result = str(e)
        self.api("answerCallbackQuery", {"callback_query_id": cq["id"], "text": result})
        if msg.get("message_id"):
            self.api(
                "editMessageText",
                {"chat_id": self.chat_id, "message_id": msg["message_id"], "text": f"{msg.get('text', '')}\n\n{result}"},
            )
        return result

    def _command(self, msg: dict) -> Optional[str]:
        """/freeze <reason> and /unfreeze typed in the chat: the emergency stop from the phone."""
        text = (msg.get("text") or "").strip()
        if not text.startswith("/freeze") and not text.startswith("/unfreeze"):
            return None
        chat = (msg.get("chat") or {}).get("id")
        user = (msg.get("from") or {}).get("id")
        if chat != self.chat_id or (self.allowed is not None and user not in self.allowed):
            self.store.audit.append("rejected_command", chat=chat, user=user)
            return "rejected"
        if text.startswith("/unfreeze"):
            self.store.unfreeze(by=f"telegram:{user}")
            result = "released: agents may act again"
        else:
            reason = text[len("/freeze"):].strip() or "no reason given"
            self.store.freeze(reason, by=f"telegram:{user}")
            result = f"FROZEN: every agent call is blocked until you send /unfreeze. Reason: {reason}"
        self.api("sendMessage", {"chat_id": self.chat_id, "text": result})
        return result

    def poll_once(self, timeout: int = 50) -> int:
        updates = self.api("getUpdates", {"offset": self.offset, "timeout": timeout,
                                          "allowed_updates": ["callback_query", "message"]})
        for u in updates:
            self.offset = max(self.offset, u["update_id"] + 1)
            self.handle(u)
        return len(updates)

    def run(self) -> None:
        while True:
            try:
                self.poll_once()
            except Exception as e:  # network errors: keep polling
                self.store.audit.append("poll_error", error=str(e)[:200])
                time.sleep(5)
