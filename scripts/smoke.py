"""Smoke test against a running stack (API on :8800 + worker). Uses mocks only; sends nothing real.

    python scripts/smoke.py [base_url]
"""

from __future__ import annotations

import sys
import time
import uuid

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8800"
TERMINAL = {"COMPLETED", "PARTIALLY_COMPLETED", "FAILED", "CANCELLED", "EXPIRED", "WAITING_FOR_USER"}


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=15)
    print("readyz:", c.get("/readyz").json())
    r = c.post("/v1/auth/register", json={"email": f"smoke-{uuid.uuid4().hex[:8]}@example.test",
                                          "password": "smoke-test-password", "display_name": "Smoke Tester"})
    r.raise_for_status()
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    for text in ("Kal subah 8 baje mummy ke liye medicine reminder laga de",
                 "Draft a response to my latest meeting email"):
        tid = c.post("/v1/chat/messages", json={"text": text}, headers=h).json()["task_id"]
        for _ in range(120):
            task = c.get(f"/v1/tasks/{tid}", headers=h).json()
            if task["status"] in TERMINAL:
                break
            time.sleep(0.5)
        print(f"\n> {text}\n  task={task['status']} planner={task['planner']} lang={task['language']}")
        for s in task["steps"]:
            print(f"  {s['key']} {s['tool']:<16} {s['status']}")
    conv = c.get("/v1/conversations", headers=h).json()[0]["id"]
    time.sleep(1)
    print("\nconversation:")
    for m in c.get(f"/v1/conversations/{conv}/messages", headers=h).json():
        print(f"  [{m['role']}] {m['content']}".replace("\n", "\n      "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
