# Every target must be listed. `ops`, `eval` and `test` are also directory
# names, and without .PHONY make sees the directory, decides the target is
# already built, and prints "'ops' is up to date" instead of running anything.
.PHONY: install api stop stop-api agent console test eval measure ops reset clean-logs smoke

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
# Says who already holds the port rather than leaving you to decode
# "[Errno 98] address already in use".
api:
	@if curl -sf --max-time 2 http://127.0.0.1:$(API_PORT)/health >/dev/null 2>&1; then \
		echo "A reservation API is already serving :$(API_PORT) — reusing it."; \
		echo "To restart it:  make stop-api && make api"; \
	else \
		$(VENV)/bin/uvicorn app:app --host 127.0.0.1 --port $(API_PORT) --app-dir mock_api; \
	fi

# Stops anything this project started: the API, the ops console, the worker.
stop:
	-@pkill -f "[u]vicorn app:app.*--port $(API_PORT)" 2>/dev/null || true
	-@pkill -f "[o]ps/server.py" 2>/dev/null || true
	-@pkill -f "[l]uma[.]worker" 2>/dev/null || true
	@echo "stopped api, ops and agent (if they were running)"

stop-api:
	-@pkill -f "[u]vicorn app:app.*--port $(API_PORT)" 2>/dev/null || true
	@echo "stopped the reservation API on :$(API_PORT)"

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
