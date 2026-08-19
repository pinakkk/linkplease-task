# Single-stage, small, and boring. Python 3.12 is pinned deliberately: the
# concurrency model assumes exactly one process and one event loop.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Dependencies first so code changes don't bust the layer cache.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# --workers 1 is NON-NEGOTIABLE (BLUEPRINT §1, §8.1): the send worker, the
# reconciler and the rate-limit budget are all single-consumer by design.
# A second worker would double the send rate and race the dedup counters.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
