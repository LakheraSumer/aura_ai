"""Replay commerce / weather / handoff conversations against a running stack with the configured planner.

    python scripts/commerce_replay.py [base_url]

Sandbox stores and TEST payments only (unless Razorpay test keys are configured): nothing real is ordered.
"""

from __future__ import annotations

import sys
import time
import uuid

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8800"
DONE = {"COMPLETED", "PARTIALLY_COMPLETED", "FAILED", "CANCELLED", "EXPIRED", "WAITING_FOR_USER"}


class Chat:
    def __init__(self) -> None:
        self.c = httpx.Client(base_url=BASE, timeout=60)
        r = self.c.post("/v1/auth/register", json={"email": f"replay-{uuid.uuid4().hex[:6]}@example.test",
                                                   "password": "replay-test-password", "display_name": "R"})
        self.h = {"Authorization": f"Bearer {r.json()['access_token']}"}
        self.task: dict = {}

    def _last(self) -> str:
        time.sleep(1.5)
        conv = self.c.get("/v1/conversations", headers=self.h).json()[0]["id"]
        msgs = self.c.get(f"/v1/conversations/{conv}/messages", headers=self.h).json()
        return msgs[-1]["content"] if msgs and msgs[-1]["role"] == "assistant" else "(no reply yet)"

    def say(self, text: str) -> None:
        tid = self.c.post("/v1/chat/messages", json={"text": text}, headers=self.h).json().get("task_id")
        if tid:
            for _ in range(240):
                self.task = self.c.get(f"/v1/tasks/{tid}", headers=self.h).json()
                if self.task["status"] in DONE:
                    break
                time.sleep(0.5)
        print(f"\n👤 {text}\n🤖 " + self._last().replace("\n", "\n   "))

    def approve(self) -> None:
        step = next((s for s in self.task.get("steps", []) if s["status"] == "AWAITING_APPROVAL"), None)
        if step is None:
            print("\n(no approval pending)")
            return
        self.c.post(f"/v1/tasks/{self.task['id']}/approve", headers=self.h,
                    json={"approval_id": step["result"]["approval_id"], "decision": "APPROVED"})
        for _ in range(120):
            t = self.c.get(f"/v1/tasks/{self.task['id']}", headers=self.h).json()
            if t["status"] in DONE - {"WAITING_FOR_USER"}:
                break
            time.sleep(0.5)
        print("\n👆 [Approve]\n🤖 " + self._last().replace("\n", "\n   "))


def main() -> int:
    a = Chat()
    a.say("Blinkit ya Zepto se 2 litre milk aur 12 eggs mangwa do, jo total mein sasta ho. Kothrud, Pune")
    a.say("1")
    a.say("ek aur 6 eggs add kar do")
    a.approve()
    a.say("what happened to my order?")
    b = Chat()
    b.say("kal Pune mein baarish hogi?")
    b.say("book an uber from Kothrud to Pune airport")
    b.say("Goa mein 20 se 22 November 2 logon ke liye hotel dhoondo")
    b.say("my upi pin is 4321")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
