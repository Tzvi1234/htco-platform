# HTCO API image: built once in CI, scanned, and deployed by digest (SOFTWARE_DESIGN §13).
# Base images are pinned by digest; Renovate proposes updates after the release-age window.

ARG PYTHON_IMAGE=python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff

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
