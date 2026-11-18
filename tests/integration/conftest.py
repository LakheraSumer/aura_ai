"""Integration harness: real Postgres + real Temporal (docker compose), in-process workers, the FastAPI
app over ASGI, the sandbox website, and Playwright Chromium. Only channel HTTP (Meta / Telegram) is
replaced by a recording MockTransport - no real messages are ever sent."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest

_TMP = Path(tempfile.mkdtemp(prefix="apna-it-"))
# Hosts are overridable so the suite can run inside a container on the compose network
# (IT_PG=postgres:5432 IT_TEMPORAL=temporal:7233); defaults match the host-published ports.
PG = os.environ.get("IT_PG", "127.0.0.1:55432")
os.environ.update({
    "DATABASE_URL": f"postgresql+asyncpg://apna:apna_dev_only@{PG}/apna_test",
    "TEMPORAL_ADDRESS": os.environ.get("IT_TEMPORAL", "127.0.0.1:57233"),
    "TEMPORAL_NAMESPACE": "default",
    # never call a paid model: scripted plans for tests that register them, rule planner otherwise
    "ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "", "GEMINI_API_KEY": "", "PLANNER_PROVIDER": "scripted",
    "APNA_ENCRYPTION_KEY": "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",  # test-only key
    "RAZORPAY_KEY_ID": "", "RAZORPAY_KEY_SECRET": "", "RAZORPAY_WEBHOOK_SECRET": "", "PAYMENT_POLL_SECONDS": "1",
    "PRICE_WATCH_POLL_SECONDS": "1", "REMINDER_FOLLOWUP_SECONDS": "0", "TAVILY_API_KEY": "",
    "GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": "", "PUBLIC_BASE_URL": "",
    "WHATSAPP_ENABLED": "true", "WHATSAPP_PHONE_NUMBER_ID": "TEST_PNID", "WHATSAPP_ACCESS_TOKEN": "test-token",
    "WHATSAPP_APP_SECRET": "test-app-secret", "WHATSAPP_VERIFY_TOKEN": "test-verify",
    "WHATSAPP_ALLOWED_TEST_NUMBERS": json.dumps(["919800000001"]),
    "TELEGRAM_ENABLED": "true", "TELEGRAM_BOT_TOKEN": "123:test", "TELEGRAM_WEBHOOK_SECRET": "tg-secret",
    "BROWSER_PROFILE_ROOT": str(_TMP / "profiles"), "BROWSER_ARTIFACT_ROOT": str(_TMP / "artifacts"),
    "TEST_SITE_BASE_URL": "http://127.0.0.1:8899",
})

from apna.channels import outbound
from apna.channels.telegram import TelegramAdapter
from apna.channels.whatsapp import WhatsAppAdapter
from apna.core.config import get_settings

API_DIR = Path(__file__).resolve().parents[2] / "services" / "api"


def _chromium_installed() -> bool:
    try:
        from playwright._impl._driver import compute_driver_executable  # noqa: F401

        home = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", Path.home() / ".cache" / "ms-playwright"))
        return any(home.glob("chromium*")) or any(
            (Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright").glob("chromium*"))
    except Exception:  # noqa: BLE001
        return False


_BROWSER_TESTS = ("test_06_", "test_15_", "test_16_")


def pytest_collection_modifyitems(items):
    no_browser = not _chromium_installed()
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)
        if no_browser and item.name.startswith(_BROWSER_TESTS):
            item.add_marker(pytest.mark.skip(reason="Playwright Chromium not installed in this environment"))


@pytest.fixture(scope="session", autouse=True)
def migrated_db():
    """Fresh schema in apna_test, built by the real Alembic migration."""
    reset = (
        "import asyncio, asyncpg\n"
        "async def m():\n"
        f"    c = await asyncpg.connect('postgresql://apna:apna_dev_only@{PG}/apna_test')\n"
        "    await c.execute('DROP SCHEMA public CASCADE; CREATE SCHEMA public; '\n"
        "                    'CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pgcrypto;')\n"
        "    await c.close()\n"
        "asyncio.run(m())\n"
    )
    subprocess.run([sys.executable, "-c", reset], check=True, capture_output=True)
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=API_DIR, check=True, capture_output=True,
                   env={**os.environ})
    yield


@pytest.fixture(scope="session", autouse=True)
def test_site():
    import uvicorn
    from apna.browser.testsite import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8899, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield
    server.should_exit = True


class Recorder:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self._n = 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        self.requests.append({"url": str(req.url), "body": body})
        self._n += 1
        if "graph.facebook.com" in str(req.url):
            return httpx.Response(200, json={"messaging_product": "whatsapp", "messages": [{"id": f"wamid.T{self._n}"}]})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": self._n}})

    def texts(self, channel: str) -> list[str]:
        out = []
        for r in self.requests:
            if channel == "whatsapp" and "graph.facebook.com" in r["url"]:
                b = r["body"]
                out.append(b.get("text", {}).get("body") or b.get("interactive", {}).get("body", {}).get("text", ""))
            if channel == "telegram" and "api.telegram.org" in r["url"]:
                out.append(r["body"]["text"])
        return out


@pytest.fixture(scope="session")
def recorder():
    rec = Recorder()
    transport = httpx.MockTransport(rec.handler)
    outbound.ADAPTER_FACTORIES["whatsapp"] = lambda: WhatsAppAdapter(get_settings(), transport)
    outbound.ADAPTER_FACTORIES["telegram"] = lambda: TelegramAdapter(get_settings(), transport)
    return rec


@pytest.fixture(scope="session")
async def temporal():
    from temporalio.client import Client

    s = get_settings()
    client = await Client.connect(s.temporal_address, namespace=s.temporal_namespace)
    # The schema is rebuilt every run, so workflows left in the *test* namespace by earlier runs point at
    # rows that no longer exist; terminate them so their retries don't starve this run's workers.
    assert s.temporal_namespace == "default", "only ever clean the test namespace"
    async for wf in client.list_workflows("ExecutionStatus='Running'"):
        try:
            await client.get_workflow_handle(wf.id, run_id=wf.run_id).terminate("stale from a previous test run")
        except Exception:  # noqa: BLE001, S110 - already finished
            pass
    return client


class Workers:
    def __init__(self, client) -> None:
        self.client, self.tasks = client, []
        self.workers = []

    async def start(self):
        from apna.workflows.worker import build_workers

        self.workers = build_workers(self.client, default=True, browser=True)
        self.tasks = [asyncio.create_task(w.run()) for w in self.workers]
        await asyncio.sleep(0.3)

    async def stop(self):
        for w in self.workers:
            await w.shutdown()
        for t in self.tasks:
            try:
                await t
            except Exception:  # noqa: BLE001, S110 - worker tasks may end with a shutdown error
                pass
        self.tasks, self.workers = [], []


@pytest.fixture(scope="session")
async def workers(temporal, migrated_db, recorder):
    w = Workers(temporal)
    await w.start()
    yield w
    await w.stop()


@pytest.fixture(scope="session")
async def api(temporal, workers):
    from apna.api.main import create_app
    from apna.services.conversation import ConversationService

    app = create_app()
    app.state.temporal = temporal
    app.state.conversations = ConversationService(temporal)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as c:
        yield c


_counter = iter(range(10_000))


@pytest.fixture
async def user(api):
    n = next(_counter)
    r = await api.post("/v1/auth/register", json={"email": f"user{n}-{time.time_ns()}@example.test",
                                                  "password": "correct horse battery", "display_name": "Asha Patil"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def wait_task(api, headers, task_id: str, statuses: set[str], timeout: float = 45) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        r = await api.get(f"/v1/tasks/{task_id}", headers=headers)
        last = r.json()
        if last["status"] in statuses:
            return last
        await asyncio.sleep(0.3)
    raise AssertionError(f"task did not reach {statuses}; last={json.dumps(last, default=str)[:2000]}")


async def say(api, headers, text: str) -> str:
    r = await api.post("/v1/chat/messages", json={"text": text}, headers=headers)
    assert r.status_code == 202, r.text
    return r.json().get("task_id") or r.json().get("approval_id")
