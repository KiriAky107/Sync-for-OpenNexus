FROM node:22-alpine AS console
WORKDIR /console
RUN corepack enable
COPY console/package.json console/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY console ./
RUN pnpm build

FROM python:3.12-slim
ARG OPENNEXUS_SYNC_VERSION=0.5.0
LABEL org.opencontainers.image.title="OpenNexus Server Sync" \
      org.opencontainers.image.version="${OPENNEXUS_SYNC_VERSION}" \
      org.opencontainers.image.source="https://gitea.kronecker.cc/Kronecker/NotesAgentic"
COPY --from=ghcr.io/astral-sh/uv:0.9.24 /uv /bin/uv
WORKDIR /service
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY sync_server ./sync_server
COPY --from=console /sync_server/static ./sync_server/static
RUN useradd --uid 10001 --create-home opennexus && mkdir /staging && chown opennexus /staging
USER 10001
ENV SYNC_STAGING_DIR=/staging
EXPOSE 8080
CMD ["/service/.venv/bin/python", "-m", "sync_server", "serve"]
