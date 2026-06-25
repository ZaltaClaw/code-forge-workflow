# Single image, shared by all five worker deployments. Each Kubernetes
# Deployment / docker-compose service selects which worker to run by setting
# the WORKER_MODULE env var (e.g. orchestrator_worker.worker).
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    HEALTH_PORT=8080 \
    AGENT_MODE=mock

WORKDIR /app

# Install deps first for layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App source.
COPY src ./src
COPY sample-input.json ./sample-input.json

# Run as non-root.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# WORKER_MODULE is required at runtime; default to the orchestrator so the
# image is runnable as-is. Override per deployment.
ENV WORKER_MODULE=orchestrator_worker.worker
ENTRYPOINT ["sh", "-c", "exec python -m \"$WORKER_MODULE\""]
