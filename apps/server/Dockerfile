# Multi-stage build — keeps final image lean (~400 MB vs ~1.2 GB single-stage)
FROM python:3.11-slim AS builder

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml .

# Install all runtime deps into the system Python (no venv needed in container)
RUN uv pip install --system -e "." --no-cache

# ── Runtime stage ──────────────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application source
COPY . .

# Compile protobuf stubs if proto/ exists (required for gRPC server on :50051)
RUN if [ -f proto/medivoice.proto ]; then \
      mkdir -p proto/generated && \
      python -m grpc_tools.protoc \
        -I proto \
        --python_out=proto/generated \
        --grpc_python_out=proto/generated \
        proto/medivoice.proto; \
    fi

EXPOSE 8000 50051

# Tini reaps zombie processes (important for Pipecat audio threads)
RUN apt-get update && apt-get install -y --no-install-recommends tini && rm -rf /var/lib/apt/lists/*
ENTRYPOINT ["/usr/bin/tini", "--"]

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
