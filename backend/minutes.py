"""Post-meeting minutes generation.

On session.stop, the full internal transcript is sent to the larger Gemma
model (the "minutes" model — distinct from the smaller redaction model) via
Ollama, with a JSON schema constraining the response to attendees, decisions,
actions, and unresolved items. The Ollama call runs in a thread executor so
it never blocks the asyncio event loop. Any failure — no model configured,
Ollama unreachable, a timeout, a malformed response — degrades to empty
lists rather than raising into the caller.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx

from shared.bus import publish
from shared.contracts import Segment

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".redline_env.json"
OLLAMA_GENERATE_URL = "http://localhost:11434/api/generate"
OLLAMA_TAGS_URL = "http://localhost:11434/api/tags"
TIMEOUT_SECONDS = 60.0
TAGS_TIMEOUT_SECONDS = 3.0

# The model is asked for a due date it often does not have. Ollama's schema decoding
# fills the gap with a placeholder rather than omitting the key, so these are dropped.
_PLACEHOLDERS = {"", "none", "n/a", "na", "null", "unknown", "unspecified",
                 "not stated", "not specified", "tbd", "tbc", "-"}

_MINUTES_SCHEMA = {
    "type": "object",
    "properties": {
        "attendees": {"type": "array", "items": {"type": "string"}},
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "decided_by": {"type": "string"},
                },
                "required": ["text"],
            },
        },
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "owner": {"type": "string"},
                    "due_date": {"type": "string"},
                },
                "required": ["text"],
            },
        },
        "unresolved": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["attendees", "decisions", "actions", "unresolved"],
}

_PROMPT_TEMPLATE = """You are taking minutes for a UK public body meeting. Read the transcript below and extract exactly what was said. Do not invent names, decisions, or dates that are not present in the transcript.

Return:
- attendees: every speaker who is named or addressed by name in the transcript.
- decisions: each concrete decision made, with its text and who decided it.
- actions: each follow-up task somebody took on or was given. A speaker saying "I'll do X", "we'll do X", or "X needs doing before Y" is an action. The owner is the speaker who committed to it, or the person it was given to. Give a due date only if one was stated.
- unresolved: any question, item, or concern raised but not settled by the end of the meeting.

If a field has no entries, return an empty list for it. Do not fabricate content.

TRANSCRIPT:
{transcript}
"""


def _empty_minutes() -> dict[str, list]:
    """A fresh dict of empty lists — never a shared instance callers could mutate."""
    return {"attendees": [], "decisions": [], "actions": [], "unresolved": []}


def _env_facts() -> dict[str, Any]:
    if not ENV_FILE.exists():
        return {}
    try:
        data = json.loads(ENV_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_model_tag() -> str:
    """minutes_model_tag -> model_tag (both from .redline_env.json) -> REDLINE_MINUTES_MODEL env var."""
    data = _env_facts()
    tag = data.get("minutes_model_tag") or data.get("model_tag")
    if not tag:
        tag = os.environ.get("REDLINE_MINUTES_MODEL", "")
    return tag or ""


def _installed_tags_sync() -> set[str]:
    """Model names Ollama currently has. Empty set if Ollama cannot be reached."""
    try:
        with httpx.Client(timeout=TAGS_TIMEOUT_SECONDS) as client:
            response = client.get(OLLAMA_TAGS_URL)
            response.raise_for_status()
            models = response.json().get("models") or []
    except Exception:
        return set()
    names: set[str] = set()
    for model in models:
        name = str(model.get("name") or "")
        if not name:
            continue
        names.add(name)
        names.add(name.split(":")[0])
    return names


def pick_model_sync() -> str:
    """The minutes model if it is pulled, otherwise the small redaction model.

    A 12B minutes model is optional kit. If it was never pulled the call would
    fail with a 404 and the panel would show nothing, so the smaller model that
    redaction already depends on is the fallback rather than an error.
    """
    data = _env_facts()
    preferred = str(data.get("minutes_model_tag") or "")
    fallback = str(data.get("model_tag") or "")
    installed = _installed_tags_sync()
    if not installed:
        return _resolve_model_tag()
    for tag in (preferred, fallback):
        if tag and tag in installed:
            return tag
    return _resolve_model_tag()


def _format_timestamp(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _format_transcript(segments: list[Segment]) -> str:
    lines = []
    for seg in sorted(segments, key=lambda s: s.t_start):
        text = (seg.text or "").strip()
        if not text:
            continue
        lines.append(f"[{_format_timestamp(seg.t_start)}] {seg.speaker}: {text}")
    return "\n".join(lines)


def _call_ollama_sync(model: str, prompt: str) -> dict:
    """Blocking network call. Only ever run off the event loop, via run_in_executor."""
    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        response = client.post(
            OLLAMA_GENERATE_URL,
            json={
                "model": model,
                "prompt": prompt,
                "format": _MINUTES_SCHEMA,
                "stream": False,
            },
        )
        response.raise_for_status()
        body = response.json()
        return json.loads(body["response"])


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in _PLACEHOLDERS else text


def _entry(item: Any, fields: tuple[str, ...]) -> dict | None:
    """One decision or action as {text, ...fields}. A bare string is a valid entry."""
    if isinstance(item, str):
        text = _clean(item)
        return {"text": text} if text else None
    if not isinstance(item, dict):
        return None
    text = _clean(item.get("text"))
    if not text:
        return None
    entry = {"text": text}
    for field in fields:
        cleaned = _clean(item.get(field))
        if cleaned:
            entry[field] = cleaned
    return entry


def _normalise(result: dict) -> dict:
    """Keep only the four expected keys, each as a list, whatever the model returned."""
    out = _empty_minutes()
    out["attendees"] = [t for t in (_clean(a) for a in result.get("attendees") or []) if t]
    out["unresolved"] = [t for t in (_clean(u) for u in result.get("unresolved") or []) if t]
    for key, fields in (("decisions", ("decided_by",)), ("actions", ("owner", "due_date"))):
        items = result.get(key)
        if not isinstance(items, list):
            continue
        out[key] = [e for e in (_entry(i, fields) for i in items) if e]
    return out


async def generate_minutes(segments: list[Segment]) -> dict:
    """Extract attendees, decisions, actions, and unresolved items via Ollama.

    Publishes "minutes.ready" with the result and also returns it. On any
    failure — no model configured, empty transcript, Ollama unreachable, a
    timeout, a malformed response — publishes and returns empty lists for
    every field, plus an "error" string saying why. Never raises into the caller.
    """
    result = _empty_minutes()
    error = ""
    try:
        loop = asyncio.get_running_loop()
        model = await loop.run_in_executor(None, pick_model_sync)
        transcript = _format_transcript(segments)
        if not model:
            error = "no local model configured"
        elif not transcript:
            error = "no transcript to summarise"
        else:
            prompt = _PROMPT_TEMPLATE.format(transcript=transcript)
            raw = await loop.run_in_executor(None, _call_ollama_sync, model, prompt)
            result = _normalise(raw)
            result["model"] = model
    except Exception as exc:
        result = _empty_minutes()
        error = f"{type(exc).__name__}: {exc}"[:200]
    if error:
        result["error"] = error

    publish("minutes.ready", result)
    return result
