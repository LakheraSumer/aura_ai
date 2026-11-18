import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from apna.agent.grounding import ground_plan
from apna.agent.language import detect_language
from apna.agent.planner import (
    ClaudePlanner,
    OpenAIPlanner,
    PlannerError,
    PlannerInput,
    RulePlanner,
    build_planner,
)
from apna.agent.policy import action_hash, evaluate
from apna.agent.schemas import DraftPlan, DraftStep
from apna.agent.tools import get_tool, substitute_refs
from apna.core.config import Settings
from apna.core.errors import PolicyViolation

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 12, 10, 0, tzinfo=IST)


async def _plan(text: str):
    lang = detect_language(text)
    draft = await RulePlanner().plan(PlannerInput(text, lang, "Asia/Kolkata", NOW.isoformat(), []))
    return ground_plan(draft, planner="rule", language=lang, tz="Asia/Kolkata", now=NOW)


async def test_reminder_plan():
    plan = await _plan("Remind me tomorrow at 8 AM to call the bank")
    (s,) = plan.steps
    assert s.tool == "create_reminder" and s.args["fire_at_local"] == "2026-11-13T08:00:00+05:30"
    assert s.args["text"] == "call the bank" and s.risk_level == 1 and not s.requires_approval


async def test_hinglish_reminder_text_keeps_only_the_subject():
    plan = await _plan("Kal subah 8 baje mummy ke liye medicine reminder laga de")
    (s,) = plan.steps
    assert s.args["text"] == "mummy ke liye medicine"
    assert s.args["fire_at_local"] == "2026-11-13T08:00:00+05:30"


async def test_spec_example_hinglish_multi_intent():
    text = ("Bhai, kal Pune se Mumbai jaana hai. Ek achhi train dekh, manager ko leave ka message bhej, "
            "aur return ki reminder bhi laga de.")
    plan = await _plan(text)
    tools = [s.tool for s in plan.steps]
    assert tools == ["search_trains", "draft_email", "send_email", "ask_user"]
    train = plan.steps[0]
    assert train.args["origin"] == "pune" and train.args["destination"] == "mumbai"
    assert train.args["travel_date"] == "2026-11-13"
    assert plan.steps[2].requires_approval and plan.steps[2].depends_on == ["s2"]
    assert plan.steps[3].args["question_key"] == "when_needed"  # return reminder has no day/time: ask
    assert plan.approval_checkpoints == ["s3"] and plan.permission_level == 2


async def test_test13_independent_subtasks():
    plan = await _plan("Kal subah Mumbai ke liye train dekh aur manager ko mail draft kar.")
    by_tool = {s.tool: s for s in plan.steps}
    assert by_tool["ask_user"].args["question_key"] == "train_origin_needed"  # origin unknown: ask
    assert by_tool["draft_email"].depends_on == []  # independent of the train question
    assert by_tool["send_email"].requires_approval


async def test_reply_to_latest_meeting_email_uses_refs():
    plan = await _plan("Draft a response to my latest meeting email")
    assert [s.tool for s in plan.steps] == ["find_email", "draft_email", "send_email"]
    assert plan.steps[1].args["reply_to_thread"] == "{{s1.thread_ref}}"
    assert substitute_refs(plan.steps[1].args, {"s1": {"thread_ref": "T", "from_address": "a@b.test"}})[
        "reply_to_thread"] == "T"


def test_unknown_tool_rejected():
    draft = DraftPlan(goal="x", steps=[DraftStep(key="s1", tool="transfer_money", description="x", args={})])
    with pytest.raises(PolicyViolation):
        ground_plan(draft, planner="t", language=detect_language("x"), tz="Asia/Kolkata", now=NOW)


def test_cycle_rejected():
    draft = DraftPlan(goal="x", steps=[
        DraftStep(key="s1", tool="find_email", description="a", args={"query": "a"}, depends_on=["s2"]),
        DraftStep(key="s2", tool="find_email", description="b", args={"query": "b"}, depends_on=["s1"])])
    with pytest.raises(PolicyViolation):
        ground_plan(draft, planner="t", language=detect_language("x"), tz="Asia/Kolkata", now=NOW)


