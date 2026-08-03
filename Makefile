# Every target must be listed. `ops`, `eval` and `test` are also directory
# names, and without .PHONY make sees the directory, decides the target is
# already built, and prints "'ops' is up to date" instead of running anything.
.PHONY: install api stop stop-api agent console test eval measure ops reset clean-logs smoke migrate migrate-down migration migrate-check migrate-sql vendor up down logs ps docker-test

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
	# The browser SDK is gitignored, so a fresh clone has no copy and the
	# console's Start-call button would 404.
	$(PY) scripts/fetch_vendor.py

vendor:
	$(PY) scripts/fetch_vendor.py

# The mock reservation API, unmodified from the starter package.
# Says who already holds the port rather than leaving you to decode
# "[Errno 98] address already in use".
api:
	@if curl -sf --max-time 2 http://127.0.0.1:$(API_PORT)/health >/dev/null 2>&1; then \
		echo "A reservation API is already serving :$(API_PORT) — reusing it."; \
		echo "To restart it:  make stop-api && make api"; \
	else \
		echo ""; \
		echo "  Reservation API — data only, there is NO web page here."; \
		echo "  Opening http://127.0.0.1:$(API_PORT)/ in a browser returns 404, which is correct."; \
		echo "    check it:  curl http://127.0.0.1:$(API_PORT)/health"; \
		echo "    the UI is: http://127.0.0.1:8100   <- run 'make ops' in another terminal"; \
		echo ""; \
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

# --- Docker ----------------------------------------------------------------
# The whole stack -- reservation API, Postgres, Redis, migrations, worker,
# console -- from one command. This is the supported path on Windows, where
# `make`, `sed` and `pkill` do not exist.

# Note this deliberately does not reuse API_PORT above. That one is parsed out
# of RESERVATION_API_URL for the *native* run; inside compose the agent reaches
# the API by service name, and the published port is only for the host.
up:
	docker compose up --build -d
	@echo ""
	@docker compose ps
	@echo ""
	@echo "  console   http://localhost:8100"
	@echo "  metrics   http://localhost:9091/metrics"
	@echo ""
	@echo "  Sign-in password: set OPS_PASSWORD in .env, or read the generated"
	@echo "  one from  make logs  (printed once at startup)."
	@echo ""

down:
	docker compose down

logs:
	docker compose logs -f agent ops

ps:
	docker compose ps

# Runs the suite inside the image that ships, against the containerised API.
docker-test:
	docker compose run --rm tests

# --- Database migrations (production layer) --------------------------------
# Needs DATABASE_URL. Alembic owns the schema on any server database; the app
# only auto-creates tables on SQLite, which is dev and test.
migrate:
	$(VENV)/bin/alembic upgrade head

migrate-down:
	$(VENV)/bin/alembic downgrade -1

# Writes a new migration from the difference between the models and the
# database. Always read it before committing -- autogenerate is a first draft.
migration:
	@test -n "$(m)" || (echo 'Usage: make migration m="what changed"'; exit 1)
	$(VENV)/bin/alembic revision --autogenerate -m "$(m)"

# Fails if the models and the migrations have drifted apart.
migrate-check:
	$(VENV)/bin/alembic check

# Prints the SQL instead of running it, for review or for a DBA.
migrate-sql:
	$(VENV)/bin/alembic upgrade head --sql
