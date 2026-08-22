.PHONY: setup doctor run test demo replay stop

PY := .venv/bin/python

setup:
	./setup.sh

doctor:
	$(PY) doctor.py

test:
	.venv/bin/pytest -q

# Live microphone. Grant Terminal microphone permission first.
run: stop
	@echo "backend :8000 (mic)  frontend :5173"
	@test -d frontend/node_modules || (cd frontend && npm install)
	@$(PY) -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 & \
	 (cd frontend && npm run dev >/dev/null 2>&1) & \
	 sleep 6; open http://localhost:5173/; wait

# The command a presenter types. Live microphone; press Start and talk.
demo: stop
	@echo "REDLINE — backend :8000, frontend :5173, live microphone"
	@test -d frontend/node_modules || (cd frontend && npm install)
	@$(PY) -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 & \
	 (cd frontend && npm run dev >/dev/null 2>&1) & \
	 sleep 6; open http://localhost:5173/; wait

# Fallback only: a dead mic or a room too loud to present in. Replays the seed
# transcript at real speed. Never runs unless it is asked for by name.
replay: stop
	@echo "REDLINE — REPLAY MODE (seed transcript, no microphone)"
	@test -d frontend/node_modules || (cd frontend && npm install)
	@REDLINE_REPLAY=1 $(PY) -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 & \
	 (cd frontend && npm run dev >/dev/null 2>&1) & \
	 sleep 6; open "http://localhost:5173/?replay=1"; wait

stop:
	@pkill -f "[u]vicorn backend.main" 2>/dev/null || true
	@pkill -f "[v]ite" 2>/dev/null || true
	@sleep 1
