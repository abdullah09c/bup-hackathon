"""Operator-note interpretation: LLM -> guardrails, with retry and fallbacks.

Order for each request:
  1. cache hit (same note texts) -> reuse validated raw interpretation
  2. for each configured LLM endpoint (primary, then fallback):
       call -> guardrails; if some notes fail, one retry with the errors as feedback
  3. notes still unresolved -> rule-based parser (same guardrails)
  4. still unresolved -> no_op (never invent a directive, never crash)
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Optional

from ..config import Settings
from ..guardrails import Directive, GuardrailError, no_op, normalize_all, normalize_item
from . import client, prompt, rule_parser

log = logging.getLogger("gridwise.interpreter")

_CACHE: "OrderedDict[tuple[str, ...], list[dict]]" = OrderedDict()
_CACHE_MAX = 512
_COOLDOWN: dict[str, float] = {}   # endpoint name -> monotonic time until which it is skipped (after 429)
_rr = 0                            # round-robin counter over primary models


def _ordered_endpoints(settings: Settings):
    """Primary models rotated round-robin (spreads per-model rate limits), then fallbacks.
    Endpoints cooling down after a 429 go last rather than being dropped."""
    global _rr
    prim = settings.endpoints[:settings.n_primary] if settings.n_primary else settings.endpoints
    rest = settings.endpoints[len(prim):]
    if prim:
        k = _rr % len(prim)
        _rr += 1
        prim = prim[k:] + prim[:k]
    now = time.monotonic()
    ready = [e for e in prim + rest if _COOLDOWN.get(e.name, 0) <= now]
    cooling = [e for e in prim + rest if _COOLDOWN.get(e.name, 0) > now]
    return ready + cooling


def _cache_get(key):
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]
    return None


def _cache_put(key, raw_items):
    _CACHE[key] = raw_items
    if len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)


def _items(parsed) -> Optional[list]:
    if isinstance(parsed, dict):
        for k in ("interpretations", "directives", "notes", "results"):
            if isinstance(parsed.get(k), list):
                return parsed[k]
    if isinstance(parsed, list):
        return parsed
    return None


async def interpret(notes: list[str], capacity_kwh: float, settings: Settings) -> tuple[list[Directive], str]:
    """Return (one Directive per note, source label)."""
    n = len(notes)
    key = tuple(notes)
    cached = _cache_get(key)
    if cached is not None:
        directives, errors = normalize_all(cached, n, capacity_kwh)
        if not errors:
            return directives, "cache"

    resolved: list[Optional[Directive]] = [None] * n
    raw_ok: list[Optional[dict]] = [None] * n
    sources: list[str] = []
    started = time.monotonic()

    for ep in _ordered_endpoints(settings):
        messages = [{"role": "system", "content": prompt.SYSTEM_PROMPT},
                    {"role": "user", "content": prompt.user_message(notes)}]
        for attempt in range(2):
            remaining = settings.request_budget_s - (time.monotonic() - started)
            if remaining < 2:
                break
            try:
                parsed, text = await client.chat_json(ep, messages, min(settings.timeout_s, remaining))
            except client.LLMError as exc:
                log.warning("LLM %s attempt %d failed: %s", ep.name, attempt + 1, exc)
                if exc.status == 429:
                    _COOLDOWN[ep.name] = time.monotonic() + min(60.0, exc.retry_after or 20.0)
                elif exc.status in (401, 403, 404):  # bad key / access denied / unknown model: not transient
                    _COOLDOWN[ep.name] = time.monotonic() + 300.0
                if attempt == 0 and "JSON" in str(exc):
                    messages.append({"role": "user", "content": "Reply with valid JSON only, following the OUTPUT FORMAT."})
                    continue
                break
            items = _items(parsed)
            directives, errors = normalize_all(items, n, capacity_kwh)
            for i, d in enumerate(directives):
                if d is not None and resolved[i] is None:
                    resolved[i] = d
                    raw_ok[i] = _raw_for(items, i)
            if all(resolved):
                sources.append(ep.name)
                break
            unresolved = {i: errors.get(i, "missing") for i in range(n) if resolved[i] is None}
            log.info("LLM %s: guardrails rejected notes %s; retrying", ep.name, sorted(unresolved))
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": prompt.retry_message(unresolved)}]
        if all(resolved):
            break

    llm_complete = all(resolved)
    if llm_complete and all(raw_ok):
        _cache_put(key, raw_ok)

    if not llm_complete:
        if settings.endpoints:
            log.warning("LLM could not interpret notes %s; using rule-based fallback",
                        [i for i in range(n) if resolved[i] is None])
        for i, item in enumerate(rule_parser.parse_notes(notes)):
            if resolved[i] is None:
                try:
                    resolved[i] = normalize_item(item, i, capacity_kwh)
                except GuardrailError:
                    resolved[i] = no_op(i, "Note could not be interpreted safely; no change applied.")
        sources.append("rule-parser" if settings.endpoints else "stub-rule-parser")

    return [d for d in resolved if d is not None], "+".join(sources)


def _raw_for(items, i):
    for pos, it in enumerate(items or []):
        if isinstance(it, dict) and it.get("note_index", pos) == i:
            return it
    return None
