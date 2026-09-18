"""Rule-based note parser.

NOT the primary interpreter (the challenge requires an LLM). It is used:
  * as the stub interpreter for local development when no LLM key is set, and
  * as a last-resort safety net if every configured LLM call fails.
It emits the same raw format as the LLM, so the same guardrails validate it.
"""
from __future__ import annotations

import re
from typing import Optional

WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
            "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
FRACTIONS = [
    (r"three[- ]quarters?", 0.75), (r"two[- ]thirds?", 2 / 3), (r"one[- ]third|a third", 1 / 3),
    (r"one[- ]fifth|a fifth", 0.2), (r"one[- ]quarter|a quarter|one[- ]fourth", 0.25),
    (r"one[- ]tenth|a tenth", 0.1), (r"\bhalf\b", 0.5),
]
OTHER_PERIOD = re.compile(r"\b(next|last|previous|coming)\s+(week|month|year|semester|term|weekend|monday|tuesday|"
                          r"wednesday|thursday|friday|saturday|sunday)\b", re.I)

_T = (r"(?:\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?|noon|midday|midnight|"
      r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)(?:\s*o'?clock)?")
RANGE = re.compile(rf"(?:from|between)?\s*({_T})\s*(?:until|till|til|to|through|thru|and|-|–|—)\s*({_T})", re.I)
DURATION = re.compile(rf"for\s+(\d+|{'|'.join(WORD_NUM)})\s+hours?\s+(?:starting|beginning|from)\s+(?:at\s+)?({_T})", re.I)


def _meridiem(tok: str) -> Optional[str]:
    m = re.search(r"([ap])\.?m\.?", tok, re.I)
    return m.group(1).lower() if m else None


def _clock(tok: str, mer: Optional[str], daytime: bool, is_end: bool) -> Optional[int]:
    t = tok.lower().replace("o'clock", "").replace("oclock", "").strip()
    if t in ("noon", "midday"):
        return 12
    if t == "midnight":
        return 24 if is_end else 0
    m = re.match(r"(\d{1,2})(?::(\d{2}))?", t)
    if m:
        h = int(m.group(1))
    else:
        word = re.match(r"[a-z]+", t)
        if not word or word.group(0) not in WORD_NUM:
            return None
        h = WORD_NUM[word.group(0)]
    mer = _meridiem(t) or mer
    if h > 12:  # already 24-hour clock
        return h if h <= 24 else None
    if mer == "p":
        return 12 if h == 12 else h + 12
    if mer == "a":
        return 24 if (h == 12 and is_end) else (0 if h == 12 else h)
    if m and ":" in t:  # 24-hour clock like 09:00
        return h
    # no meridiem: daytime heuristic
    if h == 12:
        return 12
    if daytime:
        return h + 12 if h <= 6 else h
    return h + 12 if h <= 11 else h


def _window(note: str, daytime: bool) -> Optional[tuple[int, int]]:
    d = DURATION.search(note)
    if d:
        n = d.group(1).lower()
        n = int(n) if n.isdigit() else WORD_NUM[n]
        s = _clock(d.group(2), None, daytime, False)
        if s is not None:
            return s, min(24, s + n)
    for m in RANGE.finditer(note):
        a, b = m.group(1), m.group(2)
        if not re.search(r"\d|noon|midday|midnight|" + "|".join(WORD_NUM), a, re.I):
            continue
        mer_b, mer_a = _meridiem(b), _meridiem(a)
        s = _clock(a, mer_a or mer_b, daytime, False)
        e = _clock(b, mer_b or mer_a, daytime, True)
        if s is None or e is None:
            continue
        # "11 to 2" style: end earlier than start without explicit meridiem -> end is PM
        if e <= s and not mer_b and e + 12 <= 24 and e + 12 > s:
            e += 12
        if s != e:
            return s, e
    return None


def _percent(note: str) -> Optional[float]:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent|per cent)", note, re.I)
    return float(m.group(1)) if m else None


def _kwh(note: str) -> Optional[float]:
    m = re.search(r"(\d+(?:\.\d+)?)\s*kwh?\b", note, re.I)
    return float(m.group(1)) if m else None


