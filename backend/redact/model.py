"""Local Gemma client. Sends one segment to Ollama and maps the reply back to offsets.

Gemma is the only redactor, so this module
never raises: a timeout, a dead server, a malformed body, or a hallucinated substring
all degrade to fewer spans, never to an exception.

Two things are easy to get wrong and are handled explicitly:

  * The model returns SUBSTRINGS, never offsets. Small models cannot count characters,
    so asking for offsets produces confident nonsense. We ask for exact substrings and
    locate them ourselves, with a three tier fallback ending in a fuzzy match.
  * Constrained JSON decoding is not available on every Ollama build. If the body comes
    back as prose or fenced markdown we parse it defensively instead of failing.
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable

import httpx

from shared.contracts import Exemption, RedactionSpan

log = logging.getLogger("redline.redact.model")

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".redline_env.json"
SCHEMA_PATH = REPO_ROOT / "shared" / "schema.json"

OLLAMA_URL = "http://localhost:11434"
GENERATE_URL = f"{OLLAMA_URL}/api/generate"
DEFAULT_MODEL_TAG = "gemma3:4b"
FUZZY_MIN_RATIO = 0.85
CONTEXT_SEGMENTS = 2

try:
    SCHEMA: dict[str, Any] = json.loads(SCHEMA_PATH.read_text())
except Exception:  # pragma: no cover - schema is frozen and present
    SCHEMA = {}

_EXEMPTION_BY_VALUE = {e.value: e for e in Exemption}


def _env_config() -> dict[str, Any]:
    try:
        if ENV_FILE.exists():
            loaded = json.loads(ENV_FILE.read_text())
            if isinstance(loaded, dict):
                return loaded
    except Exception as exc:
        log.warning("could not read %s: %s", ENV_FILE, exc)
    return {}


def _resolve_timeout() -> float:
    """Spec default is 1500ms. A big model on a slow laptop needs more, so allow an
    override from .redline_env.json ("model_timeout_ms") or REDLINE_MODEL_TIMEOUT_MS."""
    raw = _env_config().get("model_timeout_ms") or os.environ.get("REDLINE_MODEL_TIMEOUT_MS")
    try:
        if raw:
            return max(0.2, float(raw) / 1000.0)
    except Exception:
        pass
    return 1.5


TIMEOUT_S = _resolve_timeout()


# --------------------------------------------------------------------------
# Model tag resolution
# --------------------------------------------------------------------------

_resolved_tag: str | None = None


def _configured_tag() -> str:
    tag = _env_config().get("model_tag")
    if tag:
        return str(tag)
    return os.environ.get("REDLINE_MODEL") or DEFAULT_MODEL_TAG


def _installed_tags(timeout: float = 1.0) -> list[str]:
    """Installed tags, smallest model first. Smaller keeps us inside the 1500ms budget."""
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=timeout)
        models = [m for m in r.json().get("models", []) if m.get("name")]
        models.sort(key=lambda m: m.get("size", 0))
        return [m["name"] for m in models]
    except Exception:
        return []


def resolve_model_tag(refresh: bool = False) -> str:
    """The tag to generate with.

    Config first, then whatever Ollama actually has. The environment agent resolves the
    real tag at setup time, but the demo must not die because a pull is still running.
    """
    global _resolved_tag
    if _resolved_tag is not None and not refresh:
        return _resolved_tag

    want = _configured_tag()
    installed = _installed_tags()
    if not installed or want in installed:
        _resolved_tag = want
        return _resolved_tag

    stem = want.split(":")[0]
    for candidate in installed:
        if candidate.split(":")[0] == stem:
            log.warning("model %s not installed, using %s", want, candidate)
            _resolved_tag = candidate
            return _resolved_tag
    for candidate in installed:
        if "gemma" in candidate.lower():
            log.warning("model %s not installed, falling back to %s", want, candidate)
            _resolved_tag = candidate
            return _resolved_tag

    log.warning("model %s not installed and no gemma present, using %s", want, installed[0])
    _resolved_tag = installed[0]
    return _resolved_tag


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------

_INSTRUCTIONS = """You are a Freedom of Information officer for a UK local authority.
You are reading a live transcript of a council meeting. Your job is to mark the words
that must be withheld from a public release under the Freedom of Information Act 2000.

Mark text that is any of these:
- the name of a person, or a job title attached to a named individual -> s.40(2)
- an identifier belonging to a person: NHS number, National Insurance number, address,
  postcode, date of birth, phone number, email address, bank details -> s.40(2)
- information about a named person's health, care, or safeguarding status -> s.38
- the name of a third party company, supplier, or bidder in a commercial context -> s.43(2)
- a bid value, contract value, tender price, or negotiating position -> s.43(2)
- any reference to legal advice, a solicitor's or counsel's opinion, or privileged
  material -> s.42
- a statement that a policy or decision is still being formed and is not final -> s.35
- information given to the council in confidence -> s.41

