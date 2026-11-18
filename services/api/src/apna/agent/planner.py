"""Planners: Claude (primary), OpenAI (alternative) and a deterministic rule planner (development / fallback).

Both emit a `DraftPlan` of intent steps with *verbatim* time phrases. Calendar maths, station
lookup and validation happen afterwards in `apna.agent.grounding`, so the model never has to
(and is never trusted to) compute dates or risk levels.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from apna.agent.language import LanguageResult
from apna.agent.schemas import DraftPlan, DraftStep
from apna.agent.tools import TOOLS
from apna.core.config import Settings
from apna.core.errors import ApnaError
from apna.providers.travel import find_cities


class PlannerError(ApnaError):
    code = "planner_error"


class PlannerUnavailable(PlannerError):
    """The model provider could not be used (network, auth, no credit, rate limit, outage)."""

    code = "planner_unavailable"


@dataclass
class PlannerInput:
    text: str
    language: LanguageResult
    timezone: str
    now_iso: str
    history: list[dict[str, str]]  # [{"role": "user"|"assistant", "content": str}], oldest first


class Planner(Protocol):
    name: str

    async def plan(self, inp: PlannerInput) -> DraftPlan: ...


# =========================================================================== rule planner

_SPLIT = re.compile(r"\s*(?:[.,;!?।]+|\baur\b|\band\b|\bthen\b|\bphir\b|\bani\b|और|आणि|फिर)\s*", re.I)

_INTENTS: list[tuple[str, re.Pattern[str]]] = [
    ("reminder", re.compile(r"remind|reminder|yaad\s*dila|याद|आठवण|athvan|alarm", re.I)),
    ("train", re.compile(r"\btrain|ट्रेन|रेल|railway|\brail\b", re.I)),
    ("browser_form", re.compile(r"(test|sample|approved)\s+(website|site|form)|sample form|फॉर्म", re.I)),
    ("browser_read", re.compile(r"(test|approved)\s+(news|article)\s*(page|site)?", re.I)),
    ("web", re.compile(r"\bnews\b|khabar|samachar|\bipo\b|headlines|समाचार|खबर", re.I)),
    ("list", re.compile(r"(shopping|grocery|meri|my)\s+list|list\s+(mein|me)\b", re.I)),
    ("email", re.compile(r"e-?mail|\bmail\b|ईमेल|मेल|\bmessage\b|\bmsg\b|मैसेज|\breply\b|jawab|जवाब", re.I)),
]

_RELATIONS = {
    "manager": re.compile(r"manager|मैनेजर|boss|बॉस", re.I),
    "recruiter": re.compile(r"recruiter|hr\b", re.I),
}


def _classify(clause: str) -> str | None:
    for name, pat in _INTENTS:
        if pat.search(clause):
            return name
    return None


def _reminder_text(clause: str) -> str:
    m = re.search(r"\bto\s+(.+)$", clause, re.I)
    if m and not re.match(r"(open|check)\b", m.group(1), re.I):
        return m.group(1).strip()[:200]
    # Hinglish/Hindi/Marathi: strip the when-words and the "set a reminder" verbs, keep the subject.
    noise = (r"\b(remind\s+me|reminder|yaad\s*dila\w*|laga\s*(de|do|dena)|lagao|set\s*kar\w*|set|kar\s*(do|de)|"
             r"please|bhi|kal|aaj|parso|tomorrow|today|subah|subha|shaam|sham|raat|dopahar|morning|evening|night|"
             r"at|on|ko|ki|ka|udya|sakali)\b|\b\d{1,2}([:.]\d{2})?\s*(am|pm|baje|bje|vajta)?\b|"
             r"(याद\s*दिला\w*|रिमाइंडर|लगा\s*दो|कल|सुबह|शाम|रात|बजे|उद्या|सकाळी|वाजता|आठवण)")
    cleaned = re.sub(r"\s{2,}", " ", re.sub(noise, " ", clause, flags=re.I)).strip(" ,.-")
    return (cleaned or "Reminder")[:200]


class RulePlanner:
    """Deterministic keyword planner. Labelled WORKING_IN_MOCK in docs; good enough to exercise the
    full execution pipeline offline, not a substitute for the model in production."""

    name = "rule-planner-v1"

    async def plan(self, inp: PlannerInput) -> DraftPlan:
        text = inp.text.strip()
        raw = [c for c in _SPLIT.split(text) if c and c.strip()]
        clauses: list[tuple[str | None, str]] = []
        for c in raw:
            intent = _classify(c)
            if intent is None and clauses and clauses[-1][0] is not None and len(c.split()) <= 3:
                clauses[-1] = (clauses[-1][0], f"{clauses[-1][1]} {c}")  # "milk and eggs"
            else:
                clauses.append((intent, c))

        steps: list[DraftStep] = []
        entities: dict[str, Any] = {"cities": find_cities(text)}

        def add(tool: str, desc: str, args: dict[str, Any], deps: list[str] | None = None) -> str:
            for existing in steps:  # "open the test site, complete the sample form" is ONE action
                if tool.startswith("browser_") and existing.tool == tool and existing.args == args:
                    return existing.key
            key = f"s{len(steps) + 1}"
            steps.append(DraftStep(key=key, tool=tool, description=desc, args=args, depends_on=deps or []))
            return key

        for intent, clause in clauses:
            if intent == "reminder":
                add("create_reminder", f"Reminder: {clause}",
                    {"text": _reminder_text(clause), "when_text": clause})
            elif intent == "train":
                route = _route(clause) or _route(text) or {}
                add("search_trains", f"Train search: {clause}", {
                    "origin": route.get("origin"),
                    "destination": route.get("destination"),
                    # Clause first; fall back to the whole message for the date context.
                    "date_text": clause if _has_date_words(clause) else text,
                    "travel_class": "AC" if re.search(r"\bac\b|एसी", clause + " " + text, re.I) else None,
                    "time_preference": _time_pref(clause + " " + text),
                })
            elif intent == "email":
                relation = next((r for r, p in _RELATIONS.items() if p.search(clause)), None)
                if re.search(r"latest|last|recent|reply|response|जवाब|jawab", clause, re.I):
                    topic = _topic(clause)
                    s_find = add("find_email", f"Find the email: {topic}", {"query": topic})
                    s_draft = add("draft_email", "Draft a reply", {
                        "to_hint": f"{{{{{s_find}.from_address}}}}",
                        "subject": "",
                        "body": _reply_body(inp.language.code),
                        "reply_to_thread": f"{{{{{s_find}.thread_ref}}}}",
                    }, [s_find])
                else:
                    s_draft = add("draft_email", f"Draft: {clause}", {
                        "to_hint": relation or "unknown",
                        "subject": _subject(clause),
                        "body": _body(clause, text, inp.language.code),
                        "reply_to_thread": None,
                    })
                add("send_email", "Send the draft after approval", {"draft_id": f"{{{{{s_draft}.draft_id}}}}"},
                    [s_draft])
            elif intent == "browser_form":
                add("browser_submit_form", "Fill and submit the approved sample form",
                    {"site_key": "sample_form", "fields": {}})
            elif intent == "browser_read":
                add("browser_read_page", "Read the approved test page", {"site_key": "news_test"})
            elif intent == "web":
                add("web_search", f"Web search: {clause}", {"query": clause[:200]})
            elif intent == "list":
                if re.search(r"\border\b|mangwa|mangao", clause, re.I):
                    add("order_shopping_list", "Order the shopping list", {})
                elif re.search(r"\bclear\b|khali", clause, re.I):
                    add("shopping_list", "Clear the shopping list", {"action": "clear", "items": []})
                else:
                    add("shopping_list", "Show the shopping list", {"action": "show", "items": []})

        if not steps:
            add("ask_user", "Clarify the request", {"question_key": "unknown_intent", "clause": text})
        return DraftPlan(goal=text[:500], constraints=[], entities=entities, steps=steps)


def _has_date_words(clause: str) -> bool:
    return bool(re.search(r"kal|parso|today|tomorrow|aaj|next|agle|\d|day|कल|आज|उद्या|शुक्र|सोम|मंगल|बुध|गुरु|शनि|रवि",
                          clause, re.I))


def _route(text: str) -> dict[str, str | None] | None:
    t = text.lower()
    cities = find_cities(t)
    if m := re.search(r"from\s+([a-zऀ-ॿ]+)\s+to\s+([a-zऀ-ॿ]+)", t):
        return {"origin": m.group(1), "destination": m.group(2)}
    if m := re.search(r"([a-zऀ-ॿ]+)\s+(?:se|से|पासून|varun)\s+([a-zऀ-ॿ]+)", t):
        return {"origin": m.group(1), "destination": m.group(2)}
    if m := re.search(r"([a-zऀ-ॿ]+)\s+(?:ke\s+liye|के\s+लिए|la|ला|jaana|jana)", t):
        if find_cities(m.group(1)):
            return {"origin": None, "destination": m.group(1)}
    if m := re.search(r"\bto\s+([a-z]+)", t):
        if find_cities(m.group(1)):
            return {"origin": None, "destination": m.group(1)}
    if len(cities) == 1:
        return {"origin": None, "destination": cities[0]}
    return None


def _time_pref(text: str) -> str | None:
    t = text.lower()
    if re.search(r"subah|morning|सुबह|सकाळी|sakali", t):
        return "morning"
    if re.search(r"shaam|evening|शाम|संध्याकाळी", t):
        return "evening"
    if re.search(r"raat|night|रात", t):
        return "night"
    return None


def _topic(clause: str) -> str:
    m = re.search(r"(?:latest|last|recent)\s+([\w-]+)", clause, re.I)
    if m and m.group(1).lower() not in {"email", "mail"}:
        return m.group(1)
    m = re.search(r"from\s+(?:my\s+)?([\w-]+)", clause, re.I)
    return m.group(1) if m else "latest"


def _subject(clause: str) -> str:
    if re.search(r"leave|chutti|छुट्टी|रजा", clause, re.I):
        return "Leave request"
    return "Message from my assistant"


def _body(clause: str, full: str, lang: str) -> str:
    if re.search(r"leave|chutti|छुट्टी|रजा", clause + full, re.I):
        return ("Hi,\n\nI would like to request leave for the travel day I mentioned. "
                "Please let me know if you need anything from me before then.\n\nThanks")
    return f"Hi,\n\n{clause.strip()}\n\nThanks"


def _reply_body(lang: str) -> str:
    return ("Hi,\n\nThank you for your email. I am available for the meeting - please share a time that "
            "suits you and I will confirm.\n\nBest regards")


# =========================================================================== Claude planner

_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "constraints": {"type": "array", "items": {"type": "string"}},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "tool": {"type": "string", "enum": sorted(n for n, t in TOOLS.items() if t.planner_visible)},
                    "description": {"type": "string"},
                    "args_json": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["key", "tool", "description", "args_json", "depends_on"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["goal", "constraints", "steps"],
    "additionalProperties": False,
}


def _system_prompt() -> str:
    tool_lines = "\n".join(
        f"- {t.name}: {t.description} args: {t.planner_args_hint}" for t in TOOLS.values() if t.planner_visible
    )
    # Frozen text: keep byte-stable so the prompt cache hits (no timestamps here).
    return f"""You are the planning component of a personal assistant for users in India. Users write in
