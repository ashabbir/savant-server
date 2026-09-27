# Extend the published server image so local builds do not redownload the
# large ML/CUDA dependency set just to add the Git runtime dependency.
ARG SAVANT_SERVER_BASE_IMAGE=ashabbir/savant-server:latest

FROM ${SAVANT_SERVER_BASE_IMAGE}

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Overlay the checked-out server source, including the Git ingestion fix.
# .dockerignore excludes local credentials such as .env.
COPY --chown=savant:savant . /app
# Keep the mandatory skill bundle and reranker model in the immutable application layer. Startup
# reconciles skills into the persistent SAVANT_SERVER_DATA_DIR volume.
COPY --chown=savant:savant data/default_skills /app/data/default_skills
COPY --chown=savant:savant models/bge-reranker-base /app/models/bge-reranker-base
RUN python -m pip install --no-cache-dir $(grep -E '^(dulwich|APScheduler)' /app/requirements.txt) \
    && chmod +x /app/docker-entrypoint.sh

USER savant
