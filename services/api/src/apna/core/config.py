"""Typed application settings loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../../.env"), extra="ignore")

    product_name: str = "Apna AI"
    product_tagline: str = "Your personal AI assistant. Just ask, and it gets things done."
    environment: str = Field(default="local", description="local | test | staging | production")
    development_mode: bool = True
    # Public-facing instance: serve only /v1/webhooks/* and /healthz (see api/main.py).
    webhook_only: bool = False

    database_url: str = "postgresql+asyncpg://apna:apna_dev_only@127.0.0.1:55432/apna"
    redis_url: str = "redis://127.0.0.1:56379/0"

    temporal_address: str = "127.0.0.1:57233"
    temporal_namespace: str = "apna"
    temporal_task_queue: str = "apna-tasks"
    temporal_browser_queue: str = "apna-browser"

    jwt_secret: SecretStr = SecretStr("dev-only-change-me-0123456789abcdef0123456789")
    jwt_access_ttl_seconds: int = 900
    jwt_refresh_ttl_seconds: int = 60 * 60 * 24 * 30

    # Claude. Leave unset in local dev to use the deterministic RulePlanner.
    anthropic_api_key: SecretStr | None = None
    planner_model: str = "claude-opus-5-5"
    planner_effort: str = "medium"
    planner_enabled: bool = True
    # auto: the first provider with a key, in the order Claude, OpenAI, Gemini; else the rule planner.
    planner_provider: str = Field(default="auto", description="auto | anthropic | openai | gemini | rule")

    # OpenAI (alternative planner, used for testing).
    openai_api_key: SecretStr | None = None
    openai_planner_model: str = "gpt-5.4-mini"
    openai_reasoning_effort: str | None = "low"

    # Google Gemini via its OpenAI-compatible endpoint (free tier for testing). On the free tier
    # Google may use prompts to improve its products, so use test data only.
    gemini_api_key: SecretStr | None = None
    gemini_planner_model: str = "gemini-3.1-flash-lite"  # 2.5-flash is closed to new users
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    gemini_reasoning_effort: str | None = None  # "low" made flash-lite 4-25x slower

    default_timezone: str = "Asia/Kolkata"

    browser_profile_root: Path = Path(".data/browser-profiles")
    browser_artifact_root: Path = Path(".data/browser-artifacts")
    browser_allowed_domains: list[str] = Field(default_factory=lambda: ["127.0.0.1", "localhost"])
    browser_headless: bool = True
    # False when no browser worker runs (e.g. the base Docker image has no Chromium).
    browser_enabled: bool = True
    browser_step_timeout_ms: int = 15000

    test_site_base_url: str = "http://127.0.0.1:8899"

    approval_ttl_seconds: int = 60 * 60 * 24
    clarification_ttl_seconds: int = 60 * 60 * 24

    # ---- WhatsApp Cloud API (official). Production use for a general-purpose assistant is NOT
    # permitted under Meta's WhatsApp Business Solution terms (effective 2026-01-15); keep
    # `whatsapp_production_enabled` false unless Meta has explicitly approved this product.
    whatsapp_enabled: bool = False
    whatsapp_production_enabled: bool = False
    whatsapp_graph_base_url: str = "https://graph.facebook.com"
    whatsapp_graph_version: str = "v26.0"  # Graph API v26.0 released 2026-07-29
    whatsapp_phone_number_id: str | None = None
    whatsapp_access_token: SecretStr | None = None
    whatsapp_app_secret: SecretStr | None = None
    whatsapp_verify_token: SecretStr | None = None
    whatsapp_allowed_test_numbers: list[str] = Field(default_factory=list)
    whatsapp_reminder_template: str | None = None  # approved UTILITY template for out-of-window sends
    whatsapp_template_language: str = "en"

    # ---- Telegram Bot API (launchable channel; bots/AI assistants are permitted).
    telegram_enabled: bool = False
    telegram_api_base_url: str = "https://api.telegram.org"
    telegram_bot_token: SecretStr | None = None
    telegram_webhook_secret: SecretStr | None = None

    # ---- Secrets at rest (provider tokens, OTPs). 32 random bytes, base64url. No default on purpose.
    apna_encryption_key: SecretStr | None = None  # env APNA_ENCRYPTION_KEY
    # Public base URL of the webhook-only instance (tunnel/ingress), used for secure-entry links.
    public_base_url: str | None = None

    # ---- Payments. Razorpay payment links (UPI opens GPay/PhonePe/Paytm on the phone). Test keys first.
    razorpay_key_id: str | None = None
    razorpay_key_secret: SecretStr | None = None
    razorpay_webhook_secret: SecretStr | None = None
    razorpay_api_base_url: str = "https://api.razorpay.com"
    payment_link_ttl_minutes: int = 20
    payment_poll_seconds: int = 60  # reconciliation poll while a payment is pending

    # ---- Commerce quotes
    quote_ttl_minutes: int = 10
    offer_hold_minutes: int = 15  # how long a TEST booking hold from the offers engine lasts
    price_watch_poll_seconds: int = 6 * 3600  # price-drop checks
    price_watch_days: int = 14
    reminder_followup_seconds: int = 30 * 60  # "did you do it?" nudge after a reminder (0 = off)

    # ---- Web search. Tavily (free 1,000 credits/month) when keyed; otherwise Google News RSS + Wikipedia.
    tavily_api_key: SecretStr | None = None
    tavily_api_base_url: str = "https://api.tavily.com"
    google_news_rss_url: str = "https://news.google.com/rss/search"
    wikipedia_base_url: str = "https://en.wikipedia.org"

    # ---- Google (Gmail + Calendar) via OAuth 2.0 with PKCE. Redirect: {public_base_url}/v1/oauth/google/callback
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    google_auth_url: str = "https://accounts.google.com/o/oauth2/v2/auth"
    google_token_url: str = "https://oauth2.googleapis.com/token"
    gmail_api_base_url: str = "https://gmail.googleapis.com"
    calendar_api_base_url: str = "https://www.googleapis.com/calendar/v3"

    # ---- Weather (Open-Meteo: free for non-commercial use; commercial use needs their subscription)
    weather_base_url: str = "https://api.open-meteo.com"
    geocoding_base_url: str = "https://geocoding-api.open-meteo.com"

    def tavily_configured(self) -> bool:
        return bool(self.tavily_api_key and self.tavily_api_key.get_secret_value())

    def google_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret
                    and self.google_client_secret.get_secret_value() and self.public_base_url)

    def google_redirect_uri(self) -> str:
        return f"{(self.public_base_url or '').rstrip('/')}/v1/oauth/google/callback"

    def razorpay_configured(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret and self.razorpay_key_secret.get_secret_value()
                    and self.razorpay_webhook_secret and self.razorpay_webhook_secret.get_secret_value())

    def claude_configured(self) -> bool:
        return self.planner_enabled and self.anthropic_api_key is not None and bool(
            self.anthropic_api_key.get_secret_value()
        )

    def openai_configured(self) -> bool:
        return self.planner_enabled and self.openai_api_key is not None and bool(
            self.openai_api_key.get_secret_value()
        )

    def gemini_configured(self) -> bool:
        return self.planner_enabled and self.gemini_api_key is not None and bool(
            self.gemini_api_key.get_secret_value()
        )

    def planner_choice(self) -> str:
        """Which planner runs: 'anthropic', 'openai', 'gemini' or 'rule'.

        An explicit provider without a key → 'rule' (never silently switches to another paid provider).
        """
        configured = {"anthropic": self.claude_configured(), "openai": self.openai_configured(),
                      "gemini": self.gemini_configured()}
        p = self.planner_provider.lower()
        if p == "scripted":  # integration tests only
            return "scripted" if self.environment != "production" else "rule"
        if p in configured:
            return p if configured[p] else "rule"
        if p == "rule":
            return "rule"
        return next((name for name, ok in configured.items() if ok), "rule")


@lru_cache
def get_settings() -> Settings:
    return Settings()


def _allow_reserved_test_domain() -> None:
    """Outside production, accept RFC 2606 `.test` addresses (sandbox mailbox, fixtures) - they can
    never be delivered. In production they stay rejected by email-validator."""
    import os

    import email_validator

    if os.environ.get("ENVIRONMENT", "local").lower() != "production" and \
            "test" in email_validator.SPECIAL_USE_DOMAIN_NAMES:
        email_validator.SPECIAL_USE_DOMAIN_NAMES.remove("test")


_allow_reserved_test_domain()
