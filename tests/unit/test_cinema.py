"""Sandbox cinema: seat choice, alternatives, housefull, matching, grounding, replies (no DB)."""

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from apna.agent.grounding import ground_plan
from apna.agent.language import detect_language
from apna.agent.replies import render_task_reply
from apna.agent.schemas import DraftPlan, DraftStep
from apna.providers import cinema

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

KOTHRUD = next(t for t in cinema.theatres_for("Pune") if t.id == "city-pride-kothrud")


def test_requested_row_free_gives_seats_together_in_that_row():
    pick, alts = cinema.choose_seats(KOTHRUD, set(), 2, rows=["A"])
    assert pick and pick.row == "A" and len(pick.seats) == 2 and pick.seats[1] == pick.seats[0] + 1
    assert pick.cls.name == "Royal Gold" and pick.cls.price_minor == 49000 and alts == []


def test_full_row_offers_nearest_rows_with_seats_together():
    taken = {f"A{n}" for n in range(1, 23)}
    pick, alts = cinema.choose_seats(KOTHRUD, taken, 2, rows=["A"])
    assert pick is None and alts and alts[0].row == "B"  # nearest row first
    assert all(len(a.seats) == 2 for a in alts)


def test_housefull_show_has_no_pick_and_no_alternatives():
    sid = cinema.show_id(KOTHRUD, NOW.date(), "19:00")
    taken = cinema.presold(KOTHRUD, sid, "19:00")
    assert cinema.choose_seats(KOTHRUD, taken, 2) == (None, [])
    other = cinema.presold(KOTHRUD, cinema.show_id(KOTHRUD, NOW.date(), "22:45"), "22:45")
    assert cinema.choose_seats(KOTHRUD, other, 2)[0] is not None  # 10:45 PM still has seats


def test_seat_class_preference_is_respected():
    pick, _ = cinema.choose_seats(KOTHRUD, set(), 3, seat_class="silver")
    assert pick.cls.name == "Royal Silver" and pick.row in ("N", "O")


def test_theatre_and_show_matching():
    pune = cinema.theatres_for("Pune")
    assert cinema.match_theatre("citipride kothrud", pune).id == "city-pride-kothrud"
    assert cinema.match_theatre("PVR SB road", pune).id == "pvr-pavillion"
    assert cinema.match_theatre("seasons mall hadapsar", pune).id == "cinepolis-seasons"
    assert cinema.match_theatre("random hall", pune) is None
    times = list(KOTHRUD.times)
    assert cinema.match_show("10:45 pm", times) == "22:45"
    assert cinema.match_show("10:45", times) == "22:45"  # AM/PM resolved by the show list
    assert cinema.match_show("7 baje", times) == "19:00"
    assert cinema.match_show("night", times) == "22:45"
    assert cinema.match_show("9 am", times) is None
    assert cinema.theatres_for("Beed")[0].name == "Sandbox Cinema: Beed Central"


def test_grounding_adds_payment_step_and_date():
    lang = detect_language("book 2 ramayana tickets")
    draft = DraftPlan(goal="b", constraints=[], steps=[DraftStep(
        key="s1", tool="book_movie_tickets", description="Book",
        args={"movie": "Ramayana", "city": "Pune", "theatre": "citipride kothrud", "tickets": 2,
              "rows": ["a"], "when_text": "kal", "show_time": "10:45 pm"})])
    plan = ground_plan(draft, planner="t", language=lang, tz="Asia/Kolkata", now=NOW)
    assert [s.tool for s in plan.steps] == ["book_movie_tickets", "pay_movie_booking"]
    assert plan.steps[0].args["show_date"] == "2026-11-13" and plan.steps[0].args["rows"] == ["A"]
    pay = plan.steps[1]
    assert pay.args == {"hold_id": "{{s1.hold_id}}"} and pay.depends_on == ["s1"]
    assert pay.requires_approval and pay.risk_level == 3
    no_city = DraftPlan(goal="b", constraints=[], steps=[DraftStep(
        key="s1", tool="book_movie_tickets", description="Book", args={"movie": "Ramayana", "tickets": 2})])
    first = ground_plan(no_city, planner="t", language=lang, tz="Asia/Kolkata", now=NOW).steps[0]
    assert first.args["question_key"] == "movie_city_needed"


