# Build : dépendances, puis le projet, installés dans un venv autonome.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable

# Exécution : Python et le venv seulement, utilisateur non root (uid 1000, cf. infra/main.bicep).
FROM python:3.12-slim-bookworm
RUN useradd --uid 1000 --create-home app && mkdir /data && chown app /data
COPY --from=build --chown=app /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" NEWS_FEED_DATA_DIR=/data
USER app
EXPOSE 8000
VOLUME /data
CMD ["news-feed", "serve", "--host", "0.0.0.0", "--port", "8000"]
