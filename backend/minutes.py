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
TIMEOUT_SECONDS = 30.0

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
- actions: each follow-up action, with its owner and due date if one was stated.
- unresolved: any question, item, or concern raised but not settled by the end of the meeting.

If a field has no entries, return an empty list for it. Do not fabricate content.

TRANSCRIPT:
{transcript}
"""


def _empty_minutes() -> dict[str, list]:
    """A fresh dict of empty lists — never a shared instance callers could mutate."""
    return {"attendees": [], "decisions": [], "actions": [], "unresolved": []}


def _resolve_model_tag() -> str:
    """minutes_model_tag -> model_tag (both from .redline_env.json) -> REDLINE_MINUTES_MODEL env var."""
    data: dict[str, Any] = {}
    if ENV_FILE.exists():
        try:
            data = json.loads(ENV_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    tag = data.get("minutes_model_tag") or data.get("model_tag")
    if not tag:
        tag = os.environ.get("REDLINE_MINUTES_MODEL", "")
    return tag or ""


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


def _normalise(result: dict) -> dict:
    """Keep only the four expected keys, each as a list, whatever the model returned."""
    out = _empty_minutes()
    for key in out:
        value = result.get(key)
        if isinstance(value, list):
            out[key] = value
    return out


async def generate_minutes(segments: list[Segment]) -> dict:
    """Extract attendees, decisions, actions, and unresolved items via Ollama.

    Publishes "minutes.ready" with the result and also returns it. On any
    failure — no model configured, empty transcript, Ollama unreachable, a
    timeout, a malformed response — publishes and returns empty lists for
    every field. Never raises into the caller.
    """
    result = _empty_minutes()
    try:
        model = _resolve_model_tag()
        transcript = _format_transcript(segments)
        if model and transcript:
            prompt = _PROMPT_TEMPLATE.format(transcript=transcript)
            loop = asyncio.get_running_loop()
            raw = await loop.run_in_executor(None, _call_ollama_sync, model, prompt)
            result = _normalise(raw)
    except Exception:
        result = _empty_minutes()

    publish("minutes.ready", result)
    return result
