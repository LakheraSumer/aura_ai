"""Tool executors. Run inside Temporal activities only.

Each executor returns an `ExecResult` describing the *verified* outcome. Executors that cause
side effects call `policy.assert_authorized` immediately before acting.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apna.agent import policy
from apna.agent.tools import (
    CreateReminderArgs,
    DraftEmailArgs,
    FindEmailArgs,
    SearchTrainsArgs,
    SendEmailArgs,
    ToolSpec,
)
from apna.core.config import Settings
from apna.db.models import Reminder, UserProfile
from apna.domain.enums import StepStatus
from apna.providers.email import EmailProvider, SandboxEmailProvider, content_hash
from apna.providers.travel import OfficialLinkRailProvider, RailProvider

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


@dataclass
class ExecResult:
    status: StepStatus
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class ToolContext:
    session: AsyncSession
    settings: Settings
    user_id: uuid.UUID
    task_id: uuid.UUID
    step_id: uuid.UUID
    step_key: str
    idempotency_key: str
    source_text: str
    profile: UserProfile | None
    spec: ToolSpec
    email: EmailProvider
    rail: RailProvider
    start_reminder_workflow: Callable[[str, datetime], Awaitable[str]] | None = None
    start_payment_workflow: Callable[[str], Awaitable[str]] | None = None
    start_price_watch_workflow: Callable[[str], Awaitable[str]] | None = None


def default_providers() -> tuple[EmailProvider, RailProvider]:
    return SandboxEmailProvider(), OfficialLinkRailProvider()


# --------------------------------------------------------------------------- preparation


async def prepare_send_email(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Expand a send_email step with the draft's exact content so the approval binds to it."""
    a = SendEmailArgs.model_validate(args)
    draft = await ctx.email.get_draft(ctx.session, ctx.user_id, a.draft_id)
    if draft is None:
        raise ValueError("Draft not found.")
    return {"draft_id": a.draft_id, "to_address": draft.to_address, "subject": draft.subject, "body": draft.body}


# --------------------------------------------------------------------------- executors


# Notes that need no answer: the task finishes instead of waiting, so the user's next message is a
# fresh request and is never merged into (or re-triggers) this one.
INFORMATIONAL_QUESTIONS = frozenset({"unsupported_request", "unknown_intent"})


