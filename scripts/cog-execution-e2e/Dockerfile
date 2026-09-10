# E2E image for the Cog-execution kind test. Runs the real package under test —
# collab_hub_execution is copied verbatim from its source. Serves as both the Cog
# worker runner (default CMD) and the Op driver (the Job overrides the command).
# Build context is the repo root.
FROM python:3.13-slim
WORKDIR /app
RUN pip install --no-cache-dir "httpx>=0.28.0"
# The real package under test.
COPY execution/src/collab_hub_execution /app/collab_hub_execution
COPY scripts/cog-execution-e2e/runner.py scripts/cog-execution-e2e/driver.py /app/e2e/
ENV PYTHONPATH=/app
CMD ["python", "-m", "e2e.runner"]
