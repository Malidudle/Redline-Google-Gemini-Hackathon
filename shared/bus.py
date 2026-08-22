"""Event bus + redaction queue. FROZEN, shared by backend/main.py and backend/redact/pipeline.py.

This lives in shared/ deliberately: if main.py owned REDACTION_QUEUE, pipeline.py would
have to import main.py while main.py imports pipeline.py. That circular import is the
single most likely integration failure, so the queue is hoisted out of both.
"""
import asyncio, json
from typing import Any

REDACTION_QUEUE: "asyncio.Queue[Any]" = asyncio.Queue(maxsize=32)
_subscribers: set["asyncio.Queue[str]"] = set()


def subscribe() -> "asyncio.Queue[str]":
    q: "asyncio.Queue[str]" = asyncio.Queue(maxsize=256)
    _subscribers.add(q)
    return q


def unsubscribe(q: "asyncio.Queue[str]") -> None:
    _subscribers.discard(q)


def publish(msg_type: str, payload: Any) -> None:
    """Fan out one envelope to every connected websocket. Never blocks, never raises."""
    frame = json.dumps({"type": msg_type, "payload": payload}, default=str)
    for q in list(_subscribers):
        try:
            q.put_nowait(frame)
        except asyncio.QueueFull:
            try:
                q.get_nowait()
                q.put_nowait(frame)
            except Exception:
                pass


def enqueue_redaction(segment: Any) -> None:
    """Drop-oldest. Transcription must never block on redaction."""
    try:
        REDACTION_QUEUE.put_nowait(segment)
    except asyncio.QueueFull:
        try:
            REDACTION_QUEUE.get_nowait()
            REDACTION_QUEUE.put_nowait(segment)
        except Exception:
            pass
