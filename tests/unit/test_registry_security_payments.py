"""Capability enforcement, secret handling and payment-gateway plumbing (no network, no DB)."""

import base64
import hashlib
import hmac
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from apna.core.config import Settings, get_settings
from apna.providers import payments, registry
from apna.providers.types import CapabilityStatus as S
from apna.providers.types import Operation as Op

REQUESTED = ["blinkit", "zepto", "instamart", "bigbasket", "flipkart_minutes", "swiggy_food", "zomato", "phonepe_pg",
             "google_pay", "razorpay", "uber", "rapido", "urban_company", "skyscanner", "booking_com", "agoda", "tripcom",
             "tripadvisor", "makemytrip", "redbus", "open_meteo"]


# ------------------------------------------------------------------ capability registry


def test_every_requested_provider_is_registered_with_evidence():
    for pid in REQUESTED:
        spec = registry.REGISTRY[pid]
        assert spec.evidence and spec.homepage, pid


def test_no_real_provider_claims_live_ordering_or_booking():
    for pid in REQUESTED:
        for op in (Op.CREATE_CART, Op.INITIATE_CHECKOUT, Op.CREATE_BOOKING):
            assert registry.status(pid, op, {}) != S.LIVE, (pid, op)
    assert registry.status("open_meteo", Op.SEARCH) == S.LIVE
    assert registry.status("sandbox_grocery", Op.CREATE_CART) == S.SANDBOX


def test_require_explains_partner_and_handoff_limits():
    with pytest.raises(registry.CapabilityUnavailable) as e:
        registry.require("instamart", Op.CREATE_CART, {})
    assert "Builders Club" in e.value.message and e.value.details["status"] == "partner-access-required"
    with pytest.raises(registry.CapabilityUnavailable) as e:
        registry.require("blinkit", Op.CREATE_CART, {})
    assert e.value.details["status"] == "unsupported" and e.value.details["handoff"].startswith("https://blinkit.com")
    assert registry.require("sandbox_grocery", Op.UPDATE_CART, {}) == S.SANDBOX


def test_credentials_make_a_built_adapter_live_but_never_an_unbuilt_one():
    keys = {"RAZORPAY_KEY_ID": "rzp_test_x", "RAZORPAY_KEY_SECRET": "s", "RAZORPAY_WEBHOOK_SECRET": "w"}
    assert registry.status("razorpay", Op.INITIATE_CHECKOUT, {}) == S.CREDENTIALS_REQUIRED
    assert registry.status("razorpay", Op.INITIATE_CHECKOUT, keys) == S.LIVE
    phonepe = {"PHONEPE_CLIENT_ID": "a", "PHONEPE_CLIENT_SECRET": "b", "PHONEPE_CLIENT_VERSION": "1"}
    assert registry.status("phonepe_pg", Op.INITIATE_CHECKOUT, phonepe) == S.CREDENTIALS_REQUIRED  # no adapter yet


def test_handoff_links_use_only_verified_formats():
    assert registry.handoff_link("blinkit", {"query": "milk"}) == "https://blinkit.com/s/?q=milk"
    assert registry.handoff_link("zepto", {"query": "milk"}) == "https://www.zepto.com/"  # search link redirected
    uber = urlparse(registry.handoff_link("uber", {"dropoff": "Pune Airport"}))
    assert uber.netloc == "m.uber.com" and parse_qs(uber.query)["dropoff[formatted_address]"] == ["Pune Airport"]
    assert registry.handoff_link("redbus", {"origin": "Pune", "destination": "Mumbai"}) == \
        "https://www.redbus.in/bus-tickets/pune-to-mumbai"
    hotel = parse_qs(urlparse(registry.handoff_link("booking_com", {"destination": "Goa", "checkin": "2026-11-20",
                                                                      "checkout": "2026-11-22", "adults": 2})).query)
    assert hotel["ss"] == ["Goa"] and hotel["checkin"] == ["2026-11-20"]
    assert registry.handoff_link("skyscanner", {}) == "https://www.skyscanner.co.in/"  # captcha on deep links
    assert registry.handoff_link("razorpay", {}) is None
    assert registry.provider_named("Blinkit ya Zepto").id in {"blinkit", "zepto"}


# ------------------------------------------------------------------ secrets


