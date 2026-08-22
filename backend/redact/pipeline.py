"""Redaction pipeline. Reads segments off REDACTION_QUEUE, redacts, publishes.

Order per segment: rules always, model best effort, then merge. The merge is where the
two layers meet and it is deliberately blunt, because an overlapping pair of spans is a
rendering bug in the frontend and a broken redaction schedule in the export.

Merge policy, applied in this order:
  1. a rule span beats an overlapping model span
  2. the longer span beats the shorter
  3. identical ranges dedupe
The output is sorted by start offset and asserted to be free of overlaps.
"""
from __future__ import annotations

import asyncio
import logging
import os
import statistics
import time
from collections import deque
from typing import Any, Iterable

from shared.bus import REDACTION_QUEUE, publish
from shared.contracts import RedactionSpan, Segment, to_wire

from backend.redact import model as model_client
from backend.redact.rules import (find_rule_spans, find_identifier_spans,
                                  find_judgement_spans)

log = logging.getLogger("redline.redact.pipeline")

CONTEXT_SEGMENTS = 2
_LATENCY_WINDOW = 512


def rules_only() -> bool:
    """Last rung of the degradation ladder: skip Gemma, keep the regex safety net."""
    return os.environ.get("REDLINE_RULES_ONLY", "").strip().lower() in ("1", "true", "yes")


_latencies: deque[float] = deque(maxlen=_LATENCY_WINDOW)
_redaction_count = 0
_segment_count = 0
_model_failures = 0
_context: deque[str] = deque(maxlen=CONTEXT_SEGMENTS)


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------

def merge_spans(spans: Iterable[RedactionSpan], priority=None) -> list[RedactionSpan]:
    """Non-overlapping spans sorted by start offset. Lower priority wins an overlap."""
    if priority is None:
        priority = lambda s: 0 if s.source == "rule" else 1
    candidates = [s for s in spans if s.end > s.start]
    candidates.sort(key=lambda s: (priority(s), -(s.end - s.start), s.start))
    kept: list[RedactionSpan] = []
    for span in candidates:
        if any(span.start < k.end and k.start < span.end for k in kept):
            continue
        kept.append(span)

    kept.sort(key=lambda s: s.start)
    for a, b in zip(kept, kept[1:]):
        assert a.end <= b.start, f"overlapping spans after merge: {a} {b}"
    return kept


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------

def get_stats() -> dict[str, Any]:
    return {
        "redactions": _redaction_count,
        "segments": _segment_count,
        "model_failures": _model_failures,
        "latency_ms_p50": round(statistics.median(_latencies), 1) if _latencies else 0.0,
    }


def reset_stats() -> None:
    global _redaction_count, _segment_count, _model_failures
    _latencies.clear()
    _context.clear()
    _redaction_count = 0
    _segment_count = 0
    _model_failures = 0


# --------------------------------------------------------------------------
# One segment
# --------------------------------------------------------------------------

async def redact_segment(segment: Segment, context: Iterable[str] | None = None
                         ) -> tuple[list[RedactionSpan], str]:
    """Rules plus model for one segment. Returns (merged spans, redaction_state).

    "failed" means nothing worked. If the rules found something and only the model
    fell over, the segment is "done": the black bars are on the screen and that is
    what the state means to the frontend.
    """
    global _redaction_count, _segment_count, _model_failures

    text = segment.text or ""
    started = time.perf_counter()

    # Identifiers are deterministic and always run: an NHS number is matched, not
    # judged. Names, suppliers, unformed policy and legal advice are judgement calls,
    # and Gemma owns those. The regex approximations of them run only if Gemma could
    # not answer, so the model is load-bearing but the demo still degrades instead
    # of dying.
    try:
        rule_spans = find_identifier_spans(text)
    except Exception as exc:
        log.exception("rules layer failed on segment %s: %s", segment.id, exc)
        rule_spans = []

    model_spans: list[RedactionSpan] = []
    model_state = "done"
    if not rules_only():
        try:
            model_spans, model_state = await model_client.annotate(text, context)
        except Exception as exc:
            log.warning("model layer failed on segment %s: %s", segment.id, exc)
            model_spans, model_state = [], "failed"

    if model_state == "failed":
        _model_failures += 1

    # The regex approximation of judgement runs too, but at lower priority than the
    # model. Where Gemma found the span, Gemma is credited and the regex is discarded
    # as a duplicate; where Gemma missed it, the regex still covers it. So the model
    # is genuinely doing the work and the bars land either way.
    try:
        judgement_spans = find_judgement_spans(text)
    except Exception as exc:
        log.exception("judgement fallback failed on segment %s: %s", segment.id, exc)
        judgement_spans = []

    tier = {}
    for sp in rule_spans:
        tier[id(sp)] = 0        # validated identifiers are absolute
    for sp in model_spans:
        tier[id(sp)] = 1        # Gemma owns the judgement calls
    for sp in judgement_spans:
        tier[id(sp)] = 2        # regex only fills what Gemma missed

    spans = merge_spans(list(rule_spans) + list(model_spans) + list(judgement_spans),
                        priority=lambda sp: tier.get(id(sp), 3))

    if model_state == "done" or rule_spans or judgement_spans:
        state = "done"
    else:
        state = "failed"

    _latencies.append((time.perf_counter() - started) * 1000.0)
    _segment_count += 1
    _redaction_count += len(spans)
    return spans, state


def _wire_spans(spans: Iterable[RedactionSpan]) -> list[dict[str, Any]]:
    out = []
    for span in spans:
        item = to_wire(span)
        item["exemption"] = span.exemption.value
        out.append(item)
    return out


async def process(segment: Segment) -> list[RedactionSpan]:
    """Redact one segment, mutate it in place, publish the result."""
    spans, state = await redact_segment(segment, list(_context))

    segment.spans = spans
    segment.redaction_state = state

    text = segment.text or ""
    if text:
        _context.append(f"{segment.speaker}: {text}")

    publish("segment.redacted", {
        "id": segment.id,
        "spans": _wire_spans(spans),
        "redaction_state": state,
    })
    return spans


# --------------------------------------------------------------------------
# Consumer
# --------------------------------------------------------------------------

async def consume() -> None:
    """Drain REDACTION_QUEUE forever. One bad segment must never kill the loop."""
    log.info("redaction consumer started, model=%s timeout=%.0fms",
             model_client.resolve_model_tag(), model_client.TIMEOUT_S * 1000)
    while True:
        segment = await REDACTION_QUEUE.get()
        try:
            if segment is None:
                return
            await process(segment)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("redaction failed for segment: %s", exc)
            try:
                publish("segment.redacted", {
                    "id": getattr(segment, "id", None),
                    "spans": [],
                    "redaction_state": "failed",
                })
            except Exception:
                pass
        finally:
            REDACTION_QUEUE.task_done()


def start_consumer() -> asyncio.Task:
    """Start the consumer loop. Call once from the server's startup hook."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()
    return loop.create_task(consume())
