.PHONY: install api agent console test eval reset

VENV ?= .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip
# Take the port from RESERVATION_API_URL in .env so the API always starts where
# the agent is looking. Falls back to 8000, which is what the starter package
# and its docker-compose use. Override with `make api API_PORT=9000`.
API_PORT ?= $(or $(shell sed -n 's|^RESERVATION_API_URL=.*://[^:]*:\([0-9][0-9]*\).*|\1|p' .env 2>/dev/null),8000)

install:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	# Fetches the Silero VAD and turn-detector weights so the first call does
	# not pay for the download.
	$(PY) scripts/download_models.py

# The mock reservation API, unmodified from the starter package.
api:
	$(VENV)/bin/uvicorn app:app --host 127.0.0.1 --port $(API_PORT) --app-dir mock_api

# Browser voice call via LiveKit.
agent:
	PYTHONPATH=src $(PY) -m luma.worker dev

# Same agent, terminal microphone, no LiveKit account needed.
console:
	PYTHONPATH=src $(PY) -m luma.worker console

# Guardrail and normalisation tests. No LLM key required.
test:
	$(VENV)/bin/pytest -q

# The seven standard scenarios end to end. Requires an LLM key.
eval:
	$(PY) eval/run_evals.py

# Time the STT and TTS legs against the live providers.
measure:
	$(PY) scripts/measure_speech_latency.py --runs 6

# Read-only ops console on :8100 for the demo. Never writes; safe to run anytime.
ops:
	$(PY) ops/server.py

# Start the demo from a blank slate: fresh API state and an empty event feed.
clean-logs: reset
	rm -f logs/*.jsonl

reset:
	curl -s -X POST http://127.0.0.1:$(API_PORT)/admin/reset

# Places a real call, speaks a synthesised line, and checks the agent heard it,
# called a tool, and replied. Requires `make api` and `make agent`.
smoke:
	$(PY) scripts/smoke_call.py
