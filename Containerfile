# ABOUTME: Container image for running the edookit summary gatherer.
# ABOUTME: Python image with beautifulsoup4 and markdown dependencies.

ARG BASE_IMAGE_REF=python:3.13-slim
FROM ${BASE_IMAGE_REF}

ARG VERSION=dev
ARG BUILD_DATE
ARG VCS_REF
ARG BASE_IMAGE_NAME=python:3.13-slim
ARG BASE_IMAGE_DIGEST

LABEL org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.base.name="${BASE_IMAGE_NAME}" \
      org.opencontainers.image.base.digest="${BASE_IMAGE_DIGEST}"

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY edookit.py gather_updates.py ./

# Config and state live on a bind-mounted volume
VOLUME /data

ENTRYPOINT ["python3", "/app/gather_updates.py", "/data/cookies.json"]
