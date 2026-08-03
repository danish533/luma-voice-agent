# syntax=docker/dockerfile:1

# One image, three roles: the agent worker, the ops console, and the migration
# job. They share every dependency and all of the source, so splitting them
# would mean three images to keep in step for the sake of one `command:` line.
# Which role a container plays is decided in docker-compose.yml.

# ---------------------------------------------------------------- builder ---
FROM python:3.12-slim AS builder

# linux/amd64 has wheels for everything in requirements.txt, but arm64 (Apple
# silicon, Windows on ARM) does not for all of it, and pip silently falls back
# to building from source. A compiler here costs nothing in the final image and
# is the difference between "builds on my machine" and "builds on theirs".
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /build
RUN python -m venv /opt/venv

# Dependencies before source. This layer takes minutes; it should rebuild when
# requirements.txt changes, not when someone edits a tool.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Silero VAD and the turn detector run locally, not in the cloud. Fetching them
# at build time rather than on first use is the difference between a container
# that is ready when it reports ready and one that stalls the first caller for
# however long HuggingFace takes -- or fails outright on a network that does not
# allow the egress. HF_HOME must match in the runtime stage or the cache misses
# and it downloads again anyway.
ENV HF_HOME=/opt/models
COPY scripts/download_models.py scripts/download_models.py
RUN python scripts/download_models.py

# Same argument for the browser SDK the ops console loads. It is gitignored, so
# a fresh clone has no copy and the Start-call button would 404 at demo time.
COPY scripts/fetch_vendor.py scripts/fetch_vendor.py
RUN python scripts/fetch_vendor.py

# ---------------------------------------------------------------- runtime ---
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    HF_HOME=/opt/models

# Unprivileged, with a fixed uid so volume ownership is predictable rather than
# whatever the next base-image bump happens to assign.
RUN useradd --create-home --uid 10001 luma

COPY --from=builder /opt/venv /opt/venv
# Owned by the app user, not root: huggingface_hub takes a lock file inside its
# cache even for a warm read, and a read-only cache turns "the weights are
# already here" into a permission error on the first call.
COPY --from=builder --chown=luma:luma /opt/models /opt/models

WORKDIR /app
COPY --chown=luma:luma alembic.ini pytest.ini ./
COPY --chown=luma:luma migrations/ migrations/
COPY --chown=luma:luma src/ src/
COPY --chown=luma:luma ops/ ops/
COPY --chown=luma:luma scripts/ scripts/
COPY --chown=luma:luma eval/ eval/
COPY --chown=luma:luma tests/ tests/
COPY --from=builder --chown=luma:luma /build/ops/vendor/ ops/vendor/

# Created here, not left to the volume mount: a named volume inherits the
# ownership of the directory it covers, so without this it would arrive owned
# by root and the unprivileged worker could not write a single log line.
RUN mkdir -p /app/logs /tmp/prometheus \
    && chown luma:luma /app/logs /tmp/prometheus

USER luma

# The worker is the default role; compose overrides it for the console and the
# migration job.
CMD ["python", "-m", "luma.worker", "start"]
