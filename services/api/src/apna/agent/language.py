"""Language detection and Indian-context date/time resolution.

Deterministic and dependency-free so it can run before (and independently of) any model call.
Design rule (spec §11, ADR-008): when more than one reasonable interpretation exists we return
an *ambiguity* with options instead of guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------- language detection

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

_MARATHI_DEVA = {"उद्या", "आहे", "माझ्या", "माझ्यासाठी", "सकाळी", "संध्याकाळी", "करा", "नाही", "परवा",
                 "आठवण", "मला", "वाजता", "आणि", "पाहिजे", "बुक कर", "दुपारी", "रात्री"}
_HINDI_DEVA = {"कल", "है", "मेरे", "मेरा", "सुबह", "शाम", "लिए", "करो", "परसों", "याद", "बजे", "और",
               "मुझे", "दोपहर", "रात", "भेज", "देख"}
_HINGLISH = {"kal", "parso", "subah", "shaam", "sham", "raat", "dopahar", "ke", "liye", "kar", "karo",
             "karna", "dekh", "bhej", "laga", "lagao", "de", "dena", "mujhe", "mera", "meri", "bhai", "yaad",
             "dila", "dilana", "baje", "ko", "se", "aur", "hai", "jaana", "jana", "ki", "ka", "wala", "achhi",
             "accha", "acha", "agle", "agla", "mummy", "papa", "kya", "nahi", "haan"}
_MARATHI_LATN = {"udya", "sakali", "sakaali", "sandhyakali", "mala", "majha", "majhya", "aahe", "ahe",
                 "pahije", "ani", "vajta", "parva", "dupari", "ratri", "kara"}
_EN = {"the", "remind", "me", "tomorrow", "please", "find", "send", "draft", "my", "and", "at", "for",
       "to", "train", "email", "next", "book", "check", "with", "reply", "latest", "open", "complete"}

_WORD = re.compile(r"[a-zA-Z]+|[ऀ-ॿ]+")


@dataclass(frozen=True)
class LanguageResult:
    code: str  # en | hi | mr | hi-Latn | mr-Latn
    script: str  # Latn | Deva
    confidence: float
    code_mixed: bool


def detect_language(text: str) -> LanguageResult:
    words = [w.lower() for w in _WORD.findall(text)]
    if not words:
        return LanguageResult("en", "Latn", 0.3, False)
    deva_words = [w for w in words if _DEVANAGARI.search(w)]
    latin_words = [w for w in words if not _DEVANAGARI.search(w)]
    joined = " ".join(words)

    if len(deva_words) >= len(latin_words):
        mr = sum(1 for m in _MARATHI_DEVA if m in joined)
        hi = sum(1 for h in _HINDI_DEVA if h in joined)
        code = "mr" if mr > hi else "hi"
        conf = 0.6 + 0.1 * min(abs(mr - hi), 3)
        return LanguageResult(code, "Deva", min(conf, 0.95), bool(latin_words))

    hi_l = sum(1 for w in latin_words if w in _HINGLISH)
    mr_l = sum(1 for w in latin_words if w in _MARATHI_LATN)
    en_l = sum(1 for w in latin_words if w in _EN)
    if mr_l > hi_l and mr_l >= 1:
        return LanguageResult("mr-Latn", "Latn", min(0.5 + 0.1 * mr_l, 0.9), en_l > 0)
    if hi_l >= 2 or (hi_l >= 1 and hi_l >= en_l):
        return LanguageResult("hi-Latn", "Latn", min(0.5 + 0.08 * hi_l, 0.9), en_l > 0)
    return LanguageResult("en", "Latn", min(0.5 + 0.05 * en_l, 0.95), hi_l > 0)


def response_language(lang: LanguageResult) -> str:
    """Which language template family the reply should use."""
    return {"hi": "hi", "mr": "mr", "hi-Latn": "hi-Latn", "mr-Latn": "hi-Latn"}.get(lang.code, "en")


# --------------------------------------------------------------------------- date resolution


@dataclass
class Ambiguity:
    kind: str  # date | time | past_time
    question_key: str
    options: list[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class DateResolution:
    value: date | None
    matched: str | None = None
    ambiguity: Ambiguity | None = None


_PAST_MARKERS = re.compile(
    # Note: Hindi "the" (were) is deliberately excluded - it collides with English "the".
    r"\b(tha|thi|gaya|gayi|gaye|hua|hui|huye|kiya|kiye|kiyi|was|were|did|yesterday|happened)\b|"
    r"(था|थी|थे|गया|गई|गए|हुआ|हुई|किया)"
)
_FUTURE_MARKERS = re.compile(
    r"\b(kar|karo|karna|kardo|kar\s*de|de|dena|dekh|dekho|bhej|bhejo|laga|lagao|remind|reminder|yaad|"
    r"book|check|schedule|jaana|jana|chahiye|karunga|karungi|will|plan|find|search|draft|set)\b|"
    r"(कर|करो|करना|भेज|देख|लगा|याद|बुक)"
)

_WEEKDAYS: dict[str, int] = {
    "monday": 0, "mon": 0, "somvar": 0, "somwar": 0, "सोमवार": 0,
    "tuesday": 1, "tue": 1, "mangalvar": 1, "mangalwar": 1, "मंगलवार": 1, "मंगळवार": 1,
    "wednesday": 2, "wed": 2, "budhvar": 2, "budhwar": 2, "बुधवार": 2,
    "thursday": 3, "thu": 3, "guruvar": 3, "guruwar": 3, "brihaspativar": 3, "गुरुवार": 3,
    "friday": 4, "fri": 4, "shukravar": 4, "shukrawar": 4, "शुक्रवार": 4,
    "saturday": 5, "sat": 5, "shanivar": 5, "shaniwar": 5, "शनिवार": 5,
    "sunday": 6, "sun": 6, "ravivar": 6, "raviwar": 6, "itvar": 6, "रविवार": 6,
}
_NEXT_WORDS = r"(next|agle|agla|agli|pudhcha|pudhchya|अगले|अगला|पुढच्या|पुढचा)"
_THIS_WORDS = r"(this|is|iss|coming|ye|yeh|हा|इस)"


def _has(pattern: str, text: str) -> re.Match[str] | None:
    return re.search(pattern, text, re.I)


def resolve_date(text: str, today: date, *, future_context: bool = False) -> DateResolution:
    t = text.lower()
    future = future_context or bool(_FUTURE_MARKERS.search(t))
    past = bool(_PAST_MARKERS.search(t))

    # An explicit ISO date (e.g. from a clarification button) always wins.
    if m := _has(r"\b(\d{4})-(\d{2})-(\d{2})\b", t):
        return DateResolution(date(int(m.group(1)), int(m.group(2)), int(m.group(3))), m.group(0))
    if m := _has(r"\b(day after tomorrow)\b", t):
        return DateResolution(today + timedelta(days=2), m.group(0))
    if m := _has(r"\b(today|aaj|aj)\b|(आज)", t):
        return DateResolution(today, m.group(0))
    if m := _has(r"\b(tomorrow|tmrw|tmr|udya|udyā)\b|(उद्या)", t):
        return DateResolution(today + timedelta(days=1), m.group(0))

    # "kal"/"कल" = yesterday OR tomorrow; "parso"/"परसों"/"parva"/"परवा" = ±2 days.
    for pat, delta in ((r"\b(kal|kl)\b|(कल)", 1), (r"\b(parso|parson|parsoon|parva)\b|(परसों|परवा)", 2)):
        if m := _has(pat, t):
            if past and not future:
                return DateResolution(
                    None, m.group(0),
                    Ambiguity("date", "past_relative_date", [(today - timedelta(days=delta)).isoformat()],
                              "past-tense sentence"),
                )
            if future and not past:
                return DateResolution(today + timedelta(days=delta), m.group(0))
            return DateResolution(
                None, m.group(0),
                Ambiguity("date", "kal_ambiguous",
                          [(today + timedelta(days=delta)).isoformat(), (today - timedelta(days=delta)).isoformat()]),
            )

    # Weekdays: "next friday", "agle shukravar", "this friday", "friday ko"
    for name, wd in _WEEKDAYS.items():
        if not re.search(rf"(?<![\wऀ-ॿ]){re.escape(name)}(?![\wऀ-ॿ])", t):
            continue
        days_ahead = (wd - today.weekday()) % 7
        upcoming = today + timedelta(days=days_ahead or 7)
        if re.search(rf"{_NEXT_WORDS}\s+{re.escape(name)}", t):
            same_week = upcoming.isocalendar()[1] == today.isocalendar()[1]
            if same_week:
                return DateResolution(
                    None, name,
                    Ambiguity("date", "next_weekday_ambiguous",
                              [upcoming.isoformat(), (upcoming + timedelta(days=7)).isoformat()]),
                )
            return DateResolution(upcoming, name)
        if re.search(rf"{_THIS_WORDS}\s+{re.escape(name)}", t) or days_ahead:
            return DateResolution(upcoming, name)
        # weekday named == today, without "this"/"next": today or a week later?
        return DateResolution(None, name, Ambiguity("date", "weekday_is_today",
                                                    [today.isoformat(), (today + timedelta(days=7)).isoformat()]))

    # Explicit dates: 2026-11-20, 20/11/2026, 20-11 (Indian day-first convention)
    if m := _has(r"\b(\d{4})-(\d{2})-(\d{2})\b", t):
        return DateResolution(date(int(m.group(1)), int(m.group(2)), int(m.group(3))), m.group(0))
    if m := _has(r"\b(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?\b", t):
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else today.year
        y = y + 2000 if y < 100 else y
        try:
            value = date(y, mo, d)
        except ValueError:
            return DateResolution(None, m.group(0), Ambiguity("date", "invalid_date", [], m.group(0)))
        if not m.group(3) and value < today:
            value = date(y + 1, mo, d)
        return DateResolution(value, m.group(0))
    return DateResolution(None)


# --------------------------------------------------------------------------- time resolution

_NUM_WORDS = {
    "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5, "chhe": 6, "che": 6,
    "saat": 7, "aath": 8, "nau": 9, "das": 10, "gyarah": 11, "barah": 12, "baarah": 12,
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पांच": 5, "पाँच": 5, "छह": 6, "सात": 7, "आठ": 8, "नौ": 9,
    "दस": 10, "ग्यारह": 11, "बारह": 12,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12,
}
_MORNING = r"\b(subah|subha|savere|morning|sakali|sakaali|am|a\.m\.)\b|(सुबह|सकाळी)"
_AFTERNOON = r"\b(dopahar|dupahar|afternoon|dupari|noon)\b|(दोपहर|दुपारी)"
_EVENING = r"\b(shaam|sham|evening|sandhyakali|pm|p\.m\.)\b|(शाम|संध्याकाळी)"
_NIGHT = r"\b(raat|rat|night|ratri|tonight)\b|(रात|रात्री)"


@dataclass
class TimeResolution:
    value: time | None
    matched: str | None = None
    period_only: str | None = None
    ambiguity: Ambiguity | None = None


def _period(t: str) -> str | None:
    for name, pat in (("morning", _MORNING), ("afternoon", _AFTERNOON), ("evening", _EVENING), ("night", _NIGHT)):
        if re.search(pat, t, re.I):
            return name
    return None


def resolve_time(text: str) -> TimeResolution:
    t = text.lower().translate(_DEVANAGARI_DIGITS)
    t = re.sub(r"(\d)\s*(am|pm|a\.m\.|p\.m\.)", r"\1 \2", t)
    period = _period(t)

    hour: int | None = None
    minute = 0
    matched = None
    if m := re.search(r"\b(\d{1,2})[:.](\d{2})\b", t):
        hour, minute, matched = int(m.group(1)), int(m.group(2)), m.group(0)
        if m.group(1).startswith("0") and not period and minute <= 59:
            # Zero-padded 24-hour form ("08:00") is explicit; "10:30" stays AM/PM-ambiguous.
            return TimeResolution(time(hour, minute), matched)
    elif m := re.search(r"\b(\d{1,2})\s*(?:baje|bje|vajta|o'?clock|बजे|वाजता|am|pm|a\.m\.|p\.m\.)", t):
        hour, matched = int(m.group(1)), m.group(0)
    elif m := re.search(r"\bat\s+(\d{1,2})\b", t):
        hour, matched = int(m.group(1)), m.group(0)
    else:
        for word, n in _NUM_WORDS.items():
            if re.search(rf"(?<![\wऀ-ॿ]){word}\s*(baje|बजे|vajta|वाजता|o'?clock)", t):
                hour, matched = n, word
                break
    # "saadhe aath" (8:30), "sava aath" (8:15), "paune aath" (7:45)
    if hour is not None:
        if re.search(r"\b(saade|saadhe|sadhe)\b|साढ़े", t):
            minute = 30
        elif re.search(r"\b(sava|savva)\b|सवा", t):
            minute = 15
        elif re.search(r"\b(paune|pone)\b|पौने", t):
            hour, minute = (hour - 1) % 12 or 12, 45

    if hour is None:
        if period:
            return TimeResolution(None, None, period_only=period,
                                  ambiguity=Ambiguity("time", "time_needed_for_period", [], period))
        return TimeResolution(None)
    if hour > 23 or minute > 59:
        return TimeResolution(None, matched, ambiguity=Ambiguity("time", "invalid_time", [], matched or ""))
    if hour >= 13 or hour == 0:
        return TimeResolution(time(hour, minute), matched)

    if period == "morning":
        h = 0 if hour == 12 else hour
    elif period == "afternoon":
        h = hour if hour == 12 else (hour + 12 if hour <= 6 else hour)
    elif period == "evening":
        h = hour if hour == 12 else hour + 12
    elif period == "night":
        h = 0 if hour == 12 else (hour + 12 if hour >= 6 else hour)
    else:
        if hour == 12:
            return TimeResolution(None, matched, ambiguity=Ambiguity(
                "time", "am_pm_ambiguous", [f"12:{minute:02d}", f"00:{minute:02d}"]))
        return TimeResolution(
            None, matched,
            ambiguity=Ambiguity("time", "am_pm_ambiguous", [f"{hour:02d}:{minute:02d}", f"{hour + 12:02d}:{minute:02d}"]),
        )
    return TimeResolution(time(h, minute), matched)


# --------------------------------------------------------------------------- combined


@dataclass
class DateTimeResolution:
    local: datetime | None
    timezone: str
    date: DateResolution
    time: TimeResolution
    ambiguities: list[Ambiguity] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.local is not None and not self.ambiguities


# "in 2 minutes", "after 1 hour", "10 min baad", "2 ghante mein", "5 मिनट बाद", "10 मिनिटांनी", "2 तासांनी"
_REL_UNIT = (r"(minutes?|mins?|minat|hours?|hrs?|ghant[ae]|ghanto|मिनटों|मिनट|मिनिटांनी|मिनिटे|मिनिट|घंटों|घंटे|"
             r"घंटा|तासांनी|तास)")
_REL_BEFORE = re.compile(rf"\b(?:in|after|within)\s+(\d{{1,3}})\s*{_REL_UNIT}")
_REL_AFTER = re.compile(rf"(\d{{1,3}})\s*{_REL_UNIT}\s*(?:ke\s+)?(?:baad|bad|mein|me|later|बाद|में|नंतर|ने)")
_REL_MARATHI = re.compile(r"(\d{1,3})\s*(मिनिटांनी|तासांनी)")
_HALF_HOUR = re.compile(r"\bhalf\s+an?\s+hour\b|\baadh[ae]\s+ghant[ae]\b|आधे\s+घंटे|अर्ध्या\s+तासाने")


def resolve_relative(text: str, now_local: datetime) -> datetime | None:
    """Exact offsets from now. Hindi/Marathi digits are normalised first. Capped at 7 days."""
    t = text.lower().translate(_DEVANAGARI_DIGITS)
    if _HALF_HOUR.search(t):
        return now_local + timedelta(minutes=30)
    m = _REL_BEFORE.search(t) or _REL_AFTER.search(t) or _REL_MARATHI.search(t)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    minutes = n * 60 if re.match(r"h|ghant|घंट|तास", unit) else n
    if not 1 <= minutes <= 7 * 24 * 60:
        return None
    return now_local + timedelta(minutes=minutes)


def resolve_datetime(text: str, *, tz: str, now: datetime, future_context: bool = True,
                     require_time: bool = True) -> DateTimeResolution:
    zone = ZoneInfo(tz)
    now_local = now.astimezone(zone)
    dres = resolve_date(text, now_local.date(), future_context=future_context)
    tres = resolve_time(text)
    if (rel := resolve_relative(text, now_local)) is not None:
        # "in 2 minutes" is exact: no day/time questions.
        return DateTimeResolution(rel, tz, dres, TimeResolution(rel.time(), None), [])
    if tres.ambiguity and tres.ambiguity.question_key == "am_pm_ambiguous" and dres.ambiguity is None:
        # If only one of the two readings is still ahead on that day (e.g. "12:45" at 12:43), it is the
        # only valid reminder time, so it is not ambiguous. Both ahead (or both past) -> still ask.
        day = dres.value or now_local.date()
        ahead = [c for c in (datetime.combine(day, time.fromisoformat(o), zone) for o in tres.ambiguity.options)
                 if c > now_local]
        if len(ahead) == 1:
            tres = TimeResolution(ahead[0].time(), tres.matched)  # no date given -> "next occurrence" = today
    ambiguities = [a for a in (dres.ambiguity, tres.ambiguity) if a]

    # "tonight" implies today; "8 AM" with no date means next occurrence (only if unambiguous).
    d = dres.value
    if d is None and dres.ambiguity is None:
        if re.search(r"\btonight\b|\baaj\s+raat\b", text, re.I):
            d = now_local.date()
        elif tres.value is not None:
            candidate = datetime.combine(now_local.date(), tres.value, zone)
            d = now_local.date() if candidate > now_local else now_local.date() + timedelta(days=1)
        elif require_time:
            ambiguities.append(Ambiguity("date", "date_needed"))

    if tres.value is None and tres.ambiguity is None and require_time:
        if any(a.question_key == "date_needed" for a in ambiguities):
            # Neither day nor time given: ask one combined question instead of two.
            ambiguities = [a for a in ambiguities if a.question_key != "date_needed"]
            ambiguities.insert(0, Ambiguity("date", "when_needed"))
        else:
            ambiguities.append(Ambiguity("time", "time_needed"))

    local = None
    if d is not None and tres.value is not None:
        local = datetime.combine(d, tres.value, zone)
        if local <= now_local:
            ambiguities.append(Ambiguity("past_time", "time_in_past", [local.isoformat()]))
    return DateTimeResolution(local, tz, dres, tres, ambiguities)
