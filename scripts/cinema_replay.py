"""Replay the sandbox-cinema booking conversations against a running stack (TEST data only).

    python scripts/cinema_replay.py [base_url]

Scenarios: happy path with payment approval, housefull show -> pick another, requested row can't seat
everyone -> pick a suggested row -> reject payment, no theatre named -> choose theatre and show.
Uses the configured planner (Gemini free tier in this setup): a few free model calls per scenario.
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
        tok = self.c.post("/v1/auth/register", json={"email": f"cinema-{uuid.uuid4().hex[:6]}@example.test",
                                                     "password": "replay-test-password", "display_name": "R"})
        self.h = {"Authorization": f"Bearer {tok.json()['access_token']}"}
        self.task: dict = {}

    def _wait(self, tid: str) -> dict:
        for _ in range(240):
            t = self.c.get(f"/v1/tasks/{tid}", headers=self.h).json()
            if t["status"] in DONE:
                return t
            time.sleep(0.5)
        return t

    def _last_reply(self) -> str:
        time.sleep(1.5)
        conv = self.c.get("/v1/conversations", headers=self.h).json()[0]["id"]
        msgs = self.c.get(f"/v1/conversations/{conv}/messages", headers=self.h).json()
        return msgs[-1]["content"] if msgs and msgs[-1]["role"] == "assistant" else "(no reply yet)"

    def say(self, text: str) -> str:
        tid = self.c.post("/v1/chat/messages", json={"text": text}, headers=self.h).json()["task_id"]
        self.task = self._wait(tid)
        reply = self._last_reply()
        print(f"\n👤 {text}\n🤖 [{self.task['status']}] " + reply.replace("\n", "\n   "))
        return reply

    def decide(self, decision: str) -> str:
        step = next(s for s in self.task["steps"] if s["status"] == "AWAITING_APPROVAL")
        self.c.post(f"/v1/tasks/{self.task['id']}/approve", headers=self.h,
                    json={"approval_id": step["result"]["approval_id"], "decision": decision}).raise_for_status()
        for _ in range(120):
            t = self.c.get(f"/v1/tasks/{self.task['id']}", headers=self.h).json()
            if t["status"] in {"COMPLETED", "PARTIALLY_COMPLETED", "FAILED", "CANCELLED"}:
                break
            time.sleep(0.5)
        self.task = t
        reply = self._last_reply()
        print(f"\n👆 [{decision}]\n🤖 [{t['status']}] " + reply.replace("\n", "\n   "))
        return reply


def main() -> int:
    print("=== 1. Happy path: row A at City Pride Kothrud, 10:45 PM, approve payment")
    a = Chat()
    a.say("book a ramayan 2 person movie ticket pune also select 2 seats from A ROW from citipride kothrud "
          "10:45 pm show today")
    if any(s["status"] == "AWAITING_APPROVAL" for s in a.task["steps"]):
        a.decide("APPROVED")

    print("\n=== 2. Housefull 7 PM show -> choose another")
    b = Chat()
    b.say("book 2 tickets for Ramayana at City Pride Kothrud Pune today 7 pm")
    b.say("1")
    if any(s["status"] == "AWAITING_APPROVAL" for s in b.task["steps"]):
        b.decide("REJECTED")

    print("\n=== 3. Row can't seat everyone together -> pick a suggestion")
    c = Chat()
    c.say("book 8 tickets for Ramayana at citipride kothrud pune today 10:45 pm in row D")
    c.say("1")

    print("\n=== 4. No theatre named -> choose theatre, then show")
    d = Chat()
    d.say("Drishyam ke 2 tickets book karo Pune mein aaj")
    d.say("kothrud")
    if d.task["status"] == "WAITING_FOR_USER" and not any(
            s["status"] == "AWAITING_APPROVAL" for s in d.task["steps"]):
        d.say("10:45 wala")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
