# ── Frontend build stage ──────────────────────────────────────────────────────
FROM node:20-slim AS frontend

ARG NPM_CONFIG_REGISTRY

WORKDIR /build

COPY package.json .
RUN npm install

COPY tailwind.config.js tailwind.input.css ./
COPY app/presentation/templates ./app/presentation/templates

RUN mkdir -p dist/css dist/js && \
    npx tailwindcss -i tailwind.input.css -o dist/css/tailwind.css --minify && \
    cp node_modules/htmx.org/dist/htmx.min.js dist/js/htmx.min.js && \
    cp node_modules/htmx.org/dist/ext/sse.js dist/js/htmx-sse.js

# ── Application stage ─────────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONPATH=/app

ARG PIP_INDEX_URL
ARG PIP_TRUSTED_HOST

COPY requirements.txt .
RUN pip install --no-cache-dir \
    ${PIP_INDEX_URL:+--index-url "${PIP_INDEX_URL}"} \
    ${PIP_TRUSTED_HOST:+--trusted-host "${PIP_TRUSTED_HOST}"} \
    -r requirements.txt

COPY --from=frontend /build/dist/css/tailwind.css app/static/css/tailwind.css
COPY --from=frontend /build/dist/js/htmx.min.js  app/static/js/htmx.min.js
COPY --from=frontend /build/dist/js/htmx-sse.js  app/static/js/htmx-sse.js

COPY . .

RUN useradd -m portal
USER portal
