FROM node:22-alpine AS monaco
WORKDIR /vendor
RUN npm pack monaco-editor@0.55.1 \
    && tar -xzf monaco-editor-0.55.1.tgz \
    && mkdir -p /monaco \
    && cp -R package/min/vs /monaco/vs \
    && rm -rf package \
    && npm pack monaco-vim@0.4.4 \
    && tar -xzf monaco-vim-0.4.4.tgz \
    && cp package/dist/monaco-vim.umd.js /monaco/monaco-vim.js

FROM python:3.11-slim AS base

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --create-home --uid 1000 appuser

FROM base AS web
COPY . .
COPY --from=monaco /monaco/vs /app/static/vendor/monaco/vs
COPY --from=monaco /monaco/monaco-vim.js /app/static/vendor/monaco-vim.js
USER appuser
EXPOSE 8000

FROM base AS worker
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium chromium-driver \
    && rm -rf /var/lib/apt/lists/*
COPY . .
USER appuser

