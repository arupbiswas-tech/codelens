# syntax=docker/dockerfile:1

# ---- base: Python + uv + project dependencies --------------------------------
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# git is needed from milestone 2 onward (cloning repos to ingest).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Pin the uv version so builds are reproducible.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first so this layer is cached until pyproject/uv.lock change.
# --locked fails the build if uv.lock is missing or out of date with pyproject.toml.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev


# ---- test: adds dev tools and runs lint + tests -------------------------------
FROM base AS test
RUN uv sync --locked
COPY tests ./tests
# The database-backed tests build their schema from this file.
COPY db ./db
# The evaluation files are validated by tests.
COPY eval/questions.json eval/review_cases.json ./eval/
# The GitHub Action script is tested too.
COPY scripts/post_review.py ./scripts/post_review.py
CMD ["sh", "-c", "ruff check . && pytest -q"]


# ---- runtime: small, non-root image that serves the API -----------------------
FROM base AS runtime

# Download the embedding and reranking models while building, so containers start fast and
# work offline. The same names are passed to the app as environment variables, so the image
# and the app cannot disagree about which model is cached.
ARG EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
ARG RERANKER_MODEL=Xenova/ms-marco-MiniLM-L-6-v2
ENV EMBEDDING_MODEL=$EMBEDDING_MODEL \
    RERANKER_MODEL=$RERANKER_MODEL \
    FASTEMBED_CACHE_PATH=/opt/models
COPY scripts/download_models.py /tmp/download_models.py
RUN python /tmp/download_models.py && chmod -R a+rX /opt/models && rm /tmp/download_models.py

RUN useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 8000

# Uses only the standard library so no curl is needed in the image.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=2)" || exit 1

CMD ["uvicorn", "codelens.main:app", "--host", "0.0.0.0", "--port", "8000"]
