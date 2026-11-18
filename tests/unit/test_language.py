from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from apna.agent.language import (
    detect_language,
    resolve_date,
    resolve_datetime,
    resolve_time,
)

IST = ZoneInfo("Asia/Kolkata")
# Thursday 12 Nov 2026, 10:00 IST
NOW = datetime(2026, 11, 12, 10, 0, tzinfo=IST)
TODAY = NOW.date()


@pytest.mark.parametrize("text,code", [
    ("Remind me tomorrow at 8 AM", "en"),
    ("Kal shaam Pune se Mumbai ki train check kar.", "hi-Latn"),
    ("माझ्यासाठी उद्या सकाळी कॅब बुक कर.", "mr"),
    ("Mummy ke liye medicine reminder set kar", "hi-Latn"),
    ("कल सुबह 8 बजे मुझे याद दिलाना", "hi"),
    ("Next Friday ko manager ke saath meeting schedule kar.", "hi-Latn"),
])
def test_detect_language(text, code):
    assert detect_language(text).code == code


def test_kal_future_is_tomorrow():
    r = resolve_date("Kal shaam Pune se Mumbai ki train check kar", TODAY)
    assert r.value == date(2026, 11, 13) and r.ambiguity is None


def test_kal_past_is_not_guessed():
    r = resolve_date("kal meeting hui thi", TODAY)
    assert r.value is None and r.ambiguity is not None


def test_kal_without_context_is_ambiguous():
    r = resolve_date("kal", TODAY)
    assert r.value is None and r.ambiguity.question_key == "kal_ambiguous"
    assert r.ambiguity.options == ["2026-11-13", "2026-11-11"]


def test_parso_and_marathi_udya():
    assert resolve_date("parso train dekh", TODAY).value == date(2026, 11, 14)
    assert resolve_date("उद्या सकाळी कॅब बुक कर", TODAY).value == date(2026, 11, 13)


def test_next_weekday_same_week_is_ambiguous():
    # Thursday: "next Friday" could be tomorrow (13th) or the 20th.
    r = resolve_date("next friday", TODAY)
    assert r.value is None and r.ambiguity.options == ["2026-11-13", "2026-11-20"]


def test_next_weekday_in_following_week_is_clear():
    assert resolve_date("next monday", TODAY).value == date(2026, 11, 16)
    assert resolve_date("agle somvar", TODAY).value == date(2026, 11, 16)


def test_iso_date_wins_over_kal():
    assert resolve_date("kal 2026-11-11", TODAY).value == date(2026, 11, 11)


@pytest.mark.parametrize("text,expected", [
    ("8 AM", time(8, 0)), ("8:30 pm", time(20, 30)), ("subah 8 baje", time(8, 0)),
    ("shaam 7 baje", time(19, 0)), ("raat 11 baje", time(23, 0)), ("raat 12 baje", time(0, 0)),
    ("20:15", time(20, 15)), ("08:00", time(8, 0)), ("सकाळी ८ वाजता", time(8, 0)),
    ("saadhe aath baje subah", time(8, 30)), ("dopahar 2 baje", time(14, 0)),
])
def test_resolve_time(text, expected):
    assert resolve_time(text).value == expected


def test_bare_hour_is_ambiguous():
    r = resolve_time("8 baje")
    assert r.value is None and r.ambiguity.question_key == "am_pm_ambiguous"


def test_reminder_tomorrow_8am_ist_to_utc():
    r = resolve_datetime("Remind me tomorrow at 8 AM", tz="Asia/Kolkata", now=NOW)
    assert r.ok and r.local.isoformat() == "2026-11-13T08:00:00+05:30"


def test_past_time_today_is_flagged():
    r = resolve_datetime("today at 9 AM", tz="Asia/Kolkata", now=NOW)
    assert not r.ok and r.ambiguities[0].question_key == "time_in_past"


def test_missing_time_is_asked():
    r = resolve_datetime("return ki reminder bhi laga de", tz="Asia/Kolkata", now=NOW)
    assert not r.ok and [a.question_key for a in r.ambiguities] == ["when_needed"]


def test_relative_reminder_times_are_exact():
    from datetime import UTC, datetime, timedelta

    from apna.agent.language import resolve_datetime

    now = datetime(2026, 11, 14, 7, 12, 43, tzinfo=UTC)  # 12:42:43 IST
    for text, minutes in [("remind me in 2 minute to pay electricity bill", 2), ("after 1 hour call mom", 60),
                          ("10 min baad yaad dila", 10), ("2 ghante mein bata dena", 120),
                          ("5 मिनट बाद याद दिला", 5), ("१० मिनिटांनी आठवण कर", 10), ("in half an hour", 30)]:
        res = resolve_datetime(text, tz="Asia/Kolkata", now=now)
        assert res.ok, text
        assert res.local == now.astimezone(res.local.tzinfo) + timedelta(minutes=minutes), text


def test_am_pm_resolved_when_only_one_reading_is_still_ahead():
    from datetime import UTC, datetime

    from apna.agent.language import resolve_datetime, resolve_time

    assert resolve_time("12:45").ambiguity.options == ["12:45", "00:45"]  # minutes kept in the question
    at_1243 = datetime(2026, 11, 14, 7, 13, tzinfo=UTC)  # 12:43 IST
    res = resolve_datetime("today at 12:45", tz="Asia/Kolkata", now=at_1243)
    assert res.ok and res.local.strftime("%H:%M") == "12:45"
    at_0700 = datetime(2026, 11, 14, 1, 30, tzinfo=UTC)  # 07:00 IST: both 8 AM and 8 PM are ahead
    assert resolve_datetime("8 baje yaad dila", tz="Asia/Kolkata", now=at_0700).ambiguities[0].question_key \
        == "am_pm_ambiguous"
