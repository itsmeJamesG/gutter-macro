# Gutter Macro Dashboard — Fly.io image.
#
# Two stages so the runtime image carries no build toolchain. uv resolves from the
# committed uv.lock, so a deploy installs exactly what was tested locally.

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first: this layer is cached unless the lock file moves.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project

COPY gutter_macro/ ./gutter_macro/
COPY app/ ./app/
RUN uv sync --frozen


FROM python:3.12-slim AS runtime

# Non-root: the app only ever reads upstream APIs and writes its own volume.
RUN useradd --create-home --uid 10001 gutter

WORKDIR /app
COPY --from=builder --chown=gutter:gutter /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MACRO_DATA_PATH=/data/macro.json

# The volume mounts here; create it so the image also runs without one.
RUN mkdir -p /data && chown gutter:gutter /data

USER gutter
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
