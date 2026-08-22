"""REDLINE backend: FastAPI app, one websocket, and the capture/replay session loops.

Frames follow the envelope in shared/contracts.py exactly: {"type": ..., "payload": ...}.
Everything reaches the browser through shared.bus.publish, so this module never imports
backend.redact.pipeline at module level and the pipeline never imports this module.
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import math
import os
import statistics
import sys
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from shared.bus import REDACTION_QUEUE, enqueue_redaction, publish, subscribe, unsubscribe
from shared.contracts import Exemption, RedactionSpan, Segment, to_wire

from backend import egress
from backend.transcribe import Transcriber, load_env_facts

ROOT = Path(__file__).resolve().parent.parent
SEED_TRANSCRIPT = ROOT / "demo" / "seed_transcript.json"
OLLAMA_BASE = "http://localhost:11434"
STATS_INTERVAL_S = 2.0
ALLOWED_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]

egress.install_httpx_hook()
ENV_FACTS = load_env_facts()


def log(stage: str, message: str) -> None:
    print(f"[{stage}] {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- session


@dataclass
class SessionState:
    title: str = "Untitled meeting"
    classification: str = "OFFICIAL"
    source: str = "replay"
    speed: float = 1.0
    started_at: float = field(default_factory=time.monotonic)
    tasks: list[asyncio.Task] = field(default_factory=list)
    segments: int = 0
    redactions: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    enqueued_at: dict[str, float] = field(default_factory=dict)
    finals: dict[str, Segment] = field(default_factory=dict)
    running: bool = True

    def note_final(self, seg: Segment) -> None:
        self.segments += 1
        self.finals[seg.id] = seg
        self.enqueued_at[seg.id] = time.monotonic()

    def note_redacted(self, segment_id: str, payload: Optional[dict] = None) -> None:
        """One call per segment.redacted frame. redactions counts spans, not segments."""
        if payload:
            spans = _spans_from_wire(payload.get("spans") or [])
            self.redactions += len(spans)
            seg = self.finals.get(segment_id)
            if seg is not None:
                seg.spans = spans
                seg.redaction_state = payload.get("redaction_state", seg.redaction_state)
        t0 = self.enqueued_at.pop(segment_id, None)
        if t0 is not None:
            ms = (time.monotonic() - t0) * 1000
            self.latencies_ms.append(ms)
            log("latency", f"redaction {segment_id} round trip {ms:7.1f}ms")

    def latency_p50(self) -> float:
        if not self.latencies_ms:
            return 0.0
        return round(statistics.median(self.latencies_ms[-50:]), 1)


SESSION: Optional[SessionState] = None
TRANSCRIBER: Optional[Transcriber] = None
_CONSUMER_TASK: Optional[asyncio.Future] = None
_MINUTES_TASK: Optional[asyncio.Task] = None

# stop_session clears SESSION, but minuting a meeting is something you do once it
# has finished, so the segments of the last session stay reachable.
LAST_SESSION: Optional[SessionState] = None
EMPTY_MINUTES = {"attendees": [], "decisions": [], "actions": [], "unresolved": []}


def _spans_from_wire(raw: list) -> list[RedactionSpan]:
    """Rebuild RedactionSpan objects from the wire form the pipeline published."""
    spans: list[RedactionSpan] = []
    for item in raw:
        try:
            spans.append(RedactionSpan(
                start=int(item["start"]),
                end=int(item["end"]),
                exemption=Exemption(item["exemption"]),
                surface=item.get("surface", ""),
                source=item.get("source", "model"),
                confidence=float(item.get("confidence", 1.0)),
            ))
        except Exception as exc:
            log("export", f"skipping unusable span {item}: {exc}")
    return spans


def _pipeline_stats() -> dict:
    """Counters from the redaction pipeline, if that agent exposed any."""
    try:
        from backend.redact import pipeline
    except Exception:
        return {}
    getter = getattr(pipeline, "get_stats", None)
    if callable(getter):
        try:
            value = getter()
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}
    return {}


# --------------------------------------------------------------------------- emit


def emit_partial(seg: Segment) -> None:
    publish("segment.partial", to_wire(seg))


def emit_final(seg: Segment, session: SessionState) -> None:
    publish("segment.final", to_wire(seg))
    session.note_final(seg)
    enqueue_redaction(seg)
    log("segment", f"final {seg.id} t={seg.t_start:.1f}-{seg.t_end:.1f} "
                   f"queue={REDACTION_QUEUE.qsize()} text={seg.text[:60]!r}")


# --------------------------------------------------------------------------- replay


def load_seed_transcript(path: Path = SEED_TRANSCRIPT) -> list[dict]:
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        log("replay", f"cannot read {path}: {exc}")
        return []
    return data if isinstance(data, list) else []


def _partial_steps(word_count: int, duration: float) -> int:
    """How many interim hypotheses to show inside one utterance."""
    if duration < 2.0 or word_count < 4:
        return 1
    return max(1, min(4, int(duration // 2.5)))


async def replay_loop(session: SessionState) -> None:
    seeds = load_seed_transcript()
    if not seeds:
        log("replay", "seed transcript empty; nothing to replay")
        return
    speed = session.speed if session.speed > 0 else 1.0
    origin = time.monotonic()
    log("replay", f"{len(seeds)} seed segments at speed {speed}x")

    async def wait_until(session_seconds: float) -> None:
        delay = origin + session_seconds / speed - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    for item in seeds:
        seg_id = item.get("id") or uuid.uuid4().hex[:8]
        speaker = item.get("speaker", "Speaker")
        text = item.get("text", "")
        t_start = float(item.get("t_start", 0.0))
        t_end = float(item.get("t_end", t_start))
        duration = max(0.1, t_end - t_start)
        words = text.split()

        await wait_until(t_start)
        steps = _partial_steps(len(words), duration)
        for step in range(1, steps + 1):
            at = t_start + duration * step / (steps + 1)
            await wait_until(at)
            shown = max(1, math.ceil(len(words) * step / (steps + 1)))
            emit_partial(
                Segment(
                    id=seg_id,
                    speaker=speaker,
                    text=" ".join(words[:shown]),
                    t_start=t_start,
                    t_end=at,
                    final=False,
                )
            )

        await wait_until(t_end)
        emit_final(
            Segment(
                id=seg_id,
                speaker=speaker,
                text=text,
                t_start=t_start,
                t_end=t_end,
                final=True,
            ),
            session,
        )

    log("replay", "seed transcript exhausted")


# --------------------------------------------------------------------------- mic


async def mic_loop(session: SessionState) -> None:
    from backend.audio import AudioCapture, AudioUnavailable, default_input_device

    global TRANSCRIBER
    if TRANSCRIBER is None:
        TRANSCRIBER = Transcriber(ENV_FACTS)
    transcriber = TRANSCRIBER

    # The probed picker wins: the device recorded in .redline_env.json was whatever
    # happened to be the system default at install time, which may now be silent.
    candidates = [default_input_device(), ENV_FACTS.get("audio_device")]
    capture = None
    for device in candidates:
        if device is None and capture is not None:
            continue
        try:
            capture = AudioCapture(device=device)
            capture.start()
            break
        except AudioUnavailable as exc:
            log("audio", f"{exc}")
            capture = None
    if capture is None:
        log("audio", "no usable input device; falling back to replay so the demo still runs")
        session.source = "replay"
        await replay_loop(session)
        return

    if not transcriber.available:
        log("whisper", f"no usable backend ({transcriber.describe()}); "
                       "audio will be captured but not transcribed")

    ids: dict[int, str] = {}
    try:
        async for event in capture.events():
            seg_id = ids.get(event.utt)
            if seg_id is None:
                seg_id = uuid.uuid4().hex[:8]
                ids[event.utt] = seg_id
            is_final = event.kind == "final"
            t0 = time.monotonic()
            text = await transcriber.transcribe(event.pcm, final=is_final)
            if text is None:
                continue
            text = text.strip()
            if not text:
                if is_final:
                    ids.pop(event.utt, None)
                continue
            seg = Segment(
                id=seg_id,
                speaker="Speaker",
                text=text,
                t_start=round(event.t_start, 2),
                t_end=round(event.t_end, 2),
                final=is_final,
            )
            if not session.running:
                break
            if is_final:
                emit_final(seg, session)
                ids.pop(event.utt, None)
            else:
                emit_partial(seg)
            log("latency", f"{'final  ' if is_final else 'interim'} {seg_id} "
                           f"capture->publish {(time.monotonic() - t0) * 1000:7.1f}ms")
    finally:
        capture.stop()
        log("audio", f"capture stopped (overflows={capture.overflows})")


# --------------------------------------------------------------------------- monitor + stats


async def monitor_loop(session: SessionState) -> None:
    """Observe the bus so redaction counters work without importing the pipeline."""
    q = subscribe()
    try:
        while True:
            frame = await q.get()
            try:
                msg = json.loads(frame)
            except Exception:
                continue
            if msg.get("type") == "segment.redacted":
                payload = msg.get("payload") or {}
                session.note_redacted(str(payload.get("id", "")), payload)
    except asyncio.CancelledError:
        raise
    finally:
        unsubscribe(q)


async def stats_loop(session: SessionState) -> None:
    """Counters are observed on the bus, so they reset per session and always agree.

    The pipeline's own process-wide counters are exposed on /health instead.
    """
    while True:
        publish("session.stats", {
            "bytes_egress": egress.get_egress_bytes(),
            "segments": session.segments,
            "redactions": session.redactions,
            "latency_ms_p50": session.latency_p50(),
        })
        await asyncio.sleep(STATS_INTERVAL_S)


# --------------------------------------------------------------------------- redaction consumer


async def start_redaction_consumer(session: "SessionState") -> bool:
    """Start the pipeline consumer once per process.

    start_consumer() may return an asyncio.Task, a coroutine, or nothing. Awaiting a
    returned Task would block the session forever, so it is only ever adopted.
    """
    global _CONSUMER_TASK
    if _CONSUMER_TASK is not None and not _CONSUMER_TASK.done():
        log("redact", "consumer already running")
        return True
    try:
        from backend.redact.pipeline import start_consumer
    except ImportError as exc:
        log("redact", f"pipeline not importable yet ({exc}); running without redaction")
        return False
    except Exception as exc:
        log("redact", f"pipeline import failed ({exc}); running without redaction")
        return False
    try:
        result = start_consumer()
    except Exception as exc:
        log("redact", f"start_consumer failed ({exc}); running without redaction")
        return False

    if isinstance(result, asyncio.Future):
        _CONSUMER_TASK = result
        log("redact", "consumer task adopted")
        return True
    if inspect.isawaitable(result):
        task = asyncio.create_task(result)
        done, _ = await asyncio.wait({task}, timeout=2.0)
        if task in done:
            try:
                task.result()
            except Exception as exc:
                log("redact", f"start_consumer raised ({exc}); running without redaction")
                return False
        else:
            _CONSUMER_TASK = task
        log("redact", "consumer started")
        return True
    log("redact", "consumer started")
    return True


# --------------------------------------------------------------------------- warmup


async def warm_ollama() -> None:
    """One dummy generate so the first real redaction is not a cold start."""
    try:
        import httpx
    except Exception as exc:
        log("warm", f"httpx unavailable: {exc}")
        return
    tag = ENV_FACTS.get("model_tag") or os.environ.get("REDLINE_MODEL_TAG")
    try:
        async with httpx.AsyncClient(base_url=OLLAMA_BASE, timeout=180.0) as client:
            if not tag:
                resp = await client.get("/api/tags")
                names = [m.get("name", "") for m in resp.json().get("models", [])]
                tag = next((n for n in names if "gemma" in n.lower()), None) or (
                    names[0] if names else None)
            if not tag:
                log("warm", "no ollama model available to warm")
                return
            t0 = time.monotonic()
            await client.post("/api/generate", json={
                "model": tag,
                "prompt": "ok",
                "stream": False,
                "options": {"num_predict": 1},
            })
            log("warm", f"ollama {tag} loaded in {(time.monotonic() - t0) * 1000:.0f}ms")

        # Loading the weights is not enough. The redaction prompt carries a ~2000 token
        # few-shot prefix that Ollama caches, and until that cache is primed the first
        # real segment pays full prefill and blows the timeout — returning no spans on
        # the demo's opening utterance. Send the real prompt once, here, at boot.
        from backend.redact.model import warm as warm_redactor
        t1 = time.monotonic()
        await warm_redactor()
        log("warm", f"redaction prefix primed in {(time.monotonic() - t1) * 1000:.0f}ms")
    except Exception as exc:
        log("warm", f"ollama warm skipped: {exc}")


async def warm_whisper() -> None:
    global TRANSCRIBER
    try:
        TRANSCRIBER = Transcriber(ENV_FACTS)
        await TRANSCRIBER.warmup()
    except Exception as exc:
        log("warm", f"whisper warm skipped: {exc}")


# --------------------------------------------------------------------------- app


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log("boot", f"env facts: {ENV_FACTS or 'none (.redline_env.json missing)'}")
    log("boot", f"egress hook installed: {egress.is_installed()}")
    asyncio.create_task(warm_whisper())
    asyncio.create_task(warm_ollama())
    yield
    await stop_session()


app = FastAPI(title="REDLINE", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "session": None if SESSION is None else {
            "title": SESSION.title,
            "source": SESSION.source,
            "segments": SESSION.segments,
            "redactions": SESSION.redactions,
        },
        "egress": egress.get_stats(),
        "pipeline": _pipeline_stats(),
        "whisper": TRANSCRIBER.describe() if TRANSCRIBER else "not initialised",
        "env": ENV_FACTS,
    }


async def stop_session() -> None:
    global SESSION, LAST_SESSION
    session, SESSION = SESSION, None
    if session is None:
        return
    LAST_SESSION = session
    session.running = False
    for task in session.tasks:
        task.cancel()
    await asyncio.gather(*session.tasks, return_exceptions=True)
    log("session", f"stopped after {session.segments} segments, "
                   f"{session.redactions} redactions")
    # Minutes are a deliberate step the user takes after recording, like the export.
    # Stopping a recording must not fire the 12B model or open a panel on its own.


def _minutes_segments() -> list[Segment]:
    """Final segments to minute: the live session, or the one that just stopped."""
    session = SESSION if SESSION is not None and SESSION.finals else LAST_SESSION
    if session is None or not session.finals:
        return []
    return sorted(session.finals.values(), key=lambda s: s.t_start)


def _publish_minutes_failure(reason: str) -> None:
    publish("minutes.ready", dict(EMPTY_MINUTES, error=reason[:200]))


def _start_minutes(segments: list[Segment], requested: bool = False) -> None:
    """Fire the minutes model in the background. Never block the socket on it.

    Minutes are the internal record, so Segment.text goes to the model exactly as
    transcribed. Redaction spans describe the FOI release and are not applied here.

    An empty transcript is only reported back when the client asked for minutes.
    Stopping a session that recorded nothing is not a failure worth a frame.
    """
    global _MINUTES_TASK
    if not segments:
        log("minutes", "no session segments to minute")
        if requested:
            _publish_minutes_failure("no transcript to summarise")
        return
    if _MINUTES_TASK is not None and not _MINUTES_TASK.done():
        log("minutes", "generation already in flight; ignoring repeat request")
        return

    publish("minutes.pending", {"segments": len(segments)})

    async def run() -> None:
        t0 = time.monotonic()
        try:
            from backend.minutes import generate_minutes
            await generate_minutes(segments)
            log("latency", f"minutes {len(segments)} segments in "
                           f"{(time.monotonic() - t0) * 1000:.0f}ms")
        except Exception as exc:
            log("minutes", f"minutes failed: {exc}")
            _publish_minutes_failure(f"{type(exc).__name__}: {exc}")

    _MINUTES_TASK = asyncio.create_task(run())


async def start_session(payload: dict) -> SessionState:
    await stop_session()
    global SESSION
    source = (payload.get("source") or "replay").lower()
    if source not in ("mic", "replay"):
        source = "replay"
    speed = float(payload.get("speed") or os.environ.get("REDLINE_REPLAY_SPEED") or 1.0)
    session = SessionState(
        title=payload.get("title") or "Untitled meeting",
        classification=payload.get("classification") or "OFFICIAL",
        source=source,
        speed=speed,
    )
    SESSION = session
    log("session", f"start title={session.title!r} class={session.classification} "
                   f"source={source} speed={speed}")

    await start_redaction_consumer(session)
    session.tasks.append(asyncio.create_task(monitor_loop(session)))
    session.tasks.append(asyncio.create_task(stats_loop(session)))
    producer = mic_loop(session) if source == "mic" else replay_loop(session)
    session.tasks.append(asyncio.create_task(producer))
    return session


async def _handle_client_message(msg: dict) -> None:
    msg_type = msg.get("type")
    payload = msg.get("payload") or {}
    if msg_type == "session.start":
        await start_session(payload)
    elif msg_type == "session.stop":
        await stop_session()
    elif msg_type == "export.request":
        await handle_export(payload)
    elif msg_type == "minutes.request":
        await handle_minutes(payload)
    elif msg_type == "redaction.override":
        await handle_override(payload)
    else:
        log("ws", f"ignoring unknown message type {msg_type!r}")


async def handle_export(payload: dict) -> None:
    """Render the FOI-releasable HTML from the segments this session published."""
    # Record, stop, then export is the natural FOI workflow, so the export must
    # survive session.stop the same way the minutes do.
    session = SESSION if SESSION is not None and SESSION.finals else LAST_SESSION
    if session is None or not session.finals:
        log("export", "no session segments to export")
        return
    try:
        from backend.export import build_export, write_and_open_export
    except Exception as exc:
        log("export", f"export module unavailable ({exc})")
        return
    segments = sorted(session.finals.values(), key=lambda s: s.t_start)
    loop = asyncio.get_running_loop()
    try:
        t0 = time.monotonic()
        html = await loop.run_in_executor(
            None, build_export, segments, session.title, session.classification)
        path = await loop.run_in_executor(
            None, write_and_open_export, html, session.title, True)
        log("latency", f"export {len(segments)} segments in "
                       f"{(time.monotonic() - t0) * 1000:.0f}ms -> {path}")
        publish("export.ready", {"path": str(path)})
    except Exception as exc:
        log("export", f"export failed: {exc}")


async def handle_minutes(_payload: dict) -> None:
    """Minute the internal record. Never blocks the socket; the model runs detached."""
    _start_minutes(_minutes_segments(), requested=True)


async def handle_override(payload: dict) -> None:
    """FOI officer keeps or releases one span. Re-publish so the export follows the UI."""
    session = SESSION
    if session is None:
        log("override", "no active session")
        return
    seg = session.finals.get(payload.get("segment_id"))
    if seg is None:
        log("override", f"unknown segment {payload.get('segment_id')!r}")
        return
    try:
        index = int(payload.get("span_index"))
    except (TypeError, ValueError):
        log("override", f"bad span_index {payload.get('span_index')!r}")
        return
    if not 0 <= index < len(seg.spans):
        log("override", f"span_index {index} out of range for {seg.id}")
        return

    action = (payload.get("action") or "remove").lower()
    if action == "remove":
        dropped = seg.spans.pop(index)
        session.redactions = max(0, session.redactions - 1)
        log("override", f"released {dropped.surface!r} on {seg.id}")
    else:
        log("override", f"kept span {index} on {seg.id}")

    publish("segment.redacted", {
        "id": seg.id,
        "spans": [dict(to_wire(sp), exemption=sp.exemption.value) for sp in seg.spans],
        "redaction_state": seg.redaction_state,
    })


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    q = subscribe()

    async def pump() -> None:
        while True:
            frame = await q.get()
            await ws.send_text(frame)

    pump_task = asyncio.create_task(pump())
    log("ws", "client connected")

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                log("ws", f"bad frame: {raw[:120]!r}")
                continue
            await _handle_client_message(msg)
    except WebSocketDisconnect:
        log("ws", "client disconnected")
    except Exception as exc:
        log("ws", f"socket error: {exc}")
    finally:
        pump_task.cancel()
        unsubscribe(q)


# --------------------------------------------------------------------------- cli


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(prog="backend.main")
    parser.add_argument("--replay", action="store_true",
                        help="start the seed-transcript replay when the first client connects")
    parser.add_argument("--speed", type=float, default=None, help="replay speed factor")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    if args.speed:
        os.environ["REDLINE_REPLAY_SPEED"] = str(args.speed)

    import uvicorn
    uvicorn.run("backend.main:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
