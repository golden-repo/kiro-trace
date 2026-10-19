# Dockerfile for kiro-trace
# ==========================
#
# Builds a small, production-ready container image for the FastAPI app in
# this project. Works the same whether you run it with plain `docker run`
# or deploy it to Kubernetes as a container image.
#
# Build:
#   docker build -t kiro-trace:latest .
#
# Run (plain Docker, talking to a Redis also reachable from this host):
#   docker run --rm -p 8000:8000 \
#     -e REDIS_HOST=host.docker.internal \
#     -e REDIS_PORT=6379 \
#     -e REDIS_USERNAME=default \
#     -e REDIS_PASSWORD=redis123 \
#     -e API_KEY=some-secret-value \
#     kiro-trace:latest
#
# (Note: "host.docker.internal" is how a container reaches services
# running on your host machine, on Docker Desktop / recent Docker Engine.
# On plain Linux Docker you may instead need --network host, or point
# REDIS_HOST at the actual reachable hostname/IP of your Redis instance.)
#
# Run on Kubernetes: see the example Deployment/Service/Secret manifests
# in README.md -- this image needs no special capabilities and listens
# on a single TCP port (8000 by default, controlled by APP_PORT).

FROM python:3.12-slim AS base

# Avoids Python writing .pyc files and buffers stdout, so `docker logs`
# shows output immediately instead of only after the buffer fills up.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (separate layer) so Docker can cache this
# step and skip re-installing packages when only application code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Now copy the actual application code.
COPY app/ ./app/
COPY static/ ./static/

# Run as a non-root user for defense-in-depth (recommended for any
# container, and often required by Kubernetes Pod Security policies).
RUN useradd --create-home --shell /bin/bash appuser
USER appuser

EXPOSE 8000

# A container-level health check Docker itself can use (separate from,
# and simpler than, the app's own authenticated /api/health endpoint).
# Kubernetes typically defines its own livenessProbe/readinessProbe in
# the Pod spec instead of relying on this, but it's still useful for
# plain `docker run` usage and `docker ps` status output.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
