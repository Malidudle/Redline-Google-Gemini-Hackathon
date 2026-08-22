#!/usr/bin/env bash
# REDLINE environment setup. Idempotent: safe to re-run.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

MODEL_TAG="gemma4:e2b"
MINUTES_MODEL_TAG="gemma4:12b"
MODEL_TAG_FALLBACK="gemma3n:e2b"

echo "== REDLINE setup =="

echo "-- Homebrew packages --"
brew list ffmpeg >/dev/null 2>&1 || brew install ffmpeg
brew list portaudio >/dev/null 2>&1 || brew install portaudio

echo "-- Ollama --"
if ! command -v ollama >/dev/null 2>&1; then
  brew install ollama
fi
OLLAMA_VER=$(ollama --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
echo "ollama version: $OLLAMA_VER"

if ! curl -s -o /dev/null http://localhost:11434/api/tags; then
  nohup ollama serve > /tmp/ollama_serve.log 2>&1 &
  for i in $(seq 1 20); do
    curl -s -o /dev/null http://localhost:11434/api/tags && break
    sleep 1
  done
fi

pull_if_missing() {
  local tag="$1"
  if ! ollama list | awk '{print $1}' | grep -qx "$tag"; then
    echo "pulling $tag ..."
    ollama pull "$tag"
  else
    echo "$tag already present"
  fi
}

if ollama pull "$MODEL_TAG" 2>/dev/null; then
  :
else
  echo "$MODEL_TAG unavailable, falling back to $MODEL_TAG_FALLBACK"
  MODEL_TAG="$MODEL_TAG_FALLBACK"
  pull_if_missing "$MODEL_TAG"
fi
pull_if_missing "$MINUTES_MODEL_TAG"

echo "-- warming $MODEL_TAG --"
curl -s http://localhost:11434/api/generate -d "{\"model\":\"$MODEL_TAG\",\"prompt\":\"hello\",\"stream\":false}" >/dev/null

echo "-- whisper.cpp --"
if [ ! -d third_party/whisper.cpp ]; then
  git clone --depth 1 https://github.com/ggerganov/whisper.cpp third_party/whisper.cpp
fi
cd third_party/whisper.cpp
if [ ! -x build/bin/whisper-cli ]; then
  cmake -B build -DGGML_METAL=ON
  cmake --build build -j --config Release
fi
[ -f models/ggml-small.en.bin ] || bash models/download-ggml-model.sh small.en
[ -f models/ggml-base.en.bin ] || bash models/download-ggml-model.sh base.en
cd ../..

echo "-- Python venv --"
PYBIN=""
for cand in python3.11 python3.12 python3.13; do
  if command -v "$cand" >/dev/null 2>&1; then PYBIN="$cand"; break; fi
done
[ -z "$PYBIN" ] && PYBIN=python3

if [ ! -d .venv ]; then
  "$PYBIN" -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q
pip install pywhispercpp -q || echo "pywhispercpp failed to build, subprocess whisper-cli path will be used"
PYWHISPERCPP_OK=false
python -c "import pywhispercpp" 2>/dev/null && PYWHISPERCPP_OK=true

PY_VER=$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')

echo "-- resolving audio device --"
AUDIO_DEVICE=$(python -c "
import sounddevice as sd
try:
    d = sd.query_devices(kind='input')
    print(d['name'])
except Exception:
    print('none')
")

echo "-- writing .redline_env.json --"
WARM_MS=$(python - <<PYEOF
import json, time, urllib.request
payload = {"model": "$MODEL_TAG", "prompt": "hello", "stream": False}
req = urllib.request.Request("http://localhost:11434/api/generate", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
t0 = time.time()
try:
    urllib.request.urlopen(req, timeout=30).read()
except Exception:
    pass
t0b = time.time()
try:
    urllib.request.urlopen(req, timeout=30).read()
except Exception:
    pass
print(int((time.time()-t0b)*1000))
PYEOF
)

JSON_DECODE_OK=false
python - <<PYEOF && JSON_DECODE_OK=true
import json, urllib.request
schema = json.load(open("shared/schema.json"))
payload = {"model": "$MODEL_TAG", "prompt": "The chair discussed a confidential matter.", "format": schema, "stream": False}
req = urllib.request.Request("http://localhost:11434/api/generate", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
body = json.loads(urllib.request.urlopen(req, timeout=30).read())
resp = json.loads(body["response"])
assert "redactions" in resp
PYEOF

WHISPER_RTF=0.05
WHISPER_BIN="$(pwd)/third_party/whisper.cpp/build/bin/whisper-cli"

cat > .redline_env.json <<EOF
{
  "model_tag": "$MODEL_TAG",
  "minutes_model_tag": "$MINUTES_MODEL_TAG",
  "whisper_bin": "$WHISPER_BIN",
  "whisper_model": "$(pwd)/third_party/whisper.cpp/models/ggml-small.en.bin",
  "whisper_model_base": "$(pwd)/third_party/whisper.cpp/models/ggml-base.en.bin",
  "pywhispercpp": $PYWHISPERCPP_OK,
  "json_schema_decoding": $JSON_DECODE_OK,
  "python": "$PY_VER",
  "audio_device": "$AUDIO_DEVICE",
  "gemma_warm_latency_ms": $WARM_MS,
  "whisper_rtf": $WHISPER_RTF
}
EOF

echo "== setup complete =="
echo "run 'make doctor' to verify"
