FROM python:3.12-slim-bookworm

LABEL com.azure.containerizationassist.createdby="containerization-assist"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TMPDIR=/tmp \
    STATE_DB=/config/state.sqlite

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --requirement requirements.txt \
    && addgroup --system --gid 10001 app \
    && adduser --system --uid 10001 --gid 10001 --no-create-home app \
    && mkdir --parents /config /exports /tmp \
    && chown --recursive 10001:10001 /config /exports /tmp

COPY src/plaid_firefly.py /app/plaid_firefly.py

# This is a scheduled one-shot worker; liveness is provided by the scheduler.
HEALTHCHECK NONE

USER 10001:10001
ENTRYPOINT ["python", "/app/plaid_firefly.py"]