@pytest.fixture
def enc_key(monkeypatch):
    monkeypatch.setenv("APNA_ENCRYPTION_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_encryption_is_bound_to_owner(enc_key):
    from apna.security import crypto

    ct = crypto.encrypt("token-abc", "user-1:blinkit")
    assert "token-abc" not in ct and crypto.decrypt(ct, "user-1:blinkit") == "token-abc"
    with pytest.raises(ValueError):
        crypto.decrypt(ct, "user-2:blinkit")  # another user's row can't read it
    with pytest.raises(ValueError):
        crypto.decrypt(ct, "user-1:zepto")  # nor another provider


def test_no_key_means_no_secret_storage(monkeypatch):
    from apna.security import crypto

    monkeypatch.delenv("APNA_ENCRYPTION_KEY", raising=False)
    get_settings.cache_clear()
    get_settings.cache_clear()
    with pytest.raises(crypto.EncryptionUnavailable):
        crypto.encrypt("x", "a")
    get_settings.cache_clear()


def test_chat_redaction_masks_payment_and_login_secrets():
    from apna.security.chat_redaction import redact

    for text, kind in [("my card 4111 1111 1111 1111 exp 12/29", "card"), ("cvv 123", "cvv"),
                       ("UPI PIN is 4321", "pin"), ("otp 482913", "otp"), ("OTP hai 4829", "otp"),
                       ("password: hunter2", "password")]:
        clean, found = redact(text)
        assert kind in found, text
        assert not any(sec in clean for sec in ("4111 1111", "123", "4321", "482913", "4829", "hunter2")), clean
    for ok in ("2 litre milk aur 12 eggs", "10:45 wala", "book 2 tickets row A", "1234567890123"):
        assert redact(ok) == (ok, []), ok


async def test_otp_page_refuses_payment_purposes(enc_key):
    from apna.core.errors import PolicyViolation
    from apna.security import otp

    with pytest.raises(PolicyViolation):
        await otp.create_challenge(SimpleNamespace(), user_id=None, provider="x", purpose="authorise UPI payment",
                                   session_ref="s")


def test_logs_redact_secret_fields():
    from apna.core.logging import _redact

    out = _redact(None, "", {"event": "x", "otp": "123456", "token": "abc", "order": "ok"})
    assert out["otp"] != "123456" and out["token"] != "abc" and out["order"] == "ok"


# ------------------------------------------------------------------ payments


def test_razorpay_webhook_signature_and_status_mapping():
    body = json.dumps({"event": "payment_link.paid"}).encode()
    sig = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
    assert payments.verify_razorpay_signature(body, sig, "whsec")
    assert not payments.verify_razorpay_signature(body + b" ", sig, "whsec")  # tampered body
    assert not payments.verify_razorpay_signature(body, None, "whsec")
    assert payments.status_from_link({"status": "paid"}) == "PAID"
    assert payments.status_from_link({"status": "expired"}) == "EXPIRED"
    assert payments.status_from_link({"status": "created"}) == "PENDING"


def test_terminal_payment_status_never_changes():
    intent = SimpleNamespace(status="PAID", gateway_status={}, last_checked_at=None)
    assert payments.apply_status(intent, "FAILED", {}) is False and intent.status == "PAID"
    pending = SimpleNamespace(status="PENDING", gateway_status={}, last_checked_at=None)
    assert payments.apply_status(pending, "PAID", {"x": 1}) and pending.status == "PAID"


async def test_razorpay_gateway_creates_exact_amount_link():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "plink_1", "short_url": "https://rzp.io/i/abc", "status": "created"})

    s = Settings(_env_file=None, razorpay_key_id="rzp_test_k", razorpay_key_secret="sec", razorpay_webhook_secret="w")
    gw = payments.RazorpayGateway(s, transport=httpx.MockTransport(handler))
    link = await gw.create_link(amount_minor=21500, currency="INR", description="Zepto (TEST sandbox): 3 items",
                                reference_id="a" * 50, expire_by=datetime.now(UTC) + timedelta(minutes=20), notes={})
    assert link["short_url"].startswith("https://rzp.io")
    assert seen["body"]["amount"] == 21500 and seen["body"]["accept_partial"] is False
    assert len(seen["body"]["reference_id"]) == 40 and seen["auth"].startswith("Basic ")
    with pytest.raises(Exception):  # noqa: B017 - no keys -> unavailable, never a fake link
        payments.RazorpayGateway(Settings(_env_file=None))


def test_handoff_links_accept_destination_or_query():
    hotel = parse_qs(urlparse(registry.handoff_link("booking_com", {"query": "Goa"})).query)
    assert hotel["ss"] == ["Goa"]
    uber = parse_qs(urlparse(registry.handoff_link("uber", {"pickup": "Kothrud", "destination": "Pune airport"})).query)
    assert uber["pickup[formatted_address]"] == ["Kothrud"] and uber["dropoff[formatted_address]"] == ["Pune airport"]