Rules you must obey:
1. Copy each redaction as an EXACT SUBSTRING of the CURRENT SEGMENT, character for
   character, including capitals and punctuation inside it. Do NOT paraphrase. Do NOT
   summarise. Do NOT return character offsets or word positions.
2. If the words are not present verbatim in the CURRENT SEGMENT, do not return them.
3. NEVER return text that appears only in the CONTEXT. Context is there to tell you who
   people are, nothing else.
4. Mark the shortest span that carries the sensitive information.
5. If there is nothing to withhold, return {"redactions": []}.
6. Return only "text" and "exemption" for each redaction. Do not add a reason.
7. NEVER withhold these. They are routine meeting furniture, not exempt information:
   - the date the meeting is held, or any ordinary calendar date that is not a person's
     date of birth ("the 22nd of August", "next Tuesday", "the March meeting")
   - agenda item numbers, minute numbers, and reference numbers of the meeting itself
   - the name of the council, the committee, the panel, or the meeting
   - job titles that stand alone and are not attached to a named individual
     ("the committee", "the evaluation panel", "officers")
   - ordinary courtesies and procedural talk ("thanks for joining", "moving to item 4")
"""

_EXAMPLES = """
### Example 1
CONTEXT:
(none)
CURRENT SEGMENT:
Cabinet member Councillor Amara Nwosu confirmed she had reviewed the file personally.
OUTPUT:
{"redactions": [{"text": "Councillor Amara Nwosu", "exemption": "s.40(2)"}]}

### Example 2
CONTEXT:
CLLR HALE: Let's take the procurement item next.
CURRENT SEGMENT:
Two tenders were compliant, and Meridian Facilities Ltd came in lowest at £1.8 million against a budget of £2 million.
OUTPUT:
{"redactions": [{"text": "Meridian Facilities Ltd", "exemption": "s.43(2)"}, {"text": "£1.8 million", "exemption": "s.43(2)"}]}

### Example 3
CONTEXT:
OFFICER: The housing allocations policy came up at the last meeting.
CURRENT SEGMENT:
I would stress the revised allocations policy is still in draft and has not been agreed by cabinet, so nothing here is settled.
OUTPUT:
{"redactions": [{"text": "is still in draft and has not been agreed by cabinet", "exemption": "s.35"}]}

### Example 4
CONTEXT:
CLLR HALE: And on the indemnity point?
CURRENT SEGMENT:
We took legal advice from our external solicitors, and their opinion was that the clause is unenforceable.
OUTPUT:
{"redactions": [{"text": "legal advice from our external solicitors", "exemption": "s.42"}, {"text": "their opinion was that the clause is unenforceable", "exemption": "s.42"}]}

### Example 5
CONTEXT:
DR PATEL: The referral came through on Tuesday.
CURRENT SEGMENT:
The child is currently under a child protection plan and Mrs Okonjo is the allocated social worker.
OUTPUT:
{"redactions": [{"text": "under a child protection plan", "exemption": "s.38"}, {"text": "Mrs Okonjo", "exemption": "s.40(2)"}]}

### Example 6
CONTEXT:
CLLR HALE: Anything else before we close?
CURRENT SEGMENT:
No, chair, that is everything from me. I will circulate the papers this afternoon.
OUTPUT:
{"redactions": []}

### Example 7 (the most common mistake)
CONTEXT:
CLLR HALE: Councillor Amara Nwosu reviewed the file and Meridian Facilities Ltd bid £1.8 million.
CURRENT SEGMENT:
Agreed, chair. I will circulate the papers after the meeting.
OUTPUT:
{"redactions": []}