async def exec_ask_user(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    if args.get("question_key") in INFORMATIONAL_QUESTIONS:
        return ExecResult(StepStatus.COMPLETED_LOCALLY, dict(args))
    return ExecResult(StepStatus.AWAITING_USER_ACTION, dict(args))


async def exec_create_reminder(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    a = CreateReminderArgs.model_validate(args)
    await policy.assert_authorized(ctx.session, step_id=ctx.step_id, spec=ctx.spec, args=args, profile=ctx.profile)
    fire_utc = a.fire_at_local.astimezone(UTC)
    if fire_utc <= datetime.now(UTC):
        return ExecResult(StepStatus.AWAITING_USER_ACTION, {"question_key": "time_in_past"})
    local = a.fire_at_local.astimezone(ZoneInfo(a.timezone))
    display = local.strftime("%a %d %b %Y, %I:%M %p") + f" ({a.timezone})"

    existing = (await ctx.session.execute(
        select(Reminder).where(Reminder.idempotency_key == ctx.idempotency_key)
    )).scalar_one_or_none()
    reminder = existing or Reminder(
        user_id=ctx.user_id, task_id=ctx.task_id, text=a.text, fire_at_utc=fire_utc, timezone=a.timezone,
        local_display=display, idempotency_key=ctx.idempotency_key, status="SCHEDULED",
    )
    if existing is None:
        ctx.session.add(reminder)
        await ctx.session.flush()
    reminder.workflow_id = f"reminder-{reminder.id}"
    await ctx.session.commit()  # the row must exist before the timer workflow can fire

    if ctx.start_reminder_workflow is None:
        return ExecResult(StepStatus.FAILED, error="Scheduler unavailable.")
    run_status = await ctx.start_reminder_workflow(str(reminder.id), fire_utc)
    if run_status != "RUNNING":
        return ExecResult(StepStatus.FAILED, error=f"Scheduler reported {run_status}.")
    return ExecResult(StepStatus.SCHEDULED, {
        "reminder_id": str(reminder.id), "text": a.text, "fire_at_utc": fire_utc.isoformat(),
        "local_display": display, "timezone": a.timezone, "workflow_id": reminder.workflow_id,
        "verification": {"scheduler": "temporal", "workflow_status": run_status},
    })


async def exec_find_email(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    a = FindEmailArgs.model_validate(args)
    threads = await ctx.email.search_threads(ctx.session, ctx.user_id, a.query)
    if not threads:
        return ExecResult(StepStatus.FAILED, error=f"No email matching '{a.query}' in the connected mailbox.")
    th = threads[0]
    return ExecResult(StepStatus.COMPLETED_LOCALLY, {
        "thread_ref": th.thread_ref, "from_address": th.from_address, "from_name": th.from_name,
        "subject": th.subject, "snippet": th.snippet, "provider": ctx.email.name,
        "environment": ctx.email.environment,
    })


async def exec_draft_email(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    a = DraftEmailArgs.model_validate(args)
    await policy.assert_authorized(ctx.session, step_id=ctx.step_id, spec=ctx.spec, args=args, profile=ctx.profile)

    # Recipient rule: only an address the user typed, or one resolved from verified contacts/threads.
    to: str | None = None
    typed = {m.lower() for m in _EMAIL_RE.findall(ctx.source_text)}
    for candidate in (a.to_address, a.to_hint):
        if candidate and str(candidate).lower() in typed:
            to = str(candidate).lower()
            break
    if to is None:
        to = await ctx.email.resolve_contact(ctx.user_id, a.to_hint)
    if to is None:
        return ExecResult(StepStatus.AWAITING_USER_ACTION, {"question_key": "recipient_needed",
                                                            "to_hint": a.to_hint})

    subject = a.subject
    if not subject and a.reply_to_thread:
        threads = await ctx.email.search_threads(ctx.session, ctx.user_id, "")
        orig = next((t for t in threads if t.thread_ref == a.reply_to_thread), None)
        subject = f"Re: {orig.subject}" if orig else "Re:"
    draft = await ctx.email.create_draft(ctx.session, ctx.user_id, ctx.task_id, to, subject or "(no subject)",
                                         a.body, a.reply_to_thread)
    # Verify by reading the draft back from the provider.
    check = await ctx.email.get_draft(ctx.session, ctx.user_id, draft.draft_id)
    if check is None or check.content_hash != draft.content_hash:
        return ExecResult(StepStatus.FAILED, error="Draft could not be verified after creation.")
    return ExecResult(StepStatus.COMPLETED_LOCALLY, {
        "draft_id": draft.draft_id, "to_address": draft.to_address, "subject": draft.subject, "body": draft.body,
        "provider": draft.provider, "environment": ctx.email.environment,
        "verification": {"read_back": True, "content_hash": draft.content_hash},
    })


async def exec_send_email(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    a = SendEmailArgs.model_validate(args)
    draft = await ctx.email.get_draft(ctx.session, ctx.user_id, a.draft_id)
    if draft is None:
        return ExecResult(StepStatus.FAILED, error="Draft no longer exists.")
    # The approval was bound to the content snapshot in `args`; confirm the draft still matches it.
    if (draft.to_address, draft.subject, draft.body) != (a.to_address, a.subject, a.body):
        return ExecResult(StepStatus.AWAITING_USER_ACTION,
                          {"question_key": "draft_changed", "reason": "Draft changed after approval."})
    await policy.assert_authorized(ctx.session, step_id=ctx.step_id, spec=ctx.spec, args=args, profile=ctx.profile)
    receipt = await ctx.email.send_draft(ctx.session, ctx.user_id, a.draft_id, ctx.idempotency_key)
    if not receipt.accepted:
        return ExecResult(StepStatus.FAILED, error="Provider rejected the message.")
    # A real provider's acceptance is "submitted"; delivery confirmation arrives later via webhook.
    status = StepStatus.CONFIRMED_BY_PROVIDER if receipt.environment == "mock" else StepStatus.SUBMITTED_EXTERNALLY
    return ExecResult(status, {
        "to_address": draft.to_address, "provider": receipt.provider,
        "provider_message_id": receipt.provider_message_id, "environment": receipt.environment,
        "content_hash": content_hash(draft.to_address, draft.subject, draft.body),
    })


async def exec_search_trains(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    a = SearchTrainsArgs.model_validate(args)
    r = await ctx.rail.search(a.origin, a.destination, a.travel_date, a.travel_class, a.time_preference)
    return ExecResult(StepStatus.COMPLETED_LOCALLY, {
        "origin": a.origin, "destination": a.destination, "travel_date": r.travel_date,
        "travel_class": r.travel_class, "time_preference": r.time_preference,
        "origin_stations": r.origin_stations, "destination_stations": r.destination_stations,
        "official_link": r.official_link, "data_available": r.data_available, "trains": r.trains,
        "provider": r.provider, "environment": r.environment, "notice": r.notice,
    })


async def exec_find_movie_tickets(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    from datetime import date as _date

    from apna.agent.tools import FindMovieArgs
    from apna.providers.movies import bms_city_slug, city_page, find_movie, show_page

    a = FindMovieArgs.model_validate(args)
    movie = await find_movie(ctx.session, a.movie)
    city_slug = bms_city_slug(a.city)
    day = _date.fromisoformat(a.show_date) if a.show_date else datetime.now(UTC).date()
    if movie is not None and city_slug:
        url, exact = show_page(city_slug, movie.slug, movie.et_code, day), True
    else:
        url, exact = city_page(a.city)[0], False
    return ExecResult(StepStatus.COMPLETED_LOCALLY, {
        "movie": movie.title if movie else a.movie, "movie_known": movie is not None, "city": a.city,
        "show_date": day.isoformat(), "tickets": a.tickets, "official_link": url, "exact": exact,
        "provider": "bookmyshow-link", "data_available": False,
    })


from apna.agent.commerce import (  # noqa: E402
    exec_cancel_grocery_order,
    exec_get_order_status,
    exec_get_weather,
    exec_order_groceries,
    exec_pay_grocery_order,
    exec_provider_handoff,
    exec_update_grocery_cart,
)

CINEMA_QUESTIONS = frozenset({"theatre_choice", "show_choice", "show_full", "seats_unavailable", "seats_taken"})


def _cinema_request(a: Any) -> str:
    """The user's request in words, so a follow-up answer can be merged and re-planned."""
    if isinstance(a, dict):
        from types import SimpleNamespace

        a = SimpleNamespace(**{k: a.get(k) for k in (
            "tickets", "movie", "city", "when_text", "theatre", "show_time", "rows", "seat_class")})
    bits = [f"book {a.tickets} tickets for {a.movie} in {a.city}"]
    if a.when_text:
        bits.append(a.when_text)
    if a.theatre:
        bits.append(f"at {a.theatre}")
    if a.show_time:
        bits.append(f"{a.show_time} show")
    if a.rows:
        bits.append(f"row {'/'.join(a.rows)}")
    if a.seat_class:
        bits.append(a.seat_class)
    return " ".join(bits)


_ORDINALS = {"first": 1, "1st": 1, "pehla": 1, "pahla": 1, "second": 2, "2nd": 2, "dusra": 2, "doosra": 2,
             "third": 3, "3rd": 3, "teesra": 3, "tisra": 3, "last": -1}


def resolve_cinema_choice(key: str, args: dict[str, Any], choices: list[str], answer: str) -> str | None:
    """Turn a reply to a cinema question ("2", "kothrud", "10:45 wala", "row B") into a clean request.

    Done in code, not by the model: a long "options offered..." prompt confused the free-tier model in a
    live run. Returns None when the reply can't be matched, so the caller falls back to re-planning.
    """
    import difflib

    from apna.providers import cinema

    ans = answer.strip().lower()
    chosen: str | None = None
    if m := re.fullmatch(r"(?:option\s*|no\.?\s*|#)?(\d{1,2})(?:st|nd|rd|th)?(?:\s+(?:wala|vala|one|option))?", ans):
        i = int(m.group(1))
        chosen = choices[i - 1] if 1 <= i <= len(choices) else None
    elif (w := next((v for k, v in _ORDINALS.items() if re.search(rf"\b{k}\b", ans)), None)) and choices:
        chosen = choices[w - 1] if w > 0 and w <= len(choices) else choices[-1]
    new = dict(args)
    if key == "theatre_choice":
        names = [c.split(" (")[0] for c in choices]
        th = (cinema.match_theatre(chosen.split(" (")[0], cinema.theatres_for(args.get("city"))) if chosen else
              cinema.match_theatre(answer, [t for t in cinema.theatres_for(args.get("city")) if t.name in names]))
        if th is None:
            return None
        new["theatre"] = th.name
        if t_ := re.search(r"\d{1,2}(?::\d{2})?\s*(?:am|pm)?", ans) if not chosen else None:
            new["show_time"] = t_.group(0)
    elif key in ("show_choice", "show_full"):
        said = re.search(r"\b\d{1,2}:\d{2}\b", ans)
        src = chosen or next((c for c in choices if said and re.search(
            rf"(?<!\d){re.escape(said.group(0))} [AP]M", c)), None)
        src = src or next((c for c in choices if ans and ans in c.lower()), None)
        if src is None:
            close = difflib.get_close_matches(ans, [c.lower() for c in choices], n=1, cutoff=0.5)
            src = next((c for c in choices if close and c.lower() == close[0]), None)
        if src is None and (m := re.search(r"\d{1,2}(?::\d{2})?\s*(?:am|pm)?", ans)):
            new["show_time"] = m.group(0).strip()  # a time with no theatre: same theatre, that show
            return _cinema_request(new)
        if src is None:
            return None
        if tm := re.search(r"\d{1,2}:\d{2} [AP]M", src):
            new["show_time"] = tm.group(0).lower()
        if " - " in src and src.split(" - ")[0].count(":") >= 1 and not re.match(r"\d", src):
            new["theatre"] = src.split(" - ")[0]
    elif key == "seats_unavailable":
        row = None
        if chosen and (m := re.match(r"Row ([A-Z]{1,2}):", chosen)):
            row = m.group(1)
        elif m := re.search(r"\brow\s*([a-z]{1,2})\b|^([a-z])$", ans):
            row = (m.group(1) or m.group(2)).upper()
        if row is None:
            return None
        new["rows"] = [row]
    else:
        return None
    return _cinema_request(new)


async def exec_book_movie_tickets(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    from datetime import date as _date

    from apna.agent.tools import BookMovieArgs
    from apna.providers import cinema
    from apna.providers.movies import bms_city_slug, city_page, find_movie, show_page

    a = BookMovieArgs.model_validate(args)
    request = _cinema_request(a)
    day = _date.fromisoformat(a.show_date) if a.show_date else datetime.now(cinema.IST).date()
    movie = await find_movie(ctx.session, a.movie)
    city_slug = bms_city_slug(a.city)
    bms = (show_page(city_slug, movie.slug, movie.et_code, day) if movie and city_slug else city_page(a.city)[0])
    base = {"request": request, "movie": movie.title if movie else a.movie, "bms_link": bms,
            "show_date": day.isoformat(), "test": True}
    if movie is None:
        return ExecResult(StepStatus.COMPLETED_LOCALLY, {**base, "kind": "movie_not_listed"})

    theatres = cinema.theatres_for(a.city)
    now = datetime.now(cinema.IST)

    def upcoming(th: cinema.Theatre) -> list[str]:
        return [t for t in th.times if cinema.show_start(day, t) > now]

    async def free_count(th: cinema.Theatre, hhmm: str) -> int:
        sid = cinema.show_id(th, day, hhmm)
        taken = cinema.presold(th, sid, hhmm) | await cinema.taken_seats(ctx.session, sid)
        return sum(len(v) for v in cinema.free_by_row(th, taken).values())

    async def show_options(ths: list[cinema.Theatre], skip: tuple[str, str] | None = None) -> list[str]:
        opts = []
        for th in ths:
            for hhmm in upcoming(th):
                if skip == (th.id, hhmm):
                    continue
                n = await free_count(th, hhmm)
                if n >= a.tickets:
                    opts.append(f"{th.name} - {cinema.fmt_time(hhmm)} ({n} seats free)")
        return opts

    def ask(key: str, choices: list[str], **extra: Any) -> ExecResult:
        return ExecResult(StepStatus.AWAITING_USER_ACTION,
                          {**base, "question_key": key, "clause": request, "choices": choices[:6],
                           "args": a.model_dump(exclude={"show_date"}), **extra})

    th = cinema.match_theatre(a.theatre, theatres) if a.theatre else None
    if th is None:
        opts = []
        for t in theatres:
            times = [cinema.fmt_time(x) for x in upcoming(t)]
            if times:
                opts.append(f"{t.name} ({t.area}) - {', '.join(times)}")
        return ask("theatre_choice", opts, theatre_hint=a.theatre)

    times = upcoming(th)
    if not times:
        return ask("show_full", await show_options([t for t in theatres if t.id != th.id]), theatre=th.name,
                   reason="no_more_shows")
    hhmm = cinema.match_show(a.show_time, times) if a.show_time else (times[0] if len(times) == 1 else None)
    if hhmm is None:
        opts = []
        for x in times:
            n = await free_count(th, x)
            opts.append(f"{cinema.fmt_time(x)} - " + (f"{n} seats free" if n >= a.tickets else "HOUSEFULL"))
        return ask("show_choice", opts, theatre=th.name)

    sid = cinema.show_id(th, day, hhmm)
    taken = cinema.presold(th, sid, hhmm) | await cinema.taken_seats(ctx.session, sid)
    pick, alts = cinema.choose_seats(th, taken, a.tickets, a.rows, a.seat_class)
    show_label = f"{th.name}, {cinema.fmt_time(hhmm)}"
    if pick is None and alts:
        return ask("seats_unavailable", [
            f"Row {p.row}: {cinema.fmt_seats(p.row, p.seats)} ({p.cls.name}, {cinema.rupees(p.cls.price_minor)} each)"
            for p in alts], theatre=th.name, show=show_label, rows=a.rows)
    if pick is None:
        others = await show_options([th], skip=(th.id, hhmm))
        others += await show_options([t for t in theatres if t.id != th.id])
        return ask("show_full", others, theatre=th.name, show=show_label, reason="housefull")

    starts = cinema.show_start(day, hhmm)
    hold = await cinema.create_hold(ctx.session, user_id=ctx.user_id, idempotency_key=ctx.idempotency_key,
                                    th=th, sid=sid, starts_at=starts, movie=base["movie"], pick=pick)
    return ExecResult(StepStatus.COMPLETED_LOCALLY, {
        **base, "kind": "held", "hold_id": str(hold.id), "theatre": th.name, "area": th.area,
        "starts_at": starts.isoformat(), "seats": hold.seats, "seat_class": hold.seat_class,
        "unit_price_minor": pick.cls.price_minor, "amount_minor": hold.amount_minor,
        "hold_minutes": cinema.HOLD_MINUTES, "requested_rows": a.rows,
    })


async def prepare_movie_payment(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Expand the payment step with the hold's exact purchase so the approval binds to it."""
    import uuid as _uuid

    from apna.agent.tools import PayMovieArgs
    from apna.db.models import SandboxCinemaBooking
    from apna.providers import cinema

    a = PayMovieArgs.model_validate(args)
    hold = await ctx.session.get(SandboxCinemaBooking, _uuid.UUID(a.hold_id))
    if hold is None or hold.user_id != ctx.user_id:
        raise ValueError("Seat hold not found.")
    return {"hold_id": a.hold_id, "amount_minor": hold.amount_minor, "currency": "INR",
            "merchant": cinema.MERCHANT, "movie": hold.movie, "theatre": hold.theatre,
            "starts_at": hold.starts_at.isoformat(), "seats": list(hold.seats), "seat_class": hold.seat_class}


async def exec_pay_movie_booking(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    import uuid as _uuid

    from apna.agent.tools import PayMovieArgs
    from apna.db.models import SandboxCinemaBooking
    from apna.providers import cinema

    a = PayMovieArgs.model_validate(args)
    await policy.assert_authorized(ctx.session, step_id=ctx.step_id, spec=ctx.spec, args=args, profile=ctx.profile)
    hold = await ctx.session.get(SandboxCinemaBooking, _uuid.UUID(a.hold_id))
    if hold is None:
        return ExecResult(StepStatus.FAILED, error="Seat hold not found.")
    if (hold.amount_minor, list(hold.seats)) != (a.amount_minor, a.seats):
        # The approval was for a different purchase than the one on file: never charge.
        return ExecResult(StepStatus.FAILED, error="Booking changed after approval; nothing was charged.")
    try:
        booked = await cinema.pay_and_book(ctx.session, hold.id, ctx.user_id)
    except cinema.SeatsTaken as exc:
        return ExecResult(StepStatus.AWAITING_USER_ACTION, {
            "question_key": "seats_taken", "clause": f"book {len(a.seats or [])} tickets for {a.movie}",
            "choices": [], "taken": str(exc), "test": True})
    check = await cinema.get_booking(ctx.session, booked.booking_code)  # independent read-back
    if check is None or check.status != "BOOKED":
        return ExecResult(StepStatus.FAILED, error="Booking could not be verified.")
    return ExecResult(StepStatus.CONFIRMED_BY_PROVIDER, {
        "booking_code": check.booking_code, "payment_ref": check.payment_ref, "amount_minor": check.amount_minor,
        "movie": check.movie, "theatre": check.theatre, "starts_at": check.starts_at.isoformat(),
        "seats": list(check.seats), "seat_class": check.seat_class, "environment": "sandbox", "test": True,
    })


async def exec_answer_question(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    from apna.agent.tools import AnswerQuestionArgs

    a = AnswerQuestionArgs.model_validate(args)
    return ExecResult(StepStatus.COMPLETED_LOCALLY, {"answer": a.answer})


def _assist_executors() -> dict[str, Callable[[ToolContext, dict[str, Any]], Awaitable[ExecResult]]]:
    from apna.agent import assist as x

    return {"search_offers": x.exec_search_offers, "book_offer": x.exec_book_offer, "pay_offer": x.exec_pay_offer,
            "cancel_booking": x.exec_cancel_booking, "watch_price": x.exec_watch_price,
            "manage_price_watch": x.exec_manage_price_watch, "shopping_list": x.exec_shopping_list,
            "order_shopping_list": x.exec_order_shopping_list, "web_search": x.exec_web_search,
            "read_inbox": x.exec_read_inbox, "connect_google": x.exec_connect_google,
            "disconnect_google": x.exec_disconnect_google, "calendar_list": x.exec_calendar_list,
            "calendar_create_event": x.exec_calendar_create_event}


_ASSIST = _assist_executors()

EXECUTORS: dict[str, Callable[[ToolContext, dict[str, Any]], Awaitable[ExecResult]]] = {
    "ask_user": exec_ask_user,
    "create_reminder": exec_create_reminder,
    "find_email": exec_find_email,
    "draft_email": exec_draft_email,
    "send_email": exec_send_email,
    "search_trains": exec_search_trains,
    "find_movie_tickets": exec_find_movie_tickets,
    "answer_question": exec_answer_question,
    "book_movie_tickets": exec_book_movie_tickets,
    "pay_movie_booking": exec_pay_movie_booking,
    "order_groceries": exec_order_groceries,
    "update_grocery_cart": exec_update_grocery_cart,
    "cancel_grocery_order": exec_cancel_grocery_order,
    "pay_grocery_order": exec_pay_grocery_order,
    "get_order_status": exec_get_order_status,
    "get_weather": exec_get_weather,
    "provider_handoff": exec_provider_handoff,
    **_ASSIST,
}

# Tools whose side effect is safe to retry with the same idempotency key.
# Hold creation is keyed by the step's idempotency key; payment is keyed by the hold.
# pay_grocery_order is NOT here: a crash after STARTED may already have created a payment link, so a
# retry must reconcile (the intent is looked up by idempotency key) rather than run blindly. The gate in
# activities turns an interrupted attempt into "outcome uncertain" for that reason.
IDEMPOTENT_SIDE_EFFECTS = {"create_reminder", "draft_email", "send_email", "book_movie_tickets",
                           "pay_movie_booking", "order_groceries", "update_grocery_cart", "cancel_grocery_order",
                           # offers: holds and watches are keyed by the step's idempotency key / title; the
                           # calendar event id is derived from it. pay_offer is excluded like pay_grocery_order.
                           "book_offer", "watch_price", "order_shopping_list", "calendar_create_event",
                           "disconnect_google"}
