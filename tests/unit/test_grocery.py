"""Grocery (sandbox): equivalent quantities, variants, store choice, serviceability, answers, grounding."""

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from apna.agent.commerce import (
    grocery_request,
    resolve_grocery_answer,
    resolve_store_choice,
)
from apna.agent.grounding import ground_plan
from apna.agent.language import detect_language
from apna.agent.schemas import DraftPlan, DraftStep
from apna.providers import grocery
from apna.providers import grocery_quote as gq

NOW = datetime(2026, 11, 14, 13, 0, tzinfo=ZoneInfo("Asia/Kolkata"))


def test_item_names_in_english_hinglish_hindi_marathi():
    for name, group in [("milk", "milk"), ("doodh", "milk"), ("दूध", "milk"), ("kanda", "onion"), ("pyaz", "onion"),
                        ("tamatar", "tomato"), ("jeera", "cumin"), ("elaichi", "cardamom"), ("tej patta", "bay leaf"),
                        ("biryani masala", "biryani masala"), ("dalchini", "cinnamon"), ("toor dal", "toor dal"),
                        ("anda", "eggs")]:
        assert grocery.find_group(name) == group, name
    assert grocery.find_group("unicorn dust") is None


def test_quantities_are_amounts_not_pack_counts():
    milk = gq.make_request("milk", size="2 litre")
    assert (milk.amount, milk.unit, milk.line) == (2000, "ml", None)  # line not chosen yet -> ask
    eggs = gq.make_request("eggs", qty=12)
    assert (eggs.amount, eggs.unit, eggs.line) == (12, "pcs", "Farm Fresh White Eggs")  # one line: no question
    onion = gq.make_request("kanda", size="1 kg")
    assert (onion.amount, onion.unit) == (1000, "g")
    maggi = gq.make_request("maggi", qty=2)
    assert (maggi.amount, maggi.packs) == (None, 2)
    assert gq.make_request("milk", brand="amul taaza").line == "Amul Taaza Toned Milk"
    assert gq.parse_amount("1.5 ltr") == (1500, "ml") and gq.parse_amount("250gm") == (250, "g")


def test_variants_are_asked_not_guessed():
    assert gq.ambiguous(gq.make_request("milk", size="2 litre")) == [
        "Amul Taaza Toned Milk", "Amul Gold Full Cream Milk", "Gokul Full Cream Milk"]
    assert gq.ambiguous(gq.make_request("eggs", qty=12)) == []


def test_equivalent_pack_mix_per_store():
    milk = gq.make_request("milk", size="2 litre", brand="Amul Taaza Toned Milk")
    blinkit = gq.plan_packs(grocery.STORES["blinkit"], milk, milk.line)
    zepto = gq.plan_packs(grocery.STORES["zepto"], milk, milk.line)
    assert [(p.size, n) for p, n in blinkit] == [("1 l", 2)]  # ₹112 either way at Blinkit: fewest packs
    assert [(p.size, n) for p, n in zepto] == [("500 ml", 4)]  # Zepto: 4×500 ml (₹112) beats 2×1 L (₹114)
    eggs = gq.make_request("eggs", qty=12)
    assert [(p.size, n) for p, n in gq.plan_packs(grocery.STORES["blinkit"], eggs, eggs.line)] == [("12 pcs", 1)]


def test_acceptance_compare_blinkit_vs_zepto_with_fees():
    reqs = [gq.make_request("milk", size="2 litre", brand="Amul Taaza Toned Milk"), gq.make_request("eggs", qty=12)]
    quotes = gq.compare(reqs, "Pune", ["blinkit", "zepto"])
    by = {q.store.id: q for q in quotes}
    z, b = by["zepto"], by["blinkit"]
    assert z.eligible and b.eligible
    # Zepto: 4×500 ml ₹112 + 12 eggs ₹100; free delivery above ₹99; handling ₹3
    assert (z.breakdown.items_minor, z.breakdown.fees_minor) == (21200, {"delivery": 0, "handling": 300})
    # Blinkit: 2×1 L ₹112 + 12 eggs ₹99; free delivery above ₹199; handling ₹4
    assert (b.breakdown.items_minor, b.breakdown.fees_minor) == (21100, {"delivery": 0, "handling": 400})
    assert [q.total for q in quotes] == sorted(q.total for q in quotes)  # cheapest total first, fees included
    assert "total incl. fees" in gq.option_text(z)


def test_serviceability_minimum_order_and_stock():
    small = gq.compare([gq.make_request("chips")], "Pune", ["zepto"])[0]
    assert not small.eligible and "minimum order is ₹99" in small.reasons
    town = gq.compare([gq.make_request("eggs", qty=12)], "Ambajogai", ["blinkit"])[0]
    assert not town.eligible and town.reasons == ["doesn't deliver to Ambajogai"]
    saffron = gq.compare([gq.make_request("saffron")], "Pune", ["blinkit"])[0]
    assert saffron.missing == ["saffron"] and not saffron.eligible