Example 7 shows the mistake to avoid. "Councillor Amara Nwosu", "Meridian Facilities Ltd"
and "£1.8 million" are in the CONTEXT and not in the CURRENT SEGMENT, so they are NOT
returned. Only words that appear in the CURRENT SEGMENT may be returned.
"""


CONTEXT_CHARS = 200


def build_prompt(segment_text: str, context: Iterable[str] | None = None) -> str:
    lines = [line[-CONTEXT_CHARS:] for line in list(context or [])[-CONTEXT_SEGMENTS:]]
    context_block = "\n".join(lines) if lines else "(none)"
    return (
        f"{_INSTRUCTIONS}\n{_EXAMPLES}\n"
        "### Now do this one\n"
        "CONTEXT (background only, never quote from this):\n"
        f"{context_block}\n"
        "CURRENT SEGMENT (every redaction must be an exact substring of these words):\n"
        f"{segment_text}\n"
        "List every person, identifier, supplier, value, legal advice reference and "
        "unformed policy that appears in the CURRENT SEGMENT above. Copy each one "
        "verbatim from the CURRENT SEGMENT.\n"
        "OUTPUT:\n"
    )


# --------------------------------------------------------------------------
# Defensive JSON parsing
# --------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def parse_model_json(body: str) -> dict[str, Any]:
    """Best effort JSON out of whatever the model produced. Never raises."""
    if not body:
        return {}
    text = _FENCE_RE.sub("", body.strip())
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return {}
    for stop in range(end, start, -1):
        if text[stop] != "}":
            continue
        try:
            parsed = json.loads(text[start:stop + 1])
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue
    return {}


# --------------------------------------------------------------------------
# Substring -> offsets, three tiers
# --------------------------------------------------------------------------

_WORD_RE = re.compile(r"\S+")


def _free(start: int, end: int, taken: list[tuple[int, int]]) -> bool:
    return all(end <= s or start >= e for s, e in taken)


def _find_unclaimed(haystack: str, needle: str, taken: list[tuple[int, int]]) -> int:
    at = haystack.find(needle)
    while at != -1:
        if _free(at, at + len(needle), taken):
            return at
        at = haystack.find(needle, at + 1)
    return -1


def _fuzzy_locate(text: str, target: str, taken: list[tuple[int, int]]) -> tuple[int, int] | None:
    """Word aligned sliding window. Catches the model tidying punctuation or casing."""
    words = [(m.start(), m.end()) for m in _WORD_RE.finditer(text)]
    if not words:
        return None
    want_words = max(1, len(target.split()))
    target_low = target.casefold()
    best: tuple[float, int, int] | None = None
    for i in range(len(words)):
        for n in (want_words - 1, want_words, want_words + 1, want_words + 2):
            if n < 1 or i + n > len(words):
                continue
            start, end = words[i][0], words[i + n - 1][1]
            if not _free(start, end, taken):
                continue
            ratio = difflib.SequenceMatcher(None, text[start:end].casefold(), target_low).ratio()
            if ratio > FUZZY_MIN_RATIO and (best is None or ratio > best[0]):
                best = (ratio, start, end)
    if best is None:
        return None
    return best[1], best[2]


def locate(text: str, target: str, taken: list[tuple[int, int]] | None = None
           ) -> tuple[int, int] | None:
    """Exact, then case insensitive, then fuzzy. None means discard the suggestion."""
    taken = taken if taken is not None else []
    target = target.strip()
    if not target or not text:
        return None

    at = _find_unclaimed(text, target, taken)
    if at != -1:
        return at, at + len(target)

    at = _find_unclaimed(text.casefold(), target.casefold(), taken)
    if at != -1:
        return at, at + len(target)

    return _fuzzy_locate(text, target, taken)


def map_redactions(text: str, redactions: Iterable[dict[str, Any]]) -> list[RedactionSpan]:
    """Model suggestions -> RedactionSpan. Every unmappable suggestion is logged."""
    spans: list[RedactionSpan] = []
    taken: list[tuple[int, int]] = []
    for item in redactions or []:
        if not isinstance(item, dict):
            continue
        surface = item.get("text")
        exemption = _EXEMPTION_BY_VALUE.get(str(item.get("exemption", "")).strip())
        if not isinstance(surface, str) or exemption is None:
            log.warning("model returned unusable redaction: %r", item)
            continue
        found = locate(text, surface, taken)
        if found is None:
            log.warning("model substring not found in segment, discarded: %r", surface)
            continue
        start, end = found
        taken.append((start, end))
        spans.append(RedactionSpan(
            start=start,
            end=end,
            exemption=exemption,
            surface=text[start:end],
            source="model",
            confidence=0.8,
        ))
    spans.sort(key=lambda s: (s.start, -s.end))
    return spans


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------

def _request_body(prompt: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": resolve_model_tag(),
        "prompt": prompt,
        "stream": False,
        "think": False,
        "keep_alive": "30m",
        "options": {"temperature": 0, "top_k": 1, "num_predict": 320},
    }
    if SCHEMA:
        body["format"] = SCHEMA
    return body


async def annotate(text: str, context: Iterable[str] | None = None,
                   timeout: float | None = None) -> tuple[list[RedactionSpan], str]:
    """Ask Gemma about one segment. Returns (spans, "done"|"failed"). Never raises."""
    if not text or not text.strip():
        return [], "done"

    timeout = TIMEOUT_S if timeout is None else timeout
    body = _request_body(build_prompt(text, context))
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(GENERATE_URL, json=body)
        if response.status_code != 200:
            log.warning("ollama returned %s: %s", response.status_code, response.text[:200])
            return [], "failed"
        payload = response.json()
    except httpx.TimeoutException:
        log.warning("model timed out after %.0fms", (time.perf_counter() - started) * 1000)
        return [], "failed"
    except Exception as exc:
        log.warning("model call failed: %s", exc)
        return [], "failed"

    try:
        raw = payload.get("response", "") if isinstance(payload, dict) else ""
        parsed = parse_model_json(raw)
        if not parsed:
            log.warning("model response was not parseable JSON: %r", raw[:200])
            return [], "failed"
        return map_redactions(text, parsed.get("redactions", [])), "done"
    except Exception as exc:
        log.warning("model response handling failed: %s", exc)
        return [], "failed"
