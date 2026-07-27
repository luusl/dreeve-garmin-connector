# syntax=docker/dockerfile:1

# Slim, not Alpine, and deliberately: curl_cffi is compiled, and glibc/manylinux wheels are
# guaranteed where musllinux ones are not. On Alpine this risks building a TLS-impersonation
# library from source on every architecture.
ARG PYTHON_IMAGE=python:3.13-slim
# Covers linux/amd64 and linux/arm64, which is what is released. It publishes no linux/arm/v7;
# if that is ever wanted, install uv from PyPI instead — its wheels do cover armv7l.
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.32

# The dev stage is the toolchain container: every make target runs inside it, so uv,
# python, pytest, ruff and mypy never have to exist on the host.
FROM ${PYTHON_IMAGE} AS dev

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /usr/local/bin/

# The venv deliberately lives outside /app: the source is bind mounted over /app and
# would otherwise hide it.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

CMD ["bash"]


FROM ${PYTHON_IMAGE} AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# --no-editable so the package is copied into the venv rather than linked back at /app/src,
# which lets the runtime stage take the venv and nothing else.
RUN uv sync --frozen --no-dev --no-editable


FROM ${PYTHON_IMAGE} AS runtime

# gosu to drop privileges after the entrypoint has fixed ownership; tzdata so TZ means something.
# No openssh-client: this connector writes to a local folder and nowhere else.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GARMINTOKENS=/tokens \
    WATCH_DIR=/watch \
    STATE_DIR=/state \
    HTTP_ADDR=0.0.0.0:8080

RUN mkdir -p /watch /state /tokens

EXPOSE 8080

# The connector answers its own healthcheck, so the image needs no curl.
HEALTHCHECK --interval=1m --timeout=10s --start-period=30s --retries=3 \
    CMD ["/opt/venv/bin/dreeve-garmin-connector", "healthcheck"]

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["run"]
