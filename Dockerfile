# syntax=docker/dockerfile:1.7

ARG APP_RUNTIME_PROFILE=remote

FROM ghcr.io/astral-sh/uv:0.11.5 AS uv

FROM python:3.12.11-slim-bookworm AS builder
ARG APP_RUNTIME_PROFILE
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$APP_RUNTIME_PROFILE" = "remote" ]; then \
      uv sync --locked --no-dev --no-install-project; \
    elif [ "$APP_RUNTIME_PROFILE" = "local-ml" ]; then \
      uv sync --locked --no-dev --no-install-project --extra local-ml; \
    else \
      echo "unsupported APP_RUNTIME_PROFILE=$APP_RUNTIME_PROFILE" >&2; exit 64; \
    fi

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
COPY configs ./configs
COPY data ./data
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$APP_RUNTIME_PROFILE" = "remote" ]; then \
      uv sync --locked --no-dev --no-editable; \
    elif [ "$APP_RUNTIME_PROFILE" = "local-ml" ]; then \
      uv sync --locked --no-dev --no-editable --extra local-ml; \
    else \
      echo "unsupported APP_RUNTIME_PROFILE=$APP_RUNTIME_PROFILE" >&2; exit 64; \
    fi

FROM python:3.12.11-slim-bookworm AS runtime
ARG APP_RUNTIME_PROFILE
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install --no-install-recommends -y ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --home-dir /home/app --create-home app \
    && case "$APP_RUNTIME_PROFILE" in remote|local-ml) ;; *) exit 64 ;; esac \
    && mkdir -p /app \
    && printf '%s\n' "$APP_RUNTIME_PROFILE" > /app/.runtime-profile

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    HOME=/home/app \
    APP_RUNTIME_PROFILE=${APP_RUNTIME_PROFILE}
LABEL org.polyuquest.runtime-profile=${APP_RUNTIME_PROFILE}
WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src
COPY --from=builder --chown=app:app /app/configs /app/configs
COPY --from=builder --chown=app:app /app/data /app/data

RUN mkdir -p /app/data/runtime /app/data/cache /home/app/.cache \
    && chown -R app:app /app/data/runtime /app/data/cache /home/app/.cache

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health/live', timeout=3)"]
CMD ["agent-rag-serve"]
