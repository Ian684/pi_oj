FROM node:22-alpine AS monaco
WORKDIR /vendor
RUN npm pack monaco-editor@0.55.1 \
    && tar -xzf monaco-editor-0.55.1.tgz \
    && mkdir -p /monaco \
    && cp -R package/min/vs /monaco/vs \
    && rm -rf package \
    && npm pack monaco-vim@0.4.4 \
    && tar -xzf monaco-vim-0.4.4.tgz \
    && cp package/dist/monaco-vim.umd.js /monaco/monaco-vim.js \
    && rm -rf package \
    && npm pack marked@15.0.12 \
    && tar -xzf marked-15.0.12.tgz \
    && mkdir -p /markdown \
    && cp package/lib/marked.umd.js /markdown/marked.umd.js \
    && rm -rf package \
    && npm pack katex@0.16.22 \
    && tar -xzf katex-0.16.22.tgz \
    && mkdir -p /markdown/katex \
    && cp package/dist/katex.min.css /markdown/katex/katex.min.css \
    && cp package/dist/katex.min.js /markdown/katex/katex.min.js \
    && cp package/dist/contrib/auto-render.min.js /markdown/katex/auto-render.min.js \
    && cp -R package/dist/fonts /markdown/katex/fonts

FROM python:3.11-slim AS base

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --create-home --uid 1000 appuser

FROM base AS web
COPY . .
COPY --from=monaco /monaco/vs /app/static/vendor/monaco/vs
COPY --from=monaco /monaco/monaco-vim.js /app/static/vendor/monaco-vim.js
COPY --from=monaco /markdown/marked.umd.js /app/static/vendor/marked.umd.js
COPY --from=monaco /markdown/katex /app/static/vendor/katex
USER appuser
EXPOSE 8000

FROM base AS worker
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium chromium-driver \
    && rm -rf /var/lib/apt/lists/*
COPY . .
USER appuser

