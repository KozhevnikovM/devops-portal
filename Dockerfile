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

COPY . .