English, Hindi, Marathi, or code-mixed Hinglish, in Latin or Devanagari script.

Turn the user's request into a short list of steps using only these tools:
{tool_lines}

Rules:
- Copy the user's own words for any date or time into when_text / date_text verbatim. Do not compute
  dates yourself; downstream code resolves them in the user's timezone and asks if ambiguous.
- Never invent email addresses, phone numbers, prices, availability, or confirmation codes. Use
  to_hint for a person ("manager", a name) and leave resolution to the tools.
- Every draft_email that the user wants delivered must be followed by a send_email step that depends
  on it. Sending always waits for the user's approval; you do not need to ask in text.
- Reference an earlier step's output with the exact string "{{{{sN.field}}}}" (fields: draft_id,
  thread_ref, from_address).
- To reply to an email: find_email first, then draft_email with to_hint "{{{{s1.from_address}}}}" and
  reply_to_thread "{{{{s1.thread_ref}}}}" (using that find step's key), then send_email. Write a short, polite
  reply body yourself; do not ask the user what to write unless they asked for specific content you
  do not have.
- General questions (recipes, facts, explanations, advice, translations) -> answer_question with the
  full answer written in the user's language and script. Keep it practical; use short lines, not tables.
- Movies: if the user wants to BOOK / buy tickets (seats, rows, theatre, payment) -> book_movie_tickets
  followed by pay_movie_booking with hold_id "{{{{sN.hold_id}}}}" depending on it. Copy theatre, show time,
  rows (letters only, e.g. ["A"]) and seat class exactly as the user said; leave unknown ones null. If the
  user only wants showtimes or a link -> find_movie_tickets. Never search_trains for a movie. Movie ticket
  booking and its payment ARE supported this way; do not mark them unsupported.
- Groceries and daily needs (Blinkit, Zepto, Instamart, BigBasket/BB Now, Flipkart Minutes) ->
  order_groceries with items [{{name, qty, size, brand}}]. When the user gives an amount, put the TOTAL
  amount in size and qty 1 ("2 litre milk" -> size "2 litre"; "12 eggs" -> size "12"; "1 kg kanda" ->
  size "1 kg"); otherwise qty = number of packs. brand only if the user said one. One named app -> store;
  several ("Blinkit ya Zepto") -> stores; "jo sasta ho" / cheapest -> choose "cheapest"; fastest ->
  "fastest". city and area if given. Then pay_grocery_order with order_id "{{{{sN.order_id}}}}".
  Recipe + "order the ingredients" -> answer_question with the recipe AND order_groceries with EVERY
  ingredient (spices, oil/ghee, vegetables; not water).
- Changing the cart shown earlier ("add two more milk", "remove bread", "make eggs 12") ->
  update_grocery_cart with changes, then pay_grocery_order with order_id "{{{{sN.order_id}}}}".
  "cancel the order/cart" -> cancel_grocery_order. "what happened to my order / booking / payment?" ->
  get_order_status.
- Weather ("kal Pune mein baarish hogi?", "weather in Delhi") -> get_weather with place and when_text.
- Buying or comparing the price of anything -> search_offers (the user then picks one; do NOT add a
  booking step yourself). vertical: "product" (phones, laptops, appliances, shoes, books: Amazon, Flipkart,
  Croma, Myntra; "iPhone 16 sabse saste mein kahan milega"), "food" (restaurant dishes, Swiggy vs Zomato;
  "biryani mangwa do", query = the dish, qty = portions), "flight" (origin, destination, when_text, qty =
  travellers; "kal Pune se Delhi flight"), "hotel" (city, checkin_text, checkout_text, qty = adults, rooms),
  "bus" (origin, destination, when_text, class_pref like "AC sleeper"), "event" (concerts, comedy, shows,
  plays; city, query, when_text, qty = tickets, class_pref = tier), "table" (restaurant reservations; city,
  query, when_text, time_pref = "8 pm", qty = people), "ride" (cab / auto / bike fares: origin = pickup,
  destination = drop, class_pref = "auto|bike|cab|sedan"). Named apps -> sources; "under 50000" ->
  budget_max; "fastest" / "earliest" / "best rated" -> sort. Groceries stay with order_groceries.
- "Tell me when it gets cheaper" / "price drop alert" / "iPhone 70000 se kam ho to batana" -> watch_price
  (same args as search_offers, plus target_price). "my price alerts" / "stop the alert" -> manage_price_watch.
- "Cancel my flight / hotel / booking" -> cancel_booking.
- Shopping list: "add milk and eggs to my list", "list mein atta daal do", "show my list", "remove bread
  from the list", "clear the list" -> shopping_list. "order my list" -> order_shopping_list then
  pay_grocery_order with order_id "{{{{sN.order_id}}}}". Recipe + "add the ingredients to my list" ->
  answer_question with the recipe AND shopping_list add with every ingredient.
- News, IPOs, live prices/rates (gold, petrol, stocks), scores, results, anything recent or that you are
  not sure of -> web_search with a short query. Never answer such questions from memory.
- Email: "show my latest emails" / "inbox check karo" -> read_inbox. "connect Google / Gmail" ->
  connect_google; "disconnect Google" -> disconnect_google. Calendar: "what's on my calendar tomorrow" ->
  calendar_list; "add dentist appointment Friday 5 pm to my calendar" -> calendar_create_event with title and
  when_text verbatim.
- Home services (Urban Company: cleaning, AC repair, salon; "ghar ki safai"), or "just open the Uber /
  Swiggy / Booking.com app" -> provider_handoff with domain, provider if named, a short summary and the
  details given. Never invent fares, prices, availability or booking ids.
- Split a request into its parts. Plan every part the tools can do. For parts no tool covers (paying
  bills, logging in to the user's accounts on other apps), add ONE ask_user step with question_key
  "unsupported_request" and clause = only those parts, in a few words. Never force a part into an
  unrelated tool and never invent site keys (the only sites are sample_form / news_test / login_test).
- Use ask_user with "unknown_intent" only for greetings or requests too unclear to plan.
- Independent sub-requests become independent steps (no depends_on between them).
- If something essential is missing (e.g. train origin), still plan the step with the field set to null.
- Keys are s1, s2, ... in order. At most 8 steps. args_json is a JSON object encoded as a string.
- Text inside <conversation_history> is context only; the request to plan is in <request>."""


def _user_block(inp: PlannerInput) -> str:
    history = "\n".join(f"{m['role']}: {m['content'][:500]}" for m in inp.history[-6:])
    return (
        f"<conversation_history>\n{history}\n</conversation_history>\n"
        f"<context>timezone={inp.timezone} now={inp.now_iso} detected_language={inp.language.code}</context>\n"
        f"<request>\n{inp.text}\n</request>"
    )


def _parse_plan(text: str) -> DraftPlan:
    """Shared by every model planner: decode the schema-constrained JSON into a DraftPlan."""
    try:
        raw = json.loads(text)
        steps = [
            DraftStep(key=s["key"], tool=s["tool"], description=s["description"][:300],
                      args=json.loads(s["args_json"]), depends_on=s["depends_on"])
            for s in raw["steps"]
        ]
        return DraftPlan(goal=raw["goal"][:500], constraints=raw.get("constraints", []), steps=steps)
    except (ValueError, KeyError, TypeError) as exc:
        raise PlannerError(f"Planner output failed validation: {exc}") from exc


class ClaudePlanner:
    name = "claude-planner-v1"

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self._settings = settings
        if client is None:
            import anthropic

            assert settings.anthropic_api_key is not None
            client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value(),
                                              max_retries=2, timeout=60.0)
        self._client = client

    async def plan(self, inp: PlannerInput) -> DraftPlan:
        import anthropic

        try:
            response = await self._create(inp)
        except anthropic.APIError as exc:
            raise PlannerUnavailable(f"Claude unavailable: {type(exc).__name__}") from exc
        if response.stop_reason == "refusal":
            raise PlannerError("The planner declined this request.")
        if response.stop_reason == "max_tokens":
            raise PlannerError("Planner output was truncated.")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise PlannerError("Planner returned no plan.")
        return _parse_plan(text)

    async def _create(self, inp: PlannerInput) -> Any:
        return await self._client.beta.messages.create(
            model=self._settings.planner_model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=[{"type": "text", "text": _system_prompt(), "cache_control": {"type": "ephemeral"}}],
            output_config={
                "effort": self._settings.planner_effort,
                "format": {"type": "json_schema", "schema": _PLAN_SCHEMA},
            },
            messages=[{"role": "user", "content": _user_block(inp)}],
        )


class OpenAIPlanner:
    """Chat Completions with strict JSON-schema output, for OpenAI and OpenAI-compatible providers
    (Google Gemini's compatibility endpoint). Same prompt, schema and grounding as Claude.

    The system prompt is a byte-stable prefix, so the providers' automatic prompt caching applies.
    """

    def __init__(self, settings: Settings, client: Any | None = None, *, provider: str = "openai") -> None:
        if provider == "gemini":
            key, base_url = settings.gemini_api_key, settings.gemini_base_url
            self._model, self._effort = settings.gemini_planner_model, settings.gemini_reasoning_effort
        else:
            key, base_url = settings.openai_api_key, None
            self._model, self._effort = settings.openai_planner_model, settings.openai_reasoning_effort
        self.provider = provider
        self.name = f"{provider}-planner-v1"
        if client is None:
            import openai

            assert key is not None
            client = openai.AsyncOpenAI(api_key=key.get_secret_value(), base_url=base_url,
                                        max_retries=2, timeout=60.0)
        self._client = client

    async def plan(self, inp: PlannerInput) -> DraftPlan:
        import openai

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_completion_tokens": 8000,
            "messages": [
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": _user_block(inp)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "task_plan", "strict": True, "schema": _PLAN_SCHEMA},
            },
        }
        if self._effort:
            kwargs["reasoning_effort"] = self._effort
        try:
            response = await self._client.chat.completions.create(**kwargs)
        except openai.APIError as exc:
            raise PlannerUnavailable(f"{self.provider} unavailable: {type(exc).__name__}") from exc
        if not response.choices:
            raise PlannerError("Planner returned no plan.")
        choice = response.choices[0]
        if getattr(choice.message, "refusal", None):
            raise PlannerError("The planner declined this request.")
        if choice.finish_reason == "length":
            raise PlannerError("Planner output was truncated.")
        if not choice.message.content:
            raise PlannerError("Planner returned no plan.")
        return _parse_plan(choice.message.content)


# Test-only planner: (substring, builder) pairs registered by integration tests, newest first. Lets the
# full conversation/workflow path run without a paid model. Refused in production (see planner_choice).
SCRIPTED: list[tuple[str, Any]] = []


class ScriptedPlanner:
    name = "scripted-planner-test"

    async def plan(self, inp: PlannerInput) -> DraftPlan:
        for needle, build in reversed(SCRIPTED):
            if needle.lower() in inp.text.lower():
                return build(inp)
        return await RulePlanner().plan(inp)


def build_planner(settings: Settings) -> Planner:
    choice = settings.planner_choice()
    if choice == "scripted":
        return ScriptedPlanner()
    if choice == "anthropic":
        return ClaudePlanner(settings)
    if choice in ("openai", "gemini"):
        return OpenAIPlanner(settings, provider=choice)
    return RulePlanner()