def test_replies_for_hold_choices_payment_and_confirmation():
    held = {"key": "s1", "tool": "book_movie_tickets", "status": "COMPLETED_LOCALLY", "result": {
        "kind": "held", "movie": "Ramayana", "theatre": "City Pride: Kothrud", "starts_at": "2026-11-14T22:45:00+05:30",
        "seats": ["A10", "A11"], "seat_class": "Royal Gold", "unit_price_minor": 49000, "amount_minor": 98000,
        "requested_rows": ["A"], "bms_link": "https://in.bookmyshow.com/movies/pune/ramayana/buytickets/X/20261114"}}
    pay = {"key": "s2", "tool": "pay_movie_booking", "status": "AWAITING_APPROVAL", "approval_id": "ap1",
           "result": {"amount_minor": 98000}}
    text, actions = render_task_reply("en", [held, pay])
    assert "TEST" in text and "A10, A11" in text and "₹980" in text and "Sat 14 Nov, 10:45 PM" in text
    assert "row A as asked" in text and actions == [{"type": "approval", "approval_id": "ap1", "step_key": "s2"}]
    done = {**pay, "status": "CONFIRMED_BY_PROVIDER", "result": {
        "booking_code": "TEST-1A2B3C4D", "payment_ref": "TESTPAY-X", "amount_minor": 98000, "movie": "Ramayana",
        "theatre": "City Pride: Kothrud", "starts_at": "2026-11-14T22:45:00+05:30", "seats": ["A10", "A11"],
        "seat_class": "Royal Gold"}}
    text, _ = render_task_reply("en", [held, done])
    assert "TEST-1A2B3C4D" in text and "nothing charged" in text and "bookmyshow.com" in text
    q = {"key": "s1", "tool": "book_movie_tickets", "status": "AWAITING_USER_ACTION", "result": {
        "question_key": "seats_unavailable", "clause": "book 2 tickets for Ramayana in Pune",
        "choices": ["Row B: B10, B11 (Royal Gold, ₹490 each)", "Row C: C5, C6 (Royal Gold, ₹490 each)"]}}
    text, _ = render_task_reply("en", [q])
    assert "1. Row B: B10, B11" in text and "2. Row C" in text and "Reply with a number" in text


def test_choice_answer_is_merged_with_the_offered_options():
    from apna.services.conversation import _merge

    step = SimpleNamespace(tool="book_movie_tickets", args={}, description="Book", result={
        "question_key": "seats_unavailable", "request": "book 2 tickets for Ramayana in Pune at citipride row A",
        "clause": "book 2 tickets for Ramayana in Pune at citipride row A",
        "choices": ["Row B: B10, B11 (Royal Gold, ₹490 each)"]})
    merged = _merge([step], "1")
    assert merged.startswith("book 2 tickets for Ramayana in Pune at citipride row A.")
    assert "Options offered: 1) Row B: B10, B11" in merged and merged.endswith("User's choice: 1")


def test_replies_to_cinema_questions_are_resolved_in_code():
    from apna.agent.executors import resolve_cinema_choice

    args = {"movie": "Drishyam The Conclusion", "city": "Pune", "when_text": "aaj", "tickets": 2, "rows": [],
            "theatre": None, "show_time": None, "seat_class": None}
    theatres = ["City Pride: Kothrud (Kothrud) - 4:00 PM, 10:45 PM", "PVR: Pavillion Mall, SB Road (Shivajinagar) - 9:40 PM"]
    assert resolve_cinema_choice("theatre_choice", args, theatres, "kothrud") == \
        "book 2 tickets for Drishyam The Conclusion in Pune aaj at City Pride: Kothrud"
    assert resolve_cinema_choice("theatre_choice", args, theatres, "2").endswith("at PVR: Pavillion Mall, SB Road")
    assert resolve_cinema_choice("theatre_choice", args, theatres, "doosra").endswith("SB Road")
    shows = ["4:00 PM - 205 seats free", "7:00 PM - HOUSEFULL", "10:45 PM - 159 seats free"]
    kothrud = {**args, "theatre": "City Pride: Kothrud"}
    assert resolve_cinema_choice("show_choice", kothrud, shows, "3").endswith("at City Pride: Kothrud 10:45 pm show")
    assert resolve_cinema_choice("show_choice", kothrud, shows, "10:45 wala").endswith("10:45 pm show")
    full = ["City Pride: Kothrud - 4:00 PM (205 seats free)", "INOX: Bund Garden Road - 9:00 PM (80 seats free)"]
    assert resolve_cinema_choice("show_full", kothrud, full, "2").endswith("at INOX: Bund Garden Road 9:00 pm show")
    rows = ["Row I: I12, I13 (Royal Gold, ₹490 each)", "Row K: K7, K8 (Royal Gold, ₹490 each)"]
    assert resolve_cinema_choice("seats_unavailable", {**kothrud, "rows": ["D"]}, rows, "2").endswith("row K")
    assert resolve_cinema_choice("seats_unavailable", {**kothrud, "rows": ["D"]}, rows, "row b").endswith("row B")
    assert resolve_cinema_choice("theatre_choice", args, theatres, "kuch bhi") is None  # -> model re-plans
