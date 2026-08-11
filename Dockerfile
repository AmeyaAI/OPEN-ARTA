# F8-2: Multi-target Dockerfile.
#   Build the API server (default for compose):
#       docker build --target runtime .
#   Build the GPU-enabled upskill trainer (opt-in, ~10 GB):
#       docker build --target train .
#
# IMPORTANT: docker compose's arta-api service pins `target: runtime`
# explicitly — without that, `docker compose build` would default to the
# LAST stage in this file (train) and try to install torch + unsloth on
# every API rebuild.

FROM python:3.12-slim AS runtime

WORKDIR /app

# C1: Python runtime hygiene for containerized apps
#   PYTHONDONTWRITEBYTECODE=1 — no .pyc bloat in container layer
#   PYTHONUNBUFFERED=1        — stream stdout/stderr immediately (real-time log visibility)
#   PIP_NO_CACHE_DIR=1        — no pip wheel cache in image
#   PIP_DISABLE_PIP_VERSION_CHECK=1 — faster installs, no version-check noise
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONFAULTHANDLER=1

# Playwright browsers stored in shared path (accessible by any user)
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers

# System deps + Node.js 20 + GPG key for k6 repo
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl libpq-dev gcc gnupg2 && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    npm install -g @playwright/test newman axe-playwright && \
    mkdir -p /app/node_modules && \
    ln -s /usr/lib/node_modules/@playwright /app/node_modules/@playwright && \
    ln -s /usr/lib/node_modules/playwright /app/node_modules/playwright && \
    ln -s /usr/lib/node_modules/axe-playwright /app/node_modules/axe-playwright && \
    ln -s /usr/lib/node_modules/axe-core /app/node_modules/axe-core

# Install k6 for performance testing
RUN curl -fsSL https://dl.k6.io/key.gpg | gpg --dearmor -o /usr/share/keyrings/k6-archive-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/k6-archive-keyring.gpg] https://dl.k6.io/deb stable main" > /etc/apt/sources.list.d/k6.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends k6

# Install Chromium browser + system deps (must run before rm apt lists)
RUN npx playwright install --with-deps chromium && \
    chmod -R o+rx /opt/pw-browsers && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY src/ ./src/
COPY .arta/ ./.arta/

# Non-root user for security — UID/GID match host user so mounted volumes are accessible
ARG USER_UID=1001
ARG USER_GID=1001
RUN groupadd -g ${USER_GID} arta 2>/dev/null || true && \
    useradd -m -u ${USER_UID} -g ${USER_GID} arta && \
    chown -R arta:arta /app && \
    mkdir -p /var/arta/artifacts && \
    chown -R arta:arta /var/arta
    
RUN find / -xdev -perm -u=s -type f -exec rm -f {} + 2>/dev/null; \
    rm -f \
        /bin/sh /bin/bash /bin/dash /bin/rbash \
        /usr/bin/apt /usr/bin/apt-get /usr/bin/apt-cache /usr/bin/apt-config \
        /usr/bin/apt-key /usr/bin/apt-mark \
        /usr/bin/dpkg /usr/bin/dpkg-deb /usr/bin/dpkg-query \
        /usr/bin/dpkg-divert /usr/bin/dpkg-trigger /usr/bin/dpkg-split \
        /usr/bin/wget /usr/bin/curl \
        /usr/bin/unzip /usr/bin/xz /usr/bin/tar /usr/bin/gzip /usr/bin/gunzip \
        /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=4)"]

USER arta

EXPOSE 8000

# F3-4: Default CMD is prod-safe (no --reload). The dev compose file overrides
# this with --reload for hot-reload during development. The runtime guard in
# src/api/main.py refuses to start when ENVIRONMENT=production AND --reload is
# present, so a misconfigured prod deploy fails loudly instead of silently
# starting an unsafe server.
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]


# ── F8-2: Upskill training target ────────────────────────────────────────────
# Build:  docker build --target train -t arta-train:latest .
# Run:    via docker-compose.train.yml (handles GPU + volume mounts)
#
# CUDA 12.1 + cuDNN 8 base — required by torch 2.5.1 + Unsloth's flash-attn.
# This stage is intentionally NOT built by `docker compose up` — it's heavy
# (~10GB) and only operators training a new arta-qwen-pro variant need it.
FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04 AS train
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv python3-pip git build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1 \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1

WORKDIR /workspace

# Heavy ML deps first so this layer caches across script edits.
COPY requirements_train.txt ./
RUN pip install --no-cache-dir -r requirements_train.txt

# LiteLLM + dotenv for the dataset-generation half of the pipeline.
RUN pip install --no-cache-dir litellm python-dotenv

# Ship both halves of the pipeline + the teacher adapter package.
COPY scripts/upskill/ ./scripts/upskill/
COPY scripts/upskill_pipeline.py scripts/train_upskill.py ./scripts/

# Volumes mounted by docker-compose.train.yml at runtime:
#   /workspace/data    — generated JSONL dataset
#   /workspace/models  — output QLoRA adapter + merged model
#   /workspace/.env    — GEMINI_API_KEY / ANTHROPIC_API_KEY / OLLAMA_API_BASE
ENV PYTHONPATH=/workspace
ENTRYPOINT ["python3"]
