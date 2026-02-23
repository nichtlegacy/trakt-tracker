# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md /app/
COPY src /app/src

RUN pip install --upgrade pip setuptools wheel \
    && pip install .


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    RUNNING_IN_DOCKER="true" \
    STATE_DB_PATH="/data/state.db"

RUN addgroup --system app \
    && adduser --system --ingroup app --home /home/app app \
    && mkdir -p /data /config \
    && chown -R app:app /data /config /home/app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY config.example.toml /app/config.example.toml
COPY entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && chown app:app /app/config.example.toml

USER app
VOLUME ["/data", "/config"]

ENTRYPOINT ["docker-entrypoint.sh"]
