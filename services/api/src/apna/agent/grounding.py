"""Ground a planner's DraftPlan into an executable, validated TaskPlan.

- Resolves verbatim date/time phrases deterministically (apna.agent.language).
- Replaces steps that cannot be grounded with `ask_user` steps (never guesses).
- Validates every argument against the tool's Pydantic model.
- Derives risk, approval checkpoints and integrations from the registry, not from the planner.
- Rejects unknown tools, unknown dependencies, cycles and oversized plans.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import ValidationError

from apna.agent.language import Ambiguity, LanguageResult, resolve_date, resolve_datetime
from apna.agent.schemas import DraftPlan, DraftStep, PlanStep, TaskPlan
from apna.agent.tools import BROWSER_SITE_KEYS, BROWSER_TOOLS, get_tool, has_refs
from apna.core.errors import PolicyViolation
from apna.providers.travel import normalise_city

MAX_STEPS = 8


def _ask(step: DraftStep, amb: Ambiguity | None, question_key: str, clause: str,
         partial: dict[str, Any]) -> DraftStep:
    return DraftStep(
        key=step.key, tool="ask_user", description=f"Clarify: {step.description}"[:300],
        args={"question_key": amb.question_key if amb else question_key, "clause": clause[:500],
              "options": amb.options if amb else [], "partial": partial},
        depends_on=[],
    )


def _ground_step(step: DraftStep, *, tz: str, now: datetime, browser_enabled: bool = True) -> DraftStep:
    a = dict(step.args)
    if step.tool in BROWSER_TOOLS and (not browser_enabled or a.get("site_key") not in BROWSER_SITE_KEYS):
        # Never browse a site that isn't allow-listed (planners invent site keys like "bookmyshow"),
        # and never queue browser work when no browser worker runs: say it's not supported instead.
        return _ask(step, None, "unsupported_request", step.description, {})
    if step.tool == "create_reminder":
        when = str(a.pop("when_text", "") or a.get("text", ""))
        res = resolve_datetime(when, tz=tz, now=now, future_context=True)
        if not res.ok:
            return _ask(step, res.ambiguities[0], "time_needed", when,
                        {"tool": "create_reminder", "text": a.get("text", "")})
        assert res.local is not None
        return step.model_copy(update={"args": {"text": a.get("text") or when, "fire_at_local": res.local.isoformat(),
                                                "timezone": tz}})
    if step.tool == "search_trains":
        date_text = str(a.pop("date_text", "") or "")
        origin, dest = normalise_city(a.get("origin")), normalise_city(a.get("destination"))
        partial = {"tool": "search_trains", "origin": origin, "destination": dest}
        if dest is None:
            return _ask(step, None, "train_destination_needed", date_text, partial)
        if origin is None:
            return _ask(step, None, "train_origin_needed", date_text, partial)
        dres = resolve_date(date_text, now.astimezone(_zone(tz)).date(), future_context=True)
        if dres.value is None:
            return _ask(step, dres.ambiguity, "train_date_needed", date_text, partial)
        return step.model_copy(update={"args": {
            "origin": origin, "destination": dest, "travel_date": dres.value.isoformat(),
            "travel_class": a.get("travel_class"), "time_preference": a.get("time_preference")}})
    if step.tool == "get_weather":
        if not a.get("place"):
            return _ask(step, None, "weather_place_needed", "", {"tool": "get_weather"})
        if a.get("when_text"):
            dres = resolve_date(str(a["when_text"]), now.astimezone(_zone(tz)).date(), future_context=True)
            if dres.value is not None:
                a["day"] = dres.value.isoformat()
        return step.model_copy(update={"args": a})
    if step.tool in ("search_offers", "watch_price"):
        return _ground_offers(step, a, tz=tz, now=now)
    if step.tool == "calendar_list":
        if a.get("when_text"):
            dres = resolve_date(str(a["when_text"]), now.astimezone(_zone(tz)).date(), future_context=True)
            if dres.value is not None:
                a["day"] = dres.value.isoformat()
        a.setdefault("days", 1)
        return step.model_copy(update={"args": a})
    if step.tool == "calendar_create_event":
        when = str(a.pop("when_text", "") or "")
        res = resolve_datetime(when, tz=tz, now=now, future_context=True)
        if not res.ok:
            return _ask(step, res.ambiguities[0] if res.ambiguities else None, "time_needed", when,
                        {"tool": "calendar_create_event", "text": a.get("title", "")})
        assert res.local is not None
        return step.model_copy(update={"args": {"title": a.get("title") or "Event", "start_local": res.local.isoformat(),
                                                "timezone": tz, "duration_min": int(a.get("duration_min") or 60)}})
    if step.tool == "order_shopping_list":
        if not (a.get("city") or a.get("area")):
            return _ask(step, None, "delivery_area_needed", "", {"tool": "order_shopping_list"})
        return step
    if step.tool == "order_groceries":
        if not (a.get("city") or a.get("area")):
            return _ask(step, None, "delivery_area_needed", "", {"tool": "order_groceries", "items": a.get("items"),
                                                                  "store": a.get("store")})
        return step
    if step.tool == "book_movie_tickets":
        partial = {"tool": "book_movie_tickets", **{k: a.get(k) for k in (
            "movie", "theatre", "when_text", "show_time", "tickets", "rows", "seat_class")}}
        if not a.get("city"):
            return _ask(step, None, "movie_city_needed", str(a.get("movie") or ""), partial)
        if not a.get("tickets"):
            return _ask(step, None, "tickets_needed", str(a.get("movie") or ""), {**partial, "city": a.get("city")})
        today = now.astimezone(_zone(tz)).date()
        dres = resolve_date(str(a.get("when_text") or ""), today, future_context=True)
        a["show_date"] = (dres.value or today).isoformat()
        a["rows"] = [str(r).strip().upper()[:2] for r in (a.get("rows") or []) if str(r).strip()]
        return step.model_copy(update={"args": a})
    if step.tool == "find_movie_tickets":
        partial = {"tool": "find_movie_tickets", "movie": a.get("movie"), "when_text": a.get("when_text"),
                   "tickets": a.get("tickets")}
        if not a.get("city"):
            return _ask(step, None, "movie_city_needed", str(a.get("movie") or ""), partial)
        today = now.astimezone(_zone(tz)).date()
        dres = resolve_date(str(a.get("when_text") or ""), today, future_context=True)
        a["show_date"] = (dres.value or today).isoformat()
        return step.model_copy(update={"args": a})
    if step.tool == "browser_submit_form" and not a.get("fields"):
        a["fields"] = {}
        return step.model_copy(update={"args": a})
    return step


# What each comparison needs before it can be priced (asked, never guessed).
_OFFER_NEEDS = {"flight": (("origin", "destination"), "where from and to"),
                "bus": (("origin", "destination"), "where from and to"),
                "ride": (("origin|area", "destination"), "pickup and drop"),
                "hotel": (("city|destination",), "which city"), "product": (("query",), "which product"),
                "food": (("query",), "which dish")}


def _ground_offers(step: DraftStep, a: dict[str, Any], *, tz: str, now: datetime) -> DraftStep:
    from apna.agent.tools import SearchOffersArgs, WatchPriceArgs
    from apna.providers.offers import VERTICALS

    model = WatchPriceArgs if step.tool == "watch_price" else SearchOffersArgs
    # Planner output is untrusted: keep only known fields (an extra "date" key must not fail the whole plan).
    a = {k: v for k, v in a.items() if k in model.model_fields and v not in (None, "", [])}
    if a.get("vertical") not in VERTICALS:
        a["vertical"] = "product"
    today = now.astimezone(_zone(tz)).date()
    need, what = _OFFER_NEEDS.get(str(a["vertical"]), ((), ""))
    missing = [f for f in need if not any(a.get(x) for x in f.split("|"))]
    if missing:
        return _ask(step, None, "offer_details_needed", what, {"tool": step.tool, **a})
    for src, dst in (("when_text", "day"), ("checkin_text", "checkin"), ("checkout_text", "checkout")):
        if a.get(src):
            dres = resolve_date(str(a[src]), today, future_context=True)
            if dres.value is not None:
                a[dst] = dres.value.isoformat()
    if a.get("vertical") == "hotel" and a.get("day") and not a.get("checkin"):
        a["checkin"] = a["day"]
    for k in ("qty", "rooms", "budget_max", "target_price", "days"):
        if k in a:
            try:
                a[k] = int(str(a[k]).replace(",", "").replace("₹", "").strip())
            except ValueError:
                a.pop(k)
    if isinstance(a.get("sources"), str):
        a["sources"] = [a["sources"]]
    return step.model_copy(update={"args": a})


_PAYMENT_FOR = {"book_offer": ("pay_offer", "booking_id", "Pay for the chosen offer"),
                "order_shopping_list": ("pay_grocery_order", "order_id", "Pay for the shopping-list order"),"book_movie_tickets": ("pay_movie_booking", "hold_id", "Pay for the movie tickets (TEST)"),
                "order_groceries": ("pay_grocery_order", "order_id", "Pay for the grocery order"),
                "update_grocery_cart": ("pay_grocery_order", "order_id", "Pay for the updated grocery order")}


def _ensure_payment_step(steps: list[DraftStep]) -> list[DraftStep]:
    """A hold or cart is useless without its payment step; add it if the planner forgot."""
    out = list(steps)
    for s in steps:
        if s.tool not in _PAYMENT_FOR:
            continue
        pay_tool, ref, desc = _PAYMENT_FOR[s.tool]
        if not any(p.tool == pay_tool and s.key in p.depends_on for p in steps):
            used = {x.key for x in out}
            key = next(f"s{i}" for i in range(1, 99) if f"s{i}" not in used)
            out.append(DraftStep(key=key, tool=pay_tool, description=desc,
                                 args={ref: f"{{{{{s.key}.{ref}}}}}"}, depends_on=[s.key]))
    return out


def _dedupe_unsupported(steps: list[DraftStep]) -> list[DraftStep]:
    """One 'not supported' answer per request, not one per invented step; drop now-dangling deps."""
    out: list[DraftStep] = []
    seen = False
    for s in steps:
        if s.tool == "ask_user" and s.args.get("question_key") == "unsupported_request":
            if seen:
                continue
            seen = True
        out.append(s)
    keys = {s.key for s in out}
    return [s.model_copy(update={"depends_on": [d for d in s.depends_on if d in keys]}) for s in out]


def _zone(tz: str):  # small helper to keep imports local
    from zoneinfo import ZoneInfo

    return ZoneInfo(tz)


def ground_plan(draft: DraftPlan, *, planner: str, language: LanguageResult, tz: str, now: datetime,
                browser_enabled: bool = True) -> TaskPlan:
    if not draft.steps:
        raise PolicyViolation("Plan has no steps.")
    if len(draft.steps) > MAX_STEPS:
        raise PolicyViolation("Plan exceeds the step limit.")
    keys = [s.key for s in draft.steps]
    if len(set(keys)) != len(keys):
        raise PolicyViolation("Duplicate step keys.")

    grounded = [_ground_step(s, tz=tz, now=now, browser_enabled=browser_enabled) for s in draft.steps]
    grounded = _dedupe_unsupported(_ensure_payment_step(grounded))
    known = set(keys)
    steps: list[PlanStep] = []
    for s in grounded:
        spec = get_tool(s.tool)  # raises on unknown tool
        bad = [d for d in s.depends_on if d not in known or d == s.key]
        if bad:
            raise PolicyViolation(f"Step {s.key} has invalid dependencies {bad}.")
        if not has_refs(s.args):
            try:
                spec.args_model.model_validate(s.args)
            except ValidationError as exc:
                raise PolicyViolation(f"Step {s.key} ({s.tool}) has invalid arguments: {exc.errors()[:3]}") from exc
        steps.append(PlanStep(key=s.key, tool=s.tool, description=s.description, args=s.args,
                              depends_on=s.depends_on, risk_level=int(spec.risk),
                              requires_approval=spec.requires_approval, integration=spec.integration))
    _assert_acyclic(steps)

    return TaskPlan(
        goal=draft.goal, language=language.code, planner=planner, constraints=draft.constraints,
        entities=draft.entities, steps=steps,
        required_integrations=sorted({s.integration for s in steps}),
        permission_level=max(s.risk_level for s in steps),
        approval_checkpoints=[s.key for s in steps if s.requires_approval],
        estimated_cost_inr=0.0, timeout_seconds=60 * 60 * 24,
        verification_criteria=[f"{s.key}: provider-verified outcome for {s.tool}" for s in steps],
        recovery_policy={"max_attempts": 3, "backoff": "exponential", "on_failure": "report_partial"},
    )


def _assert_acyclic(steps: list[PlanStep]) -> None:
    graph = {s.key: set(s.depends_on) for s in steps}
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(k: str) -> None:
        if k in done:
            return
        if k in visiting:
            raise PolicyViolation("Plan dependencies contain a cycle.")
        visiting.add(k)
        for d in graph[k]:
            visit(d)
        visiting.discard(k)
        done.add(k)

    for k in graph:
        visit(k)