def test_invalid_args_rejected():
    draft = DraftPlan(goal="x", steps=[DraftStep(key="s1", tool="find_email", description="a",
                                                 args={"query": "a", "delete_all": True})])
    with pytest.raises(PolicyViolation):
        ground_plan(draft, planner="t", language=detect_language("x"), tz="Asia/Kolkata", now=NOW)


def test_policy_and_hash_binding():
    send = get_tool("send_email")
    assert not evaluate(send, {"draft_id": "d"}, None).allowed  # no verified recipient
    d = evaluate(send, {"draft_id": "d", "to_address": "a@b.test"}, None)
    assert d.allowed and d.requires_approval
    a = {"draft_id": "d", "to_address": "a@b.test", "subject": "s", "body": "hello"}
    assert action_hash("send_email", a) != action_hash("send_email", {**a, "body": "hello!"})
    assert action_hash("send_email", a) == action_hash("send_email", dict(reversed(list(a.items()))))


# ---------------------------------------------------------------- Claude planner (fake client, no network)


class _FakeMessages:
    def __init__(self, response):
        self.response, self.calls = response, []

    async def create(self, **kw):
        self.calls.append(kw)
        return self.response


def _fake_client(text: str | None, stop_reason: str = "end_turn"):
    content = [SimpleNamespace(type="text", text=text)] if text is not None else []
    msgs = _FakeMessages(SimpleNamespace(stop_reason=stop_reason, content=content))
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


async def test_claude_planner_request_shape_and_parse():
    plan_json = json.dumps({"goal": "remind", "constraints": [], "steps": [
        {"key": "s1", "tool": "create_reminder", "description": "Remind",
         "args_json": json.dumps({"text": "call bank", "when_text": "kal subah 8 baje"}), "depends_on": []}]})
    client, msgs = _fake_client(plan_json)
    planner = ClaudePlanner(Settings(anthropic_api_key="test-key"), client=client)
    lang = detect_language("kal subah 8 baje bank ko call karna yaad dila")
    draft = await planner.plan(PlannerInput("kal subah 8 baje bank ko call karna yaad dila", lang,
                                            "Asia/Kolkata", NOW.isoformat(), []))
    call = msgs.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in call["betas"]
    assert "thinking" not in call  # Opus 5.5: thinking is always on; never send disabled/budget
    plan = ground_plan(draft, planner=planner.name, language=lang, tz="Asia/Kolkata", now=NOW)
    assert plan.steps[0].args["fire_at_local"] == "2026-11-13T08:00:00+05:30"


async def test_claude_planner_refusal_and_bad_json():
    for client, _ in (_fake_client(None, "refusal"), _fake_client("{not json")):
        with pytest.raises(PlannerError):
            await ClaudePlanner(Settings(anthropic_api_key="k"), client=client).plan(
                PlannerInput("hi", detect_language("hi"), "Asia/Kolkata", NOW.isoformat(), []))


# ---------------------------------------------------------------- OpenAI planner (fake client, no network)


class _FakeCompletions:
    def __init__(self, response):
        self.response, self.calls = response, []

    async def create(self, **kw):
        self.calls.append(kw)
        return self.response


def _fake_openai(content: str | None, finish_reason: str = "stop", refusal: str | None = None):
    msg = SimpleNamespace(content=content, refusal=refusal)
    comp = _FakeCompletions(SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish_reason)]))
    return SimpleNamespace(chat=SimpleNamespace(completions=comp)), comp