def parse_note(note: str, index: int) -> dict:
    n = note.lower()
    base = {"note_index": index, "start_hour": None, "end_hour": None, "hours": None,
            "value": None, "value_kind": None}
    noop = {**base, "directive_type": "no_op", "explanation": "Note does not change today's energy schedule."}
    if OTHER_PERIOD.search(n):
        return noop

    neg = r"(?:not|no|n't|never|disabled?|unavailable|prohibit\w*|block\w*|suspend\w*|isolat\w*|lock\w*|paus\w*|offline|out of service|halt\w*|stop\w*)"
    solar = re.search(r"solar|\bpv\b|photovoltaic|panel|rooftop array", n)
    discharge_words = r"discharg|(?:energy|power) (?:out of|from) the battery|drain\w* the battery|draw\w* (?:on|from) the battery"
    if re.search(discharge_words, n) and re.search(neg, n):
        dtype = "no_discharge_window"
    elif re.search(r"(?<!dis)charg", n) and re.search(neg, n) and not re.search(r"at least|reserve|keep", n):
        dtype = "no_charge_window"
    elif solar:
        dtype = "solar_reduction"
    elif re.search(r"reserve|at least|no less than|minimum|keep|hold|remain|stored|maintain|back-?up", n) and re.search(r"batter|storage|stored|kwh", n):
        dtype = "minimum_battery_reserve"
    elif re.search(r"grid|import|feeder|transformer|intake|substation|utility|draw", n) and _kwh(n) is not None:
        dtype = "max_grid_window"
    else:
        return noop

    win = _window(note, daytime=dtype == "solar_reduction")
    if not win:
        return noop
    item = {**base, "directive_type": dtype, "start_hour": win[0], "end_hour": win[1],
            "explanation": "Interpreted by rule-based fallback parser."}

    if dtype == "solar_reduction":
        pct = _percent(n)
        if pct is not None:
            reduction = re.search(r"(reduc|cut|drop|fall|lower|decreas|loss|lose|down)\w*\s+(?:of\s+|by\s+)?(?:about\s+|roughly\s+|around\s+|approximately\s+)?\d+(?:\.\d+)?\s*(?:%|percent)", n) \
                or re.search(r"\d+(?:\.\d+)?\s*(?:%|percent)\s+(?:reduction|less|lower|drop|cut|decrease|loss)", n)
            to_form = re.search(r"\b(to|at|only|leav\w*|remain\w*|of (?:the |its |normal |forecast)|about|roughly)\b[^.]*?\d+(?:\.\d+)?\s*(?:%|percent)", n)
            if reduction and not re.search(r"(drop|fall|reduc\w*|cut|lower\w*|down)\s+to\b", n):
                item.update(value=pct, value_kind="percent_reduction")
            elif to_form or not reduction:
                item.update(value=pct, value_kind="percent_remaining")
        else:
            frac = next((v for pat, v in FRACTIONS if re.search(pat, n)), None)
            if frac is not None:
                by_form = re.search(r"(reduc|cut|drop|fall|lower|decreas)\w*\s+by\b", n)
                item.update(value=frac, value_kind="fraction_reduction" if by_form else "fraction_remaining")
            elif re.search(r"unavailable|offline|no solar|zero", n):
                item.update(value=0, value_kind="percent_remaining")
            else:
                return noop
    elif dtype == "minimum_battery_reserve":
        kwh, pct = _kwh(n), _percent(n)
        if kwh is not None:
            item.update(value=kwh, value_kind="kwh")
        elif pct is not None:
            item.update(value=pct, value_kind="percent_of_capacity")
        else:
            frac = next((v for pat, v in FRACTIONS if re.search(pat, n)), None)
            if frac is None:
                return noop
            item.update(value=frac, value_kind="fraction_of_capacity")
    elif dtype == "max_grid_window":
        item.update(value=_kwh(n), value_kind="kwh")
    return item


def parse_notes(notes: list[str]) -> list[dict]:
    return [parse_note(n, i) for i, n in enumerate(notes)]
