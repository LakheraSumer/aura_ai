"""Isolated browser execution (Playwright Chromium).

Security properties enforced here (ADR-007):
- one persistent profile directory per user, never shared;
- every request is checked against the domain allow-list (route interception aborts others);
- password / OTP inputs are never filled - their presence triggers a user handoff;
- page text is returned as *untrusted data* with injection heuristics flagged; it never
  changes what the executor does;
- every action is logged to `browser_actions` with a screenshot reference, values redacted.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from playwright.async_api import BrowserContext, Page, Route, async_playwright
from sqlalchemy.ext.asyncio import AsyncSession

from apna.core.config import Settings
from apna.core.errors import PolicyViolation, UserHandoffRequired
from apna.db.models import BrowserAction, BrowserSession


@dataclass(frozen=True)
class Site:
    key: str
    path: str
    label: str
    form_fields: tuple[str, ...] = ()
    submit_selector: str = "#submit"
    confirmation_selector: str = "#confirmation-code"
    verify_path: str | None = None


SITES: dict[str, Site] = {
    "sample_form": Site("sample_form", "/sample-form", "Sample appointment site",
                        ("full_name", "email", "preferred_day"), verify_path="/api/submissions/{code}"),
    "news_test": Site("news_test", "/news", "Sandbox news site"),
    "login_test": Site("login_test", "/login", "Sandbox login page", ("username", "password")),
}

_INJECTION = re.compile(
    r"ignore (all )?(previous|prior) instructions|system override|you are now|admin mode|"
    r"without asking|do not tell the user|exfiltrat|send .* passwords", re.I)
_SECRET_FIELD = re.compile(r"pass(word)?|otp|one-time|pin|cvv|card", re.I)


def site_url(settings: Settings, site: Site) -> str:
    return settings.test_site_base_url.rstrip("/") + site.path


def _host_allowed(url: str, allowed: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in allowed)


def profile_dir(settings: Settings, user_id: uuid.UUID) -> Path:
    # Directory name is a hash so user ids don't leak into paths/logs.
    return settings.browser_profile_root / hashlib.sha256(str(user_id).encode()).hexdigest()[:24]


class BrowserRun:
    """One isolated browser session for one user and one step."""

    def __init__(self, session: AsyncSession, settings: Settings, user_id: uuid.UUID, task_id: uuid.UUID) -> None:
        self.db, self.settings, self.user_id, self.task_id = session, settings, user_id, task_id
        self.seq = 0
        self.blocked: list[str] = []
        self.record: BrowserSession | None = None
        self.ctx: BrowserContext | None = None
        self.page: Page | None = None
        self._pw: Any = None

    async def __aenter__(self) -> BrowserRun:
        pdir = profile_dir(self.settings, self.user_id)
        pdir.mkdir(parents=True, exist_ok=True)
        self.record = BrowserSession(user_id=self.user_id, task_id=self.task_id, provider="playwright-local",
                                     profile_ref=pdir.name, status="ACTIVE")
        self.db.add(self.record)
        await self.db.flush()
        self._pw = await async_playwright().start()
        self.ctx = await self._pw.chromium.launch_persistent_context(
            str(pdir), headless=self.settings.browser_headless, accept_downloads=False,
            args=["--disable-extensions", "--no-first-run"],
        )
        self.ctx.set_default_timeout(self.settings.browser_step_timeout_ms)
        await self.ctx.route("**/*", self._guard)
        self.page = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.record:
            self.record.status = "FAILED" if exc[0] else ("HANDOFF" if self.record.status == "HANDOFF" else "CLOSED")
            self.record.closed_at = datetime.now(UTC)
        if self.ctx:
            await self.ctx.close()
        if self._pw:
            await self._pw.stop()

    async def _guard(self, route: Route) -> None:
        url = route.request.url
        if url.startswith(("data:", "blob:", "about:")) or _host_allowed(url, self.settings.browser_allowed_domains):
            await route.continue_()
        else:
            self.blocked.append(urlparse(url).hostname or url)
            await route.abort("blockedbyclient")

    async def log(self, action: str, target: str | None, outcome: str, detail: dict[str, Any] | None = None,
                  screenshot: bool = True) -> str | None:
        assert self.record and self.page
        self.seq += 1
        ref = None
        if screenshot:
            out = self.settings.browser_artifact_root / str(self.record.id)
            out.mkdir(parents=True, exist_ok=True)
            path = out / f"{self.seq:03d}-{action}.png"
            await self.page.screenshot(path=str(path), full_page=True)
            ref = str(path)
        self.db.add(BrowserAction(session_id=self.record.id, seq=self.seq, action=action, target=target,
                                  detail=detail or {}, outcome=outcome, screenshot_ref=ref))
        return ref

    async def open(self, url: str) -> None:
        assert self.page
        if not _host_allowed(url, self.settings.browser_allowed_domains):
            await self.log("open_url", url, "BLOCKED", screenshot=False)
            raise PolicyViolation(f"Domain not on the allow-list: {urlparse(url).hostname}")
        await self.page.goto(url, wait_until="domcontentloaded")
        await self.log("open_url", url, "OK")

    async def secret_fields_present(self) -> list[str]:
        assert self.page
        names: list[str] = await self.page.eval_on_selector_all(
            "input", "els => els.filter(e => e.type === 'password' || /otp|one-time/i.test(e.name + ' ' + "
                     "(e.autocomplete||''))).map(e => e.name || e.type)")
        return names


async def run_read_page(run: BrowserRun, site: Site) -> dict[str, Any]:
    await run.open(site_url(run.settings, site))
    assert run.page
    headline = (await run.page.locator("h1").first.inner_text()).strip()
    text = (await run.page.locator("main").inner_text()).strip()
    flagged = bool(_INJECTION.search(text))
    await run.log("read_page", site.key, "OK", {"chars": len(text), "injection_flagged": flagged}, screenshot=False)
    return {
        "site": site.label, "headline": headline,
        # Untrusted content: rendered to the user as data, never fed back as instructions.
        "untrusted_text": text[:4000], "untrusted_instructions_detected": flagged,
        "browser_session_id": str(run.record.id) if run.record else None,
    }


async def run_submit_form(run: BrowserRun, site: Site, fields: dict[str, str]) -> dict[str, Any]:
    unknown = set(fields) - set(site.form_fields)
    if unknown:
        raise PolicyViolation(f"Fields not declared for {site.key}: {sorted(unknown)}")
    if any(_SECRET_FIELD.search(k) for k in fields):
        raise UserHandoffRequired("Form asks for a secret; handing off to the user.")
    await run.open(site_url(run.settings, site))
    assert run.page and run.record
    secrets_on_page = await run.secret_fields_present()
    if secrets_on_page:
        run.record.status = "HANDOFF"
        await run.log("detect_secret_fields", site.key, "HANDOFF", {"fields": secrets_on_page})
        raise UserHandoffRequired(f"{site.label} asks for a password/OTP.")

    for name, value in fields.items():
        loc = run.page.locator(f"[name='{name}']")
        tag = await loc.evaluate("e => e.tagName")
        if tag == "SELECT":
            await loc.select_option(value)
        else:
            await loc.fill(value)
        await run.log("type_text" if tag != "SELECT" else "select_option", name, "OK",
                      {"value_sha256": hashlib.sha256(value.encode()).hexdigest()[:12]}, screenshot=False)
    await run.log("form_filled", site.key, "OK")
    await run.page.locator(site.submit_selector).click()
    await run.log("click", site.submit_selector, "OK", screenshot=False)

    code_loc = run.page.locator(site.confirmation_selector)
    await code_loc.wait_for(state="visible")
    code = (await code_loc.inner_text()).strip()
    await run.log("read_confirmation", site.confirmation_selector, "OK", {"code": code})

    # Independent verification against the site's own record (not just "the click worked").
    verified = False
    if site.verify_path:
        verify_url = run.settings.test_site_base_url.rstrip("/") + site.verify_path.format(code=code)
        if not _host_allowed(verify_url, run.settings.browser_allowed_domains):
            raise PolicyViolation("Verification endpoint is not allow-listed.")
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(verify_url)
        verified = resp.status_code == 200 and all(resp.json().get(k) == v for k, v in fields.items())
    return {
        "site": site.label, "confirmation_code": code, "verified_with_site": verified,
        "browser_session_id": str(run.record.id), "actions_logged": run.seq, "blocked_requests": run.blocked,
    }
