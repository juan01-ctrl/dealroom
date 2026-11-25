FROM ghcr.io/astral-sh/uv:0.10.12 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /uvx /usr/local/bin/
WORKDIR /app
ENV PYTHONUNBUFFERED=1 UV_PYTHON_DOWNLOADS=never PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
COPY app app
COPY migrations migrations
COPY evals evals
USER 10001:10001
CMD ["sh", "-c", "exec uvicorn app.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
