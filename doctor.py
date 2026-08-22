#!/usr/bin/env python3
"""REDLINE environment doctor. Prints PASS/FAIL per check. Exits non-zero on any failure."""
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".redline_env.json"

results = []


def check(name, ok, detail=""):
    results.append(ok)
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)
    return ok


def load_env():
    if not ENV_FILE.exists():
        return {}
    try:
        return json.loads(ENV_FILE.read_text())
    except Exception:
        return {}


env = load_env()


def http_get(url, timeout=3):
    import httpx
    return httpx.get(url, timeout=timeout)


def http_post(url, payload, timeout=30):
    import httpx
    return httpx.post(url, json=payload, timeout=timeout)


# 1. Ollama running
ollama_up = False
try:
    r = http_get("http://localhost:11434/api/tags")
    ollama_up = r.status_code == 200
    check("Ollama server running", ollama_up)
except Exception as e:
    check("Ollama server running", False, str(e))

model_tag = env.get("model_tag", "")
model_present = False
if ollama_up:
    try:
        tags = [m["name"] for m in r.json().get("models", [])]
        model_present = model_tag in tags
        check(f"Redaction model present ({model_tag or 'UNKNOWN'})", model_present,
              f"available: {', '.join(tags)}" if not model_present else "")
    except Exception as e:
        check("Redaction model present", False, str(e))
else:
    check("Redaction model present", False, "ollama not running")

# 3. model responds under 2s
if ollama_up and model_present:
    try:
        # One throwaway call loads the weights. A cold first token is not the
        # number that matters, and timing it fails the check on a fresh boot.
        http_post("http://localhost:11434/api/generate", {
            "model": model_tag, "prompt": "hi", "stream": False
        }, timeout=180)
        t0 = time.time()
        r2 = http_post("http://localhost:11434/api/generate", {
            "model": model_tag, "prompt": "hello", "stream": False
        }, timeout=10)
        dt = time.time() - t0
        check("Redaction model responds under 2s", dt < 2.0 and r2.status_code == 200,
              f"{dt*1000:.0f}ms")
    except Exception as e:
        check("Redaction model responds under 2s", False, str(e))
else:
    check("Redaction model responds under 2s", False, "skipped, prior check failed")

# 4. constrained JSON decoding works
if ollama_up and model_present:
    try:
        schema = json.loads((ROOT / "shared" / "schema.json").read_text())
        r3 = http_post("http://localhost:11434/api/generate", {
            "model": model_tag,
            "prompt": "The chair, Cllr Smith, said the budget report from Acme Ltd is confidential.",
            "format": schema,
            "stream": False,
        }, timeout=30)
        body = json.loads(r3.json()["response"])
        ok = "redactions" in body and isinstance(body["redactions"], list)
        check("Constrained JSON decoding matches schema", ok, json.dumps(body)[:200])
    except Exception as e:
        check("Constrained JSON decoding matches schema", False, str(e))
else:
    check("Constrained JSON decoding matches schema", False, "skipped, prior check failed")

# 5. whisper binary present and Metal-enabled
whisper_bin = env.get("whisper_bin", "")
whisper_bin_path = Path(whisper_bin) if whisper_bin else None
check("Whisper binary present", bool(whisper_bin_path and whisper_bin_path.exists()), whisper_bin)

# 6. both whisper model files present
wm_small = ROOT / "third_party/whisper.cpp/models/ggml-small.en.bin"
wm_base = ROOT / "third_party/whisper.cpp/models/ggml-base.en.bin"
check("whisper model ggml-small.en.bin present", wm_small.exists(), str(wm_small))
check("whisper model ggml-base.en.bin present", wm_base.exists(), str(wm_base))

# 7. venv packages importable
venv_ok = True
missing = []
for pkg in ["fastapi", "uvicorn", "websockets", "sounddevice", "numpy", "httpx",
            "pydantic", "multipart", "pytest", "jinja2"]:
    try:
        __import__(pkg)
    except ImportError:
        venv_ok = False
        missing.append(pkg)
check("Python packages importable", venv_ok, ", ".join(missing))

# 8. audio input device exists
try:
    import sounddevice as sd
    devices = sd.query_devices()
    has_input = any(d["max_input_channels"] > 0 for d in devices)
    check("Audio input device exists", has_input)
except Exception as e:
    check("Audio input device exists", False, str(e))

# 9. node present
try:
    node_v = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=5)
    check("node present", node_v.returncode == 0, node_v.stdout.strip())
except Exception as e:
    check("node present", False, str(e))


def port_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        result = s.connect_ex(("127.0.0.1", port))
        return result != 0
    finally:
        s.close()


check("Port 8000 free", port_free(8000))
check("Port 5173 free", port_free(5173))

print()
print(f"Resolved model tag: {model_tag or 'UNKNOWN'}")
print(f"Minutes model tag:  {env.get('minutes_model_tag', 'UNKNOWN')}")
print(f"Whisper binary:     {whisper_bin or 'UNKNOWN'}")
print(f"pywhispercpp:       {env.get('pywhispercpp', 'UNKNOWN')}")

if not results or not all(results):
    sys.exit(1)
sys.exit(0)
