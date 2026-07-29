.PHONY: install api agent console test eval reset

VENV ?= .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip
API_PORT ?= 8000

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

reset:
	curl -s -X POST http://127.0.0.1:$(API_PORT)/admin/reset