def test_answers_to_grocery_questions_resolved_in_code():
    args = {"items": [{"name": "milk", "qty": 1, "size": "2 litre", "brand": None},
                      {"name": "eggs", "qty": 1, "size": "12", "brand": None}],
            "stores": ["Blinkit", "Zepto"], "choose": "cheapest", "city": "Pune", "area": "Kothrud"}
    variant = {"args": args, "item": "milk", "cheapest_line": "Amul Taaza Toned Milk",
               "choices": ["Amul Taaza Toned Milk (500 ml ₹28, 1 l ₹56)", "Amul Gold Full Cream Milk (500 ml ₹34)"]}
    out = resolve_grocery_answer("variant_choice", variant, "2")
    assert "2 litre Amul Gold Full Cream Milk milk" in out and "from Blinkit or Zepto" in out
    assert "Amul Taaza Toned Milk milk" in resolve_grocery_answer("variant_choice", variant, "sasta wala")
    assert "Amul Taaza" in resolve_grocery_answer("variant_choice", variant, "amul taaza")
    sub = {"args": args, "item": "milk", "choices": ["Use Gokul Full Cream Milk instead", "Skip milk",
                                                     "Try Swiggy Instamart (has milk)"]}
    assert "Gokul Full Cream Milk milk" in resolve_grocery_answer("substitution_choice", sub, "1")
    assert "milk" not in resolve_grocery_answer("substitution_choice", sub, "skip").split("order ")[1].split(",")[0]
    assert "from Swiggy Instamart" in resolve_grocery_answer("substitution_choice", sub, "3")
    store_choices = ["Zepto - ₹114 total incl. fees, ~10 min", "Blinkit - ₹155 total incl. fees, ~10 min"]
    assert "from Zepto" in resolve_store_choice(args, store_choices, "choose the cheaper one")
    assert "from Blinkit" in resolve_store_choice(args, store_choices, "2")
    price = {"args": args, "choices": ["Yes, rebuild the cart at today's price", "Cancel"]}
    assert resolve_grocery_answer("price_changed", price, "yes").startswith("order ")
    assert resolve_grocery_answer("price_changed", price, "cancel") is None
    assert grocery_request(args) == ("order 2 litre milk, 12 eggs from Blinkit or Zepto, choose the cheapest total, "
                                     "deliver to Kothrud, Pune")


def test_grounding_area_question_and_payment_steps():
    lang = detect_language("order milk")
    no_area = DraftPlan(goal="g", constraints=[], steps=[DraftStep(
        key="s1", tool="order_groceries", description="Order", args={"items": [{"name": "milk", "qty": 2}],
                                                                      "store": "blinkit"})])
    first = ground_plan(no_area, planner="t", language=lang, tz="Asia/Kolkata", now=NOW).steps[0]
    assert first.args["question_key"] == "delivery_area_needed"
    from apna.services.conversation import _merge

    merged = _merge([SimpleNamespace(tool="ask_user", result=None, description="", args=first.args)], "Kothrud, Pune")
    assert merged.endswith("deliver to Kothrud, Pune")
    edit = DraftPlan(goal="g", constraints=[], steps=[DraftStep(
        key="s1", tool="update_grocery_cart", description="Edit", args={"changes": [{"action": "remove",
                                                                                       "name": "bread"}]})])
    plan = ground_plan(edit, planner="t", language=lang, tz="Asia/Kolkata", now=NOW)
    assert [s.tool for s in plan.steps] == ["update_grocery_cart", "pay_grocery_order"]
    assert plan.steps[1].args == {"order_id": "{{s1.order_id}}"} and plan.steps[1].requires_approval
    weather = DraftPlan(goal="w", constraints=[], steps=[DraftStep(
        key="s1", tool="get_weather", description="Weather", args={"place": "Pune", "when_text": "kal"})])
    assert ground_plan(weather, planner="t", language=lang, tz="Asia/Kolkata", now=NOW).steps[0].args["day"] == \
        "2026-11-15"


def test_reprice_detects_changed_prices(monkeypatch):
    order = SimpleNamespace(store="blinkit", items=[{"sku": "eggs-12", "qty": 1}], total_minor=0)
    base = grocery.reprice(order)
    assert base == 9900 + 2500 + 400
    monkeypatch.setitem(grocery.STORES, "blinkit", grocery.Store(**{**grocery.STORES["blinkit"].__dict__,
                                                                    "price_factor": 1.10}))
    assert grocery.reprice(order) != base
