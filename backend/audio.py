"""Microphone capture, ring buffer, and energy VAD.

Produces two kinds of event for the transcriber:
  - "interim": the rolling utterance buffer, emitted about once a second
  - "final":   the finished utterance buffer, emitted at a VAD boundary

Both carry the same utterance index, so the caller can reuse one segment id and let
the UI swap interim text in place.
"""
from __future__ import annotations

import asyncio
import collections
import sys
import threading
import time
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import numpy as np

SAMPLE_RATE = 16000
BLOCK_MS = 100
BLOCK_SAMPLES = SAMPLE_RATE * BLOCK_MS // 1000

INTERIM_EVERY_S = 1.0
SILENCE_HANGOVER_MS = 600
MAX_UTTERANCE_S = 15.0
MIN_UTTERANCE_S = 0.45
PREROLL_BLOCKS = 3
TAIL_KEEP_BLOCKS = 2
RING_SECONDS = 30

ABSOLUTE_FLOOR_RMS = 0.006
SPEECH_FACTOR = 3.0


@dataclass
class AudioEvent:
    kind: str            # "interim" | "final"
    utt: int             # utterance index; interim and final share it
    pcm: np.ndarray      # float32 mono 16kHz
    t_start: float       # seconds from capture start
    t_end: float


class AudioUnavailable(RuntimeError):
    pass


