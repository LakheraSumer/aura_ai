"""FastAPI application entry point:  uvicorn apna.api.main:app --port 8800"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from temporalio.client import Client

from apna import __version__
from apna.api import oauth, routes, secure, webhooks
from apna.core.config import get_settings
from apna.core.errors import ApnaError
from apna.core.logging import configure_logging
from apna.db.session import dispose_engine, session_scope
from apna.services.conversation import ConversationService


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    s = get_settings()
    client = await Client.connect(s.temporal_address, namespace=s.temporal_namespace)
    app.state.temporal = client
    app.state.conversations = ConversationService(client)
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    s = get_settings()
    hidden = {"openapi_url": None, "docs_url": None, "redoc_url": None} if s.webhook_only else {}
    app = FastAPI(title=f"{s.product_name} API", version=__version__, lifespan=lifespan,
                  description=s.product_tagline, **hidden)

    @app.exception_handler(ApnaError)
    async def _apna_error(_: Request, exc: ApnaError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status,
                            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request) -> dict[str, str]:
        async with session_scope() as session:
            await session.execute(text("select 1"))
        await request.app.state.temporal.service_client.check_health()
        choice = s.planner_choice()
        label = {"anthropic": f"claude ({s.planner_model})", "openai": f"openai ({s.openai_planner_model})",
                 "gemini": f"gemini ({s.gemini_planner_model})"}
        return {"status": "ready", "planner": label.get(choice, "rule-planner (mock)")}

    if not s.webhook_only:
        app.include_router(routes.router)
    # webhook_only: the instance exposed to the internet (tunnel / ingress) serves only signed webhooks
    # and /healthz, so a public URL never exposes sign-up, chat or task endpoints.
    app.include_router(webhooks.router)
    app.include_router(secure.router)  # one-time secure code page (public, like webhooks)
    app.include_router(oauth.router)  # Google sign-in landing page (public; one-time state)
    return app


app = create_app()
