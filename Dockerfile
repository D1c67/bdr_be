# Production image for the BDR FastAPI API.
# TODO(owner): pin the base image by digest for full reproducibility, e.g.
#   FROM python:3.12-slim@sha256:<digest>
# Obtain the current digest with:
#   docker buildx imagetools inspect python:3.12-slim
FROM python:3.12-slim

# uv for fast, reproducible installs. Pinned to the 0.11 release line — the
# same major.minor that generated the committed uv.lock — so `uv sync --frozen`
# below can't fail on a lockfile-format mismatch, and builds don't silently pick
# up a new resolver from :latest. A digest pin (@sha256:...) is stronger still —
# obtain it with:  docker buildx imagetools inspect ghcr.io/astral-sh/uv:0.11
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

WORKDIR /app

# Install deps first (layer-cached) strictly from the committed lockfile.
# No glob, no fallback: a missing or stale uv.lock must FAIL the build loudly
# rather than silently resolving unlocked dependencies (supply-chain risk).
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen

COPY app ./app

ENV PORT=3000
EXPOSE 3000

# Honor the platform-provided $PORT (Render/Railway/Fly set this).
# Exec the venv binary directly (not `uv run`) so the dependency resolver can
# never run — or mutate the environment — at container startup.
CMD ["sh", "-c", ".venv/bin/uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