class AudioCapture:
    """Energy-gated capture from the default input device."""

    def __init__(
        self,
        device: Optional[object] = None,
        sample_rate: int = SAMPLE_RATE,
        interim_every: float = INTERIM_EVERY_S,
        silence_hangover_ms: int = SILENCE_HANGOVER_MS,
        max_utterance_s: float = MAX_UTTERANCE_S,
    ) -> None:
        self.device = device
        self.sample_rate = sample_rate
        self.interim_every = interim_every
        self.hangover_blocks = max(1, silence_hangover_ms // BLOCK_MS)
        self.max_utterance_s = max_utterance_s

        self._stream = None
        self._incoming: "collections.deque[np.ndarray]" = collections.deque()
        self._lock = threading.Lock()
        self._overflows = 0
        self._started = False

        self.ring: "collections.deque[np.ndarray]" = collections.deque(
            maxlen=int(RING_SECONDS * 1000 / BLOCK_MS)
        )

        self._noise_floor = ABSOLUTE_FLOOR_RMS
        self._calibrated = 0
        self._blocks_seen = 0

        self._utt = 0
        self._buffer: list[np.ndarray] = []
        self._preroll: "collections.deque[np.ndarray]" = collections.deque(maxlen=PREROLL_BLOCKS)
        self._active = False
        self._silence_run = 0
        self._utt_start_sample = 0
        self._last_interim = 0.0

    # ---- device ----

    def start(self) -> None:
        try:
            import sounddevice as sd
        except Exception as exc:
            raise AudioUnavailable(f"sounddevice not importable: {exc}") from exc

        def callback(indata, frames, time_info, status):
            if status:
                self._overflows += 1
            block = np.asarray(indata, dtype=np.float32).reshape(-1).copy()
            with self._lock:
                self._incoming.append(block)

        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=BLOCK_SAMPLES,
                device=self.device,
                callback=callback,
            )
            self._stream.start()
        except Exception as exc:
            raise AudioUnavailable(f"cannot open input device {self.device!r}: {exc}") from exc
        self._started = True
        print(f"[audio] capture started device={self.device!r} rate={self.sample_rate}",
              file=sys.stderr)

    def stop(self) -> None:
        self._started = False
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    def recent(self, seconds: float) -> np.ndarray:
        """Last N seconds from the ring buffer."""
        want = int(seconds * 1000 / BLOCK_MS)
        blocks = list(self.ring)[-want:]
        return np.concatenate(blocks) if blocks else np.zeros(0, dtype=np.float32)

    @property
    def overflows(self) -> int:
        return self._overflows

    # ---- VAD ----

    def _classify(self, block: np.ndarray) -> bool:
        rms = float(np.sqrt(np.mean(np.square(block)))) if block.size else 0.0
        self._blocks_seen += 1
        if self._calibrated < 10:
            self._noise_floor = max(
                ABSOLUTE_FLOOR_RMS * 0.25,
                (self._noise_floor * self._calibrated + rms) / (self._calibrated + 1),
            )
            self._calibrated += 1
            return False
        threshold = max(self._noise_floor * SPEECH_FACTOR, ABSOLUTE_FLOOR_RMS)
        speech = rms > threshold
        if not speech:
            self._noise_floor = 0.95 * self._noise_floor + 0.05 * rms
        return speech

    def _buffered_samples(self) -> int:
        return sum(b.size for b in self._buffer)

    def _emit_final(self, trim_tail: bool) -> Optional[AudioEvent]:
        blocks = list(self._buffer)
        if trim_tail and self._silence_run > TAIL_KEEP_BLOCKS:
            drop = self._silence_run - TAIL_KEEP_BLOCKS
            blocks = blocks[: len(blocks) - drop] or blocks
        pcm = np.concatenate(blocks) if blocks else np.zeros(0, dtype=np.float32)
        t_start = self._utt_start_sample / self.sample_rate
        event = AudioEvent(
            kind="final",
            utt=self._utt,
            pcm=pcm,
            t_start=t_start,
            t_end=t_start + pcm.size / self.sample_rate,
        )
        self._buffer = []
        self._active = False
        self._silence_run = 0
        self._utt += 1
        if pcm.size / self.sample_rate < MIN_UTTERANCE_S:
            return None
        return event

    def _feed(self, block: np.ndarray, sample_index: int, now: float) -> list[AudioEvent]:
        """Advance the VAD by one block. Returns any events it produced."""
        events: list[AudioEvent] = []
        self.ring.append(block)
        speech = self._classify(block)

        if not self._active:
            if speech:
                pre = list(self._preroll)
                self._buffer = pre + [block]
                self._utt_start_sample = sample_index - sum(b.size for b in pre)
                self._active = True
                self._silence_run = 0
                self._last_interim = now
                self._preroll.clear()
            else:
                self._preroll.append(block)
            return events

        self._buffer.append(block)
        self._silence_run = 0 if speech else self._silence_run + 1

        duration = self._buffered_samples() / self.sample_rate
        boundary = self._silence_run >= self.hangover_blocks
        capped = duration >= self.max_utterance_s

        if boundary or capped:
            event = self._emit_final(trim_tail=boundary)
            if event is not None:
                events.append(event)
            if capped and not boundary:
                # A monologue keeps going: open the next utterance immediately.
                self._buffer = [block]
                self._utt_start_sample = sample_index
                self._active = True
                self._silence_run = 0
                self._last_interim = now
            return events

        if now - self._last_interim >= self.interim_every and duration >= 0.6:
            self._last_interim = now
            t_start = self._utt_start_sample / self.sample_rate
            pcm = np.concatenate(self._buffer)
            events.append(
                AudioEvent(
                    kind="interim",
                    utt=self._utt,
                    pcm=pcm,
                    t_start=t_start,
                    t_end=t_start + pcm.size / self.sample_rate,
                )
            )
        return events

    # ---- async surface ----

    async def events(self) -> AsyncIterator[AudioEvent]:
        """Yield interim and final utterance buffers until stop() is called."""
        if not self._started:
            self.start()
        sample_index = 0
        try:
            while self._started:
                with self._lock:
                    pending = list(self._incoming)
                    self._incoming.clear()
                if not pending:
                    await asyncio.sleep(0.02)
                    continue
                now = time.monotonic()
                for block in pending:
                    for event in self._feed(block, sample_index, now):
                        yield event
                    sample_index += block.size
                await asyncio.sleep(0)
        finally:
            if self._active and self._buffer:
                tail = self._emit_final(trim_tail=False)
                if tail is not None:
                    yield tail
            self.stop()


def default_input_device() -> Optional[object]:
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        for index, dev in enumerate(devices):
            if dev.get("max_input_channels", 0) > 0:
                return index
    except Exception:
        return None
    return None


def audio_available() -> bool:
    return default_input_device() is not None