async def test_openai_planner_request_shape_and_parse():
    plan_json = json.dumps({"goal": "remind", "constraints": [], "steps": [
        {"key": "s1", "tool": "create_reminder", "description": "Remind",
         "args_json": json.dumps({"text": "call bank", "when_text": "kal subah 8 baje"}), "depends_on": []}]})
    client, comp = _fake_openai(plan_json)
    planner = OpenAIPlanner(Settings(openai_api_key="test-key"), client=client)
    text = "kal subah 8 baje bank ko call karna yaad dila"
    lang = detect_language(text)
    draft = await planner.plan(PlannerInput(text, lang, "Asia/Kolkata", NOW.isoformat(), []))
    call = comp.calls[0]
    assert call["model"] == "gpt-5.4-mini"
    rf = call["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert call["messages"][0]["role"] == "system" and "<request>" in call["messages"][1]["content"]
    plan = ground_plan(draft, planner=planner.name, language=lang, tz="Asia/Kolkata", now=NOW)
    assert plan.steps[0].args["fire_at_local"] == "2026-11-13T08:00:00+05:30"


async def test_openai_planner_refusal_truncation_and_bad_json():
    inp = PlannerInput("hi", detect_language("hi"), "Asia/Kolkata", NOW.isoformat(), [])
    for client, _ in (_fake_openai(None, refusal="no"), _fake_openai('{"goal"', "length"),
                      _fake_openai("{not json"), _fake_openai(None)):
        with pytest.raises(PlannerError):
            await OpenAIPlanner(Settings(openai_api_key="k"), client=client).plan(inp)


def test_planner_choice():
    def pick(**kw):
        return Settings(_env_file=None, **{"planner_provider": "auto", **kw}).planner_choice()

    assert pick() == "rule"
    assert pick(openai_api_key="o") == "openai"
    assert pick(openai_api_key="o", anthropic_api_key="a") == "anthropic"
    assert pick(openai_api_key="o", anthropic_api_key="a", planner_provider="openai") == "openai"
    assert pick(planner_provider="openai") == "rule"  # chosen provider without a key → offline planner
    assert pick(openai_api_key="o", planner_provider="rule") == "rule"
    assert pick(openai_api_key="o", planner_enabled=False) == "rule"
    assert isinstance(build_planner(Settings(_env_file=None, openai_api_key="o", planner_provider="auto")), OpenAIPlanner)


async def test_provider_errors_become_planner_unavailable():
    import httpx
    import openai
    from apna.agent.planner import PlannerUnavailable

    class _Down:
        async def create(self, **kw):
            raise openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"))

    client = SimpleNamespace(chat=SimpleNamespace(completions=_Down()))
    inp = PlannerInput("hi", detect_language("hi"), "Asia/Kolkata", NOW.isoformat(), [])
    with pytest.raises(PlannerUnavailable):
        await OpenAIPlanner(Settings(openai_api_key="k"), client=client).plan(inp)


async def test_gemini_uses_openai_compatible_endpoint_and_own_model():
    plan_json = json.dumps({"goal": "g", "constraints": [], "steps": [
        {"key": "s1", "tool": "create_reminder", "description": "Remind",
         "args_json": json.dumps({"text": "call bank", "when_text": "kal subah 8 baje"}), "depends_on": []}]})
    client, comp = _fake_openai(plan_json)
    planner = OpenAIPlanner(Settings(gemini_api_key="g"), client=client, provider="gemini")
    await planner.plan(PlannerInput("x", detect_language("x"), "Asia/Kolkata", NOW.isoformat(), []))
    assert planner.name == "gemini-planner-v1" and comp.calls[0]["model"].startswith("gemini")
    s = Settings(_env_file=None, gemini_api_key="g", planner_provider="auto")
    assert s.planner_choice() == "gemini"
    assert Settings(_env_file=None, gemini_api_key="g", openai_api_key="o", planner_provider="auto").planner_choice() == "openai"
    assert Settings(_env_file=None, gemini_api_key="g", openai_api_key="o",
                    planner_provider="gemini").planner_choice() == "gemini"
    built = build_planner(s)
    assert isinstance(built, OpenAIPlanner) and built.name == "gemini-planner-v1"
    assert "generativelanguage.googleapis.com" in str(built._client.base_url)


def test_reply_to_help_prompt_is_a_fresh_request_not_merged():
    from apna.services.conversation import _merge

    help_ask = SimpleNamespace(tool="ask_user", args={"question_key": "unknown_intent", "clause": "hi"},
                               result=None, description="Clarify the request")
    msg = "Kal subah 8 baje mummy ke liye medicine reminder laga de"
    assert _merge([help_ask], msg) == msg
    time_ask = SimpleNamespace(tool="ask_user", args={"question_key": "am_pm_ambiguous", "clause": "remind me at 8"},
                               result=None, description="Clarify")
    assert _merge([time_ask], "subah") == "remind me at 8 subah"


def test_unrecognised_question_key_is_treated_as_fresh_request():
    from apna.services.conversation import _merge

    ask = SimpleNamespace(tool="ask_user", args={"question_key": "greeting", "clause": "hi"},
                          result=None, description="Clarify")
    assert _merge([ask], "remind me at 8 pm to call papa") == "remind me at 8 pm to call papa"


def test_unsupported_and_invented_sites_become_one_honest_answer():
    from apna.agent.schemas import DraftPlan, DraftStep
    from apna.agent.tools import BROWSER_SITE_KEYS
    from apna.browser.executor import SITES

    assert set(SITES) == BROWSER_SITE_KEYS  # grounding's allow-list matches the real browser config
    draft = DraftPlan(goal="movie", constraints=[], steps=[
        DraftStep(key="s1", tool="browser_read_page", description="Open BookMyShow", args={"site_key": "bookmyshow"}),
        DraftStep(key="s2", tool="browser_submit_form", description="Book 2 tickets", depends_on=["s1"],
                  args={"site_key": "bookmyshow", "fields": {"tickets": "2"}})])
    lang = detect_language("book ticket ramayana 2 person tonight pune")
    plan = ground_plan(draft, planner="t", language=lang, tz="Asia/Kolkata", now=NOW)
    assert [(s.tool, s.args["question_key"]) for s in plan.steps] == [("ask_user", "unsupported_request")]
    ok = DraftPlan(goal="news", constraints=[], steps=[
        DraftStep(key="s1", tool="browser_read_page", description="Read", args={"site_key": "news_test"})])
    assert ground_plan(ok, planner="t", language=lang, tz="Asia/Kolkata", now=NOW).steps[0].tool == "browser_read_page"
    off = ground_plan(ok, planner="t", language=lang, tz="Asia/Kolkata", now=NOW, browser_enabled=False)
    assert off.steps[0].args["question_key"] == "unsupported_request"


def test_non_city_reply_to_train_question_is_a_fresh_request():
    from apna.services.conversation import _merge

    ask = SimpleNamespace(tool="ask_user", result=None, description="Clarify",
                          args={"question_key": "train_destination_needed", "clause": "tonight",
                                "partial": {"origin": "PUNE", "destination": None}})
    assert _merge([ask], "movie ticket from bookmyshow") == "movie ticket from bookmyshow"
    assert _merge([ask], "Mumbai").startswith("train from PUNE to Mumbai")


def test_reminder_subject_survives_time_questions():
    from apna.services.conversation import _merge

    ask = SimpleNamespace(tool="ask_user", result=None, description="Clarify",
                          args={"question_key": "time_needed", "clause": "today",
                                "partial": {"tool": "create_reminder", "text": "pay electricity bill"}})
    assert _merge([ask], "12:45 pm") == "remind me to pay electricity bill today 12:45 pm"


def test_movie_links_use_bookmyshow_formats_and_never_guess_ids():
    from datetime import date

    from apna.providers.movies import (
        BMS_HOME,
        bms_city_slug,
        city_page,
        match_movie,
        parse_bms_links,
        show_page,
    )

    assert bms_city_slug("Pune") == "pune" and bms_city_slug("Bombay") == "mumbai"
    assert bms_city_slug("gurgaon") == "national-capital-region-ncr" and bms_city_slug("पुणे") == "pune"
    assert bms_city_slug("Ambajogai") == "ambajogai" and bms_city_slug("Navi Mumbai") == "navi-mumbai"
    assert bms_city_slug("அம்பா") is None and city_page(None) == (BMS_HOME, False)
    # the exact format from the user's own BookMyShow link
    assert show_page("ambajogai", "ramayana", "ET00451914", date(2026, 11, 14)) ==         "https://in.bookmyshow.com/movies/ambajogai/ramayana/buytickets/ET00451914/20261114"
    link = "https://in.bookmyshow.com/movies/ambajogai/ramayana/buytickets/ET00451914/20261114?etCodes=ET00451914"
    assert parse_bms_links("see " + link) == [("ambajogai", "ramayana", "ET00451914")]
    rows = [SimpleNamespace(slug=s) for s in ("ramayana", "rajini-the-jailer-2", "drishyam-the-conclusion")]
    assert match_movie("Ramayan", rows).slug == "ramayana"
    assert match_movie("Jailer 2", rows).slug == "rajini-the-jailer-2"
    assert match_movie("Drishyam The Conclusion", rows).slug == "drishyam-the-conclusion"
    assert match_movie("Pushpa 3", rows) is None  # unknown -> no made-up ID


def test_movie_and_answer_replies_render():
    from apna.agent.replies import render_task_reply

    text, _ = render_task_reply("en", [
        {"key": "s1", "tool": "answer_question", "status": "COMPLETED_LOCALLY", "result": {"answer": "Biryani: ..."}},
        {"key": "s2", "tool": "find_movie_tickets", "status": "COMPLETED_LOCALLY",
         "result": {"movie": "Ramayana", "city": "pune", "tickets": 2, "show_date": "2026-11-14", "exact": True,
                    "official_link": "https://in.bookmyshow.com/movies/pune/ramayana/buytickets/ET00451914/20261114"}},
        {"key": "s3", "tool": "ask_user", "status": "AWAITING_USER_ACTION",
         "result": {"question_key": "unsupported_request", "clause": "ordering from Blinkit"}}])
    assert "Biryani: ..." in text and "buytickets/ET00451914/20261114" in text
    assert "Ramayana - Pune, Sat 14 Nov · 2 tickets" in text
    assert "ordering from Blinkit" in text and "book tickets" not in text


async def test_informational_notes_do_not_wait_for_an_answer():
    from apna.agent.executors import exec_ask_user
    from apna.agent.replies import render_task_reply

    note = await exec_ask_user(None, {"question_key": "unsupported_request", "clause": "paying for the tickets"})
    real = await exec_ask_user(None, {"question_key": "am_pm_ambiguous", "clause": "8 baje"})
    assert note.status == "COMPLETED_LOCALLY" and real.status == "AWAITING_USER_ACTION"
    text, _ = render_task_reply("en", [{"key": "s1", "tool": "ask_user", "status": "COMPLETED_LOCALLY",
                                        "result": note.result}])
    assert "paying for the tickets" in text


def test_movie_grounding_needs_city_and_resolves_date():
    from apna.agent.schemas import DraftPlan, DraftStep
    from apna.services.conversation import _merge

    lang = detect_language("book ramayana tickets")
    no_city = DraftPlan(goal="m", constraints=[], steps=[DraftStep(key="s1", tool="find_movie_tickets",
                        description="Movie", args={"movie": "Ramayana", "tickets": 2, "when_text": "kal"})])
    ask = ground_plan(no_city, planner="t", language=lang, tz="Asia/Kolkata", now=NOW).steps[0]
    assert ask.args["question_key"] == "movie_city_needed"
    merged = _merge([SimpleNamespace(tool="ask_user", result=None, description="", args=ask.args)], "Pune")
    assert merged == "movie tickets for Ramayana in Pune 2 tickets kal"
    ok = DraftPlan(goal="m", constraints=[], steps=[DraftStep(key="s1", tool="find_movie_tickets",
                   description="Movie", args={"movie": "Ramayana", "city": "Pune", "when_text": "kal"})])
    step = ground_plan(ok, planner="t", language=lang, tz="Asia/Kolkata", now=NOW).steps[0]
    assert step.args["show_date"] == "2026-11-13"  # NOW is 2026-11-12 in these tests
