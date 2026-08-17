"""llm.py — Gemini with round-robin key rotation.

Ported from example-service/example-service's `llm.py` (the same key ring lives in
that app's `.env` as `GEMINI_API_KEYS`). Differences: no config.yaml, settings
come from environment variables, and there is no dependency on PyYAML.

**This is the only place acadtrack talks to an LLM, and it is used for exactly
one thing** — estimating lecture ranges for syllabus topics whose handout gave
none. Everything else in the app is arithmetic on the handout's own numbers.
Nothing here runs unless the owner presses the button.
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


ESTIMATE_PROMPT = """You are given the ordered topic list for a university \
course, taken from its official handout. The handout did NOT state how many \
lectures each topic takes.

Course: {code} — {title}
Total lectures in the course plan: {planned}
Topics, in the order they are taught:
{topics}

Assign each topic a contiguous lecture range. Rules:
- Ranges must be in order and must not overlap.
- The first range starts at lecture 1; the last ends at lecture {planned}.
- Every lecture from 1 to {planned} must be covered by exactly one topic.
- Weight the ranges by how much material each topic plausibly contains.

Return JSON only, in this exact shape:
{{"ranges": [{{"n": 1, "from": 1, "to": 4}}, {{"n": 2, "from": 5, "to": 9}}]}}
where "n" is the 1-based position of the topic in the list above."""


def estimate_lecture_ranges(code: str, title: str, planned: int,
                            topics: list[str]) -> list[tuple[int, int]]:
    """Ask Gemini to spread `planned` lectures across `topics`, in order.

    Returns a list of (from, to) aligned with `topics`. Raises LLMError if the
    model's answer is not a usable, in-order, non-overlapping cover — a bad
    estimate is worse than none, because it silently mis-maps the watermark.
    """
    if not topics:
        return []
    if planned < len(topics):
        raise LLMError(f"{planned} lectures cannot cover {len(topics)} topics")
    listing = "\n".join(f"{i}. {t}" for i, t in enumerate(topics, 1))
    out = GeminiBackend().complete(ESTIMATE_PROMPT.format(
        code=code, title=title or code, planned=planned, topics=listing))

    by_n = {}
    for r in out.get("ranges", []):
        try:
            by_n[int(r["n"])] = (int(r["from"]), int(r["to"]))
        except (KeyError, TypeError, ValueError):
            continue
    if len(by_n) != len(topics):
        raise LLMError(f"model returned {len(by_n)} ranges for "
                       f"{len(topics)} topics")

    ranges, prev_end = [], 0
    for i in range(1, len(topics) + 1):
        lo, hi = by_n[i]
        if lo > hi or lo != prev_end + 1 or hi > planned:
            raise LLMError(f"range {i} ({lo}-{hi}) is not a contiguous, "
                           f"in-order cover of 1..{planned}")
        ranges.append((lo, hi))
        prev_end = hi
    if prev_end != planned:
        raise LLMError(f"ranges stop at {prev_end}, not {planned}")
    return ranges
