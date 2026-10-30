# HTCO API image: built once in CI, scanned, and deployed by digest (SOFTWARE_DESIGN §13).
# Base images are pinned by digest; Renovate proposes updates after the release-age window.

ARG PYTHON_IMAGE=python:3.14.10-slim-trixie@sha256:3cabfd7d7aaf394e6040e79fa1eec6ca1c52b74ed9188650472695d9a9326999
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0

FROM ${UV_IMAGE} AS uv

# Build: install the locked API package and its dependencies into /app/.venv, non-editable,
# so the runtime stage needs no source tree, no uv and no build tools.
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package htco-api --package htco-worker

# Runtime: the virtual environment only, owned by root and run by an unprivileged user.
FROM ${PYTHON_IMAGE} AS runtime
LABEL org.opencontainers.image.title="htco-api" \
      org.opencontainers.image.description="HTCO OS HTTP API and agent-worker (python -m htco_worker)" \
      org.opencontainers.image.source="https://github.com/Tzvi1234/htco-platform"
RUN groupadd --system --gid 10001 htco \
 && useradd --system --uid 10001 --gid htco --no-create-home --home-dir /nonexistent \
      --shell /usr/sbin/nologin htco
COPY --from=build /app/.venv /app/.venv
# The worker's Action Gateway reads the reviewed registry and the action contracts (slice 5.4).
COPY --from=build /src/registry /app/registry
COPY --from=build /src/packages/contracts/actions /app/packages/contracts/actions
WORKDIR /app
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HTCO_HOST=0.0.0.0 \
    HTCO_PORT=8080
USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "htco_api"]
