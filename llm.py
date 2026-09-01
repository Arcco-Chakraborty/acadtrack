"""llm.py — Gemini with round-robin key rotation.

Ported from example-service/example-service's `llm.py` (the same key ring lives in
that app's `.env` as `GEMINI_API_KEYS`). Differences: no config.yaml, settings
come from environment variables, and there is no dependency on PyYAML.

**This is the only place acadtrack talks to an LLM.** It does two things, both
of them only when the owner presses a button:

- `resolve_coverage` — read the owner's own log of what was covered, in his
  words, against the handout's topic list, and say which topics that makes
  done, partly done, or not reached. This is how progress is recorded.
- `estimate_hours` — weight the topics of a handout that gave no hours, so the
  percentage means something for those courses too.

Everything else in the app is arithmetic on what those two return.
"""
from __future__ import annotations

import json
import os
import re
import time

import requests


class LLMError(Exception):
    """Raised when the backend cannot return a parsed JSON object."""


_FENCE = re.compile(r"```(?:json)?\s*|\s*```", re.IGNORECASE)


def parse_json_object(text: str) -> dict:
    """Strip code fences and parse a JSON object, falling back to the outermost
    {...} slice when the model wrapped it in prose."""
    cleaned = _FENCE.sub("", text or "").strip()
    try:
        obj = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError) as e:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if 0 <= start < end:
            try:
                obj = json.loads(cleaned[start:end + 1])
            except (json.JSONDecodeError, ValueError):
                raise LLMError(f"non-JSON model output: {e}") from e
        else:
            raise LLMError(f"non-JSON model output: {e}") from e
    if not isinstance(obj, dict):
        raise LLMError("model output was not a JSON object")
    return obj


def load_keys(var: str = "GEMINI_API_KEYS") -> list[str]:
    return [k.strip() for k in os.environ.get(var, "").split(",") if k.strip()]


class GeminiBackend:
    """generateContent with round-robin key rotation.

    - Asks for application/json output, so valid JSON is the contract.
    - Transient failures (429/5xx/network) rotate to the next key; every key
      gets two tries, the second sweep after a short backoff.
    - Safety blocks and truncation raise immediately with an actionable message
      rather than being retried into the ground.
    """

    TRANSIENT = (429, 500, 502, 503, 504)

    def __init__(self):
        self.model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        self.endpoint = os.environ.get(
            "GEMINI_ENDPOINT",
            "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self.temperature = float(os.environ.get("GEMINI_TEMPERATURE", "0.2"))
        self.max_output_tokens = int(os.environ.get("GEMINI_MAX_TOKENS", "4096"))
        self.timeout = int(os.environ.get("GEMINI_TIMEOUT", "60"))
        self.retry_backoff = float(os.environ.get("GEMINI_BACKOFF", "1.5"))
        self.keys = load_keys()
        self._i = 0

    def _next_key(self) -> str:
        key = self.keys[self._i % len(self.keys)]
        self._i += 1
        return key

    def complete(self, prompt: str) -> dict:
        if not self.keys:
            raise LLMError("no Gemini keys configured (GEMINI_API_KEYS unset)")
        last = "all keys exhausted"
        parse_retries = 1
        attempts = len(self.keys) * 2
        for attempt in range(attempts):
            if attempt == len(self.keys) and self.retry_backoff:
                time.sleep(self.retry_backoff)
            key = self._next_key()
            url = f"{self.endpoint}/models/{self.model}:generateContent?key={key}"
            try:
                resp = requests.post(url, json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "temperature": self.temperature,
                        "maxOutputTokens": self.max_output_tokens,
                        "responseMimeType": "application/json",
                    },
                }, timeout=self.timeout)
            except requests.RequestException as e:
                last = f"request failed: {e}"
                continue
            if resp.status_code in self.TRANSIENT:
                last = f"gemini unavailable ({resp.status_code})"
                continue
            if not resp.ok:
                last = f"gemini error ({resp.status_code})"
                continue
            try:
                data = resp.json()
            except ValueError as e:
                last = f"invalid API response: {e}"
                continue
            block = (data.get("promptFeedback") or {}).get("blockReason")
            if block:
                raise LLMError(f"blocked by Gemini safety ({block})")
            try:
                cand = data["candidates"][0]
                text = cand["content"]["parts"][0]["text"]
            except (KeyError, IndexError, TypeError):
                reason = ""
                if isinstance(data.get("candidates"), list) and data["candidates"]:
                    reason = data["candidates"][0].get("finishReason", "")
                if reason == "MAX_TOKENS":
                    raise LLMError("output hit the token limit before any text "
                                   "— raise GEMINI_MAX_TOKENS")
                last = f"unexpected response shape ({reason or 'no candidates'})"
                continue
            if cand.get("finishReason") == "MAX_TOKENS":
                raise LLMError("output was truncated (MAX_TOKENS) — raise "
                               "GEMINI_MAX_TOKENS")
            try:
                return parse_json_object(text)
            except LLMError as e:
                if parse_retries > 0:
                    parse_retries -= 1
                    last = str(e)
                    continue
                raise
        raise LLMError(last)


def configured() -> bool:
    return bool(load_keys())


