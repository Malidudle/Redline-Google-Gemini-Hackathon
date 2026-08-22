"""whisper.cpp wrapper with two interchangeable backends.

Backend choice at runtime, in order:
  1. pywhispercpp, if importable (in-process, takes a numpy array, no temp file)
  2. the whisper-cli binary, via subprocess against a temp wav

Whisper always runs in a thread executor. It must never block the event loop.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import re
import sys
import tempfile
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".redline_env.json"
MODELS_DIR = ROOT / "third_party" / "whisper.cpp" / "models"
DEFAULT_MODEL = "ggml-small.en.bin"
FALLBACK_MODEL = "ggml-base.en.bin"
SAMPLE_RATE = 16000


def load_env_facts() -> dict:
    """Resolved environment facts written by the setup agent. Never raises."""
    try:
        return json.loads(ENV_FILE.read_text())
    except Exception:
        return {}


def _normalise_model_name(name: str) -> str:
    if name.endswith(".bin"):
        return name
    if not name.startswith("ggml-"):
        name = "ggml-" + name
    return name + ".bin"


def resolve_model_path(env: Optional[dict] = None) -> Optional[Path]:
    """REDLINE_WHISPER_MODEL > .redline_env.json > ggml-small.en.bin > ggml-base.en.bin."""
    env = load_env_facts() if env is None else env
    candidates: list[str] = []
    for raw in (os.environ.get("REDLINE_WHISPER_MODEL"), env.get("whisper_model")):
        if raw:
            candidates.append(raw)
    candidates.extend([DEFAULT_MODEL, FALLBACK_MODEL])

    for raw in candidates:
        p = Path(raw)
        if p.is_absolute() and p.exists():
            return p
        named = MODELS_DIR / _normalise_model_name(p.name if p.suffix else raw)
        if named.exists():
            return named
    return None


def resolve_whisper_bin(env: Optional[dict] = None) -> Optional[Path]:
    env = load_env_facts() if env is None else env
    for raw in (os.environ.get("REDLINE_WHISPER_BIN"), env.get("whisper_bin")):
        if raw and Path(raw).exists():
            return Path(raw)
    built = ROOT / "third_party" / "whisper.cpp" / "build" / "bin" / "whisper-cli"
    return built if built.exists() else None


def write_wav(path: Path, pcm: np.ndarray, sample_rate: int = SAMPLE_RATE) -> None:
    clipped = np.clip(np.asarray(pcm, dtype=np.float32), -1.0, 1.0)
    pcm16 = (clipped * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16.tobytes())


DOMAIN_PROMPT = (
    "UK local authority committee meeting. Housing enforcement, safeguarding, "
    "procurement and freedom of information. Speakers state names, job titles, "
    "addresses, postcodes, NHS numbers, National Insurance numbers, supplier "
    "names and contract values aloud."
)
PROMPT_CARRY_CHARS = 220



_JUNK_TOKENS = re.compile(r"\[(?:BLANK_AUDIO|SILENCE|MUSIC|NOISE|INAUDIBLE)\]|\((?:silence|music|inaudible)\)|^\s*>>\s*", re.I)


def clean_transcript(text: str) -> str:
    """Strip whisper's non-speech markers. A segment that is only markers is empty,
    and main.py drops empty finals, so [BLANK_AUDIO] never reaches the screen."""
    if not text:
        return ""
    out = _JUNK_TOKENS.sub("", text)
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out


class Transcriber:
    """Thread-confined whisper.cpp. One decode at a time; interims are droppable."""

    def __init__(self, env: Optional[dict] = None, backend: Optional[str] = None) -> None:
        self._last_final = ""
        self.env = load_env_facts() if env is None else env
        self.model_path = resolve_model_path(self.env)
        self.whisper_bin = resolve_whisper_bin(self.env)
        self.backend = backend or self._pick_backend()
        self.threads = int(os.environ.get("REDLINE_WHISPER_THREADS", "6"))

        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="whisper")
        self._decode_lock = threading.Lock()
        self._model = None
        self._model_error: Optional[str] = None
        self.decodes = 0
        self.dropped_interims = 0
        self.last_ms = 0.0

    def _pick_backend(self) -> str:
        forced = os.environ.get("REDLINE_WHISPER_BACKEND")
        if forced:
            return forced
        if self.model_path is None:
            return "none"
        if self.env.get("pywhispercpp") is False:
            return "cli" if self.whisper_bin else "none"
        try:
            import pywhispercpp  # noqa: F401
            return "pywhispercpp"
        except Exception:
            pass
        return "cli" if self.whisper_bin else "none"

    @property
    def available(self) -> bool:
        return self.backend in ("pywhispercpp", "cli") and self.model_path is not None

    def describe(self) -> str:
        return (f"backend={self.backend} model={self.model_path} "
                f"bin={self.whisper_bin} threads={self.threads}")

    # ---- blocking work, runs on the whisper thread ----

    def _ensure_model(self):
        if self._model is not None or self._model_error is not None:
            return self._model
        verbose = os.environ.get("REDLINE_WHISPER_VERBOSE")
        try:
            from pywhispercpp.model import Model
            t0 = time.monotonic()
            model = Model(
                str(self.model_path),
                redirect_whispercpp_logs_to=(sys.stderr if verbose else None),
                print_progress=False,
                print_realtime=False,
                print_timestamps=False,
                n_threads=self.threads,
                language="en",
                no_timestamps=True,
            )
            # A truncated or corrupt ggml file does not raise on load, only on decode.
            model.transcribe(np.zeros(SAMPLE_RATE // 5, dtype=np.float32))
            self._model = model
            print(f"[whisper] loaded {self.model_path.name} in "
                  f"{(time.monotonic() - t0) * 1000:.0f}ms", file=sys.stderr)
        except Exception as exc:
            self._model_error = str(exc)
            print(f"[whisper] pywhispercpp unusable ({exc}); falling back to cli",
                  file=sys.stderr)
            self.backend = "cli" if self.whisper_bin else "none"
        return self._model

    def _prompt(self) -> str:
        """Domain priming plus the tail of the last final segment.

        Whisper conditions on this, so naming the setting and carrying the previous
        sentence keeps proper nouns and spoken digit runs consistent across a
        segment boundary. Measured: "Ltd." became "Limited" for free.
        """
        parts = [DOMAIN_PROMPT]
        if self._last_final:
            parts.append(self._last_final[-PROMPT_CARRY_CHARS:])
        return " ".join(parts)

    def _decode_pywhispercpp(self, pcm: np.ndarray, greedy: bool) -> str:
        model = self._ensure_model()
        if model is None:
            return self._decode_cli(pcm, greedy)
        params = {"n_threads": self.threads, "language": "en", "no_timestamps": True,
                  "initial_prompt": self._prompt()}
        if greedy:
            params["greedy"] = {"best_of": 1}
        try:
            segments = model.transcribe(pcm.astype(np.float32, copy=False), **params)
        except Exception as exc:
            self._model = None
            self._model_error = str(exc)
            self.backend = "cli" if self.whisper_bin else "none"
            print(f"[whisper] decode failed in pywhispercpp ({exc}); switching to cli",
                  file=sys.stderr)
            return self._decode_cli(pcm, greedy)
        parts = []
        for seg in segments:
            text = getattr(seg, "text", None)
            if text is None:
                text = str(seg)
            parts.append(text.strip())
        return " ".join(p for p in parts if p).strip()

    def _decode_cli(self, pcm: np.ndarray, greedy: bool) -> str:
        if not self.whisper_bin or not self.model_path:
            return ""
        with tempfile.TemporaryDirectory(prefix="redline-wav-") as tmp:
            wav = Path(tmp) / "chunk.wav"
            write_wav(wav, pcm)
            cmd = [
                str(self.whisper_bin),
                "-m", str(self.model_path),
                "-f", str(wav),
                "-l", "en",
                "-t", str(self.threads),
                "-nt", "-np",
                "--no-fallback",
                "--prompt", self._prompt(),
            ]
            if greedy:
                cmd += ["-bs", "1", "-bo", "1"]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            except Exception as exc:
                print(f"[whisper] cli failed: {exc}", file=sys.stderr)
                return ""
            if proc.returncode != 0:
                print(f"[whisper] cli rc={proc.returncode}: {proc.stderr[-300:]}",
                      file=sys.stderr)
                return ""
            lines = [ln.strip() for ln in proc.stdout.splitlines()]
        return " ".join(ln for ln in lines if ln and not ln.startswith("[")).strip()

    def _decode(self, pcm: np.ndarray, greedy: bool) -> str:
        t0 = time.monotonic()
        text = self._decode_raw(pcm, greedy)
        return clean_transcript(text)

    def _decode_raw(self, pcm: np.ndarray, greedy: bool) -> str:
        t0 = time.monotonic()
        try:
            if self.backend == "pywhispercpp":
                text = self._decode_pywhispercpp(pcm, greedy)
            elif self.backend == "cli":
                text = self._decode_cli(pcm, greedy)
            else:
                text = ""
        except Exception as exc:
            print(f"[whisper] decode error: {exc}", file=sys.stderr)
            return ""
        self.last_ms = (time.monotonic() - t0) * 1000
        self.decodes += 1
        print(f"[latency] whisper {'interim' if greedy else 'final  '} "
              f"audio={pcm.size / SAMPLE_RATE:5.2f}s decode={self.last_ms:7.1f}ms",
              file=sys.stderr)
        return text

    def _decode_guarded(self, pcm: np.ndarray, greedy: bool, droppable: bool) -> Optional[str]:
        if droppable:
            if not self._decode_lock.acquire(blocking=False):
                self.dropped_interims += 1
                return None
        else:
            self._decode_lock.acquire()
        try:
            return self._decode(pcm, greedy)
        finally:
            self._decode_lock.release()

    # ---- async surface ----

    async def transcribe(self, pcm: np.ndarray, final: bool = True) -> Optional[str]:
        """Decode a buffer off the event loop. Interims return None when whisper is busy."""
        if not self.available or pcm.size == 0:
            return "" if final else None
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(
            self._executor, self._decode_guarded, pcm, not final, not final
        )
        if final and text:
            self._last_final = text
        return text

    async def warmup(self) -> None:
        """Load the model and run one tiny decode so the first utterance is not cold."""
        if not self.available:
            print(f"[whisper] unavailable: {self.describe()}", file=sys.stderr)
            return
        silence = np.zeros(SAMPLE_RATE, dtype=np.float32)
        t0 = time.monotonic()
        await self.transcribe(silence, final=True)
        print(f"[whisper] warm in {(time.monotonic() - t0) * 1000:.0f}ms "
              f"({self.describe()})", file=sys.stderr)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
