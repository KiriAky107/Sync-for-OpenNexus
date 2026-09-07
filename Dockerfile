FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.24 /uv /bin/uv
WORKDIR /service
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY sync_server ./sync_server
RUN useradd --uid 10001 --create-home sync && mkdir /staging && chown sync /staging
USER 10001
ENV SYNC_STAGING_DIR=/staging
CMD ["/service/.venv/bin/python", "-m", "sync_server", "serve"]
