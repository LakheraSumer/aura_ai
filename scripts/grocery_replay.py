"""Replay sandbox quick-commerce conversations against a running stack (TEST data only).

    python scripts/grocery_replay.py [base_url]

Uses the configured planner (Gemini free tier here): a few free model calls per scenario.
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
        self.c = httpx.Client(base_url=BASE, timeout=30)
        tok = self.c.post("/v1/auth/register", json={"email": f"grocery-{uuid.uuid4().hex[:6]}@example.test",
                                                     "password": "replay-test-password", "display_name": "R"})
        self.h = {"Authorization": f"Bearer {tok.json()['access_token']}"}
        self.task: dict = {}

    def _get(self, tid: str) -> dict:
        return self.c.get(f"/v1/tasks/{tid}", headers=self.h).json()

    def _last(self) -> str:
        time.sleep(1.5)
        conv = self.c.get("/v1/conversations", headers=self.h).json()[0]["id"]
        msgs = self.c.get(f"/v1/conversations/{conv}/messages", headers=self.h).json()
        return msgs[-1]["content"] if msgs and msgs[-1]["role"] == "assistant" else "(no reply yet)"

    def say(self, text: str) -> dict:
        tid = self.c.post("/v1/chat/messages", json={"text": text}, headers=self.h).json()["task_id"]
        for _ in range(240):
            self.task = self._get(tid)
            if self.task["status"] in DONE:
                break
            time.sleep(0.5)
        print(f"\n👤 {text}\n🤖 [{self.task['status']}] " + self._last().replace("\n", "\n   "))
        return self.task

    def decide(self, decision: str, task: dict | None = None) -> None:
        task = task or self.task
        step = next(s for s in task["steps"] if s["status"] == "AWAITING_APPROVAL")
        r = self.c.post(f"/v1/tasks/{task['id']}/approve", headers=self.h,
                        json={"approval_id": step["result"]["approval_id"], "decision": decision})
        if r.status_code >= 400:
            print(f"\n👆 [{decision}] on task {task['id'][:8]} -> HTTP {r.status_code} {r.text[:200]}")
            return
        for _ in range(120):
            t = self._get(task["id"])
            if t["status"] in {"COMPLETED", "PARTIALLY_COMPLETED", "FAILED", "CANCELLED"}:
                break
            time.sleep(0.5)
        print(f"\n👆 [{decision}] on task {task['id'][:8]}\n🤖 [{t['status']}] " + self._last().replace("\n", "\n   "))


def waiting_payment(task: dict) -> bool:
    return any(s["status"] == "AWAITING_APPROVAL" for s in task["steps"])


def main() -> int:
    print("=== 1. Named store, approve payment")
    a = Chat()
    if waiting_payment(a.say("order 2 milk, bread and 6 eggs from blinkit, deliver to Kothrud Pune")):
        a.decide("APPROVED")

    print("\n=== 2. Recipe + order the ingredients, reject payment")
    b = Chat()
    if waiting_payment(b.say("give me veg pulav recipe and order the ingredients from instamart, Kothrud Pune")):
        b.decide("REJECTED")

    print("\n=== 3. No store named (Hinglish) -> compare -> 'sabse sasta'")
    c = Chat()
    c.say("1 kg kanda, 1 kg tamatar aur paneer mangwa do, Kothrud Pune")
    if waiting_payment(c.say("sabse sasta")):
        c.decide("APPROVED")

    print("\n=== 4. Out of stock at the chosen store")
    d = Chat()
    d.say("order saffron and biryani masala from blinkit, Kothrud Pune")

    print("\n=== 5. No delivery area -> ask")
    e = Chat()
    e.say("order 2 maggi and a coke from zepto")
    e.say("Baner, Pune")

    print("\n=== 6. Change the cart; the old Pay button must not place the old order")
    f = Chat()
    old = f.say("order 2 milk and bread from blinkit, Kothrud Pune")
    new = f.say("remove the bread and add 6 eggs")
    if waiting_payment(old):
        f.decide("APPROVED", task=old)
    if waiting_payment(new):
        f.decide("APPROVED", task=new)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
