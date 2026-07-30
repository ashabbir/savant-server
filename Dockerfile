# Extend the published server image so local builds do not redownload the
# large ML/CUDA dependency set just to add the Git runtime dependency.
ARG SAVANT_SERVER_BASE_IMAGE=savant-server:base

FROM ${SAVANT_SERVER_BASE_IMAGE}

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Overlay the checked-out server source, including the Git ingestion fix.
# .dockerignore excludes local credentials such as .env.
COPY --chown=savant:savant . /app
RUN python -m pip install --no-cache-dir $(grep -E '^(dulwich|APScheduler)' /app/requirements.txt) \
    && chmod +x /app/docker-entrypoint.sh

USER savant