COVERAGE_PROMPT = """You are working out how far a university course has \
actually got through its syllabus.

Course: {code} — {title}

The official handout's topic list, in the order the handout prints it:
{topics}

The student's own log of what was covered in class, oldest first:
{notes}

For EVERY topic in the list, decide one of:
  "done"    — the log says this was covered
  "partial" — started or half-covered; the class is in the middle of it
  "pending" — not reached

Rules, in order of authority:
1. The log is the only evidence. The handout supplies the topic names and \
nothing else. Never infer that a topic was covered because of where it sits in \
the handout, or because of any lecture number.
2. Classes run late and instructors reorder the syllabus. Handout order is a \
weak hint about what was probably covered along the way, not a rule. If the \
log says a later topic was taught, do NOT mark the earlier ones done unless \
the log implies they were — leave them pending. A topic being skipped or \
postponed is normal and must be reported as pending.
3. The log names topics loosely, in the lecturer's words or the student's. \
Match them to handout topics by meaning, not by wording.
4. If the log says something was "started", "half done", or "we got up to X in \
it", that topic is partial.
5. If the log says nothing that bears on a topic, it is pending.

Return JSON only, in this exact shape:
{{"topics": [{{"n": 1, "state": "done"}}, {{"n": 2, "state": "partial"}}],
  "summary": "one short sentence on where the course actually is",
  "unmatched": ["anything in the log you could not place in the handout"]}}
where "n" is the 1-based position of the topic in the list above. Every topic \
must appear exactly once."""

STATES = ("done", "partial", "pending")


def _topic_lines(topics) -> str:
    """Number the handout topics for the prompt, with their unit and hours."""
    out = []
    for i, t in enumerate(topics, 1):
        unit = (t["unit"] or "").strip()
        hours = t["hours"] if "hours" in t.keys() else 1
        out.append(f"{i}. " + (f"[{unit}] " if unit else "")
                   + (t["title"] or "").strip()
                   + (f"  ({hours} lecture-hours in the handout)" if hours else ""))
    return "\n".join(out)


def resolve_coverage(code: str, title: str, topics, notes) -> dict:
    """Turn the owner's log into a done/partial/pending state per topic.

    `topics` are the handout's topics in handout order; `notes` are his own
    entries, oldest first, each with `date` and `text`.

    Returns {"states": [...aligned with topics...], "summary": str,
    "unmatched": [str]}. Raises LLMError rather than return a partial answer:
    a states list that is short or keyed to the wrong topics would silently
    mark the wrong half of a syllabus covered.
    """
    if not topics:
        raise LLMError("this course has no syllabus loaded")
    if not notes:
        raise LLMError("nothing in the log yet — write what was covered first")

    log = "\n".join(f"- {n['date']}: {(n['text'] or '').strip()}" for n in notes)
    out = GeminiBackend().complete(COVERAGE_PROMPT.format(
        code=code, title=title or code, topics=_topic_lines(topics), notes=log))

    by_n = {}
    for t in out.get("topics", []):
        try:
            n, state = int(t["n"]), str(t["state"]).strip().lower()
        except (KeyError, TypeError, ValueError):
            continue
        if state in STATES:
            by_n[n] = state
    if set(by_n) != set(range(1, len(topics) + 1)):
        missing = sorted(set(range(1, len(topics) + 1)) - set(by_n))
        raise LLMError(f"model answered for {len(by_n)} of {len(topics)} topics"
                       + (f" (missing {missing[:6]})" if missing else ""))

    unmatched = [str(u) for u in (out.get("unmatched") or [])
                 if isinstance(u, (str, int, float))][:8]
    return {"states": [by_n[i] for i in range(1, len(topics) + 1)],
            "summary": str(out.get("summary", "")).strip()[:300],
            "unmatched": unmatched}


HOURS_PROMPT = """You are given the ordered topic list for a university \
course, taken from its official handout. The handout did NOT say how many \
lecture-hours each topic takes.

Course: {code} — {title}
Total lecture-hours in the course: {total}
Topics, in handout order:
{topics}

Give each topic a whole number of lecture-hours, by how much material it \
plausibly holds. Every topic gets at least 1, and the numbers must add up to \
exactly {total}.

Return JSON only, in this exact shape:
{{"hours": [{{"n": 1, "hours": 4}}, {{"n": 2, "hours": 2}}]}}
where "n" is the 1-based position of the topic in the list above."""


def estimate_hours(code: str, title: str, topics: list[str],
                   total: int) -> list[int]:
    """Spread `total` lecture-hours across `topics`, in handout order.

    This only ever weights the percentage. It is not a claim about when
    anything is taught, and nothing in the app reads it as one.
    """
    if not topics:
        return []
    if total < len(topics):
        raise LLMError(f"{total} hours cannot cover {len(topics)} topics")
    listing = "\n".join(f"{i}. {t}" for i, t in enumerate(topics, 1))
    out = GeminiBackend().complete(HOURS_PROMPT.format(
        code=code, title=title or code, total=total, topics=listing))

    by_n = {}
    for h in out.get("hours", []):
        try:
            by_n[int(h["n"])] = int(h["hours"])
        except (KeyError, TypeError, ValueError):
            continue
    if set(by_n) != set(range(1, len(topics) + 1)):
        raise LLMError(f"model returned hours for topics {sorted(by_n)}, "
                       f"not 1..{len(topics)}")
    hours = [by_n[i] for i in range(1, len(topics) + 1)]
    if any(h < 1 for h in hours):
        raise LLMError("every topic must be worth at least one hour")
    if sum(hours) != total:
        raise LLMError(f"hours add up to {sum(hours)}, not {total}")
    return hours
