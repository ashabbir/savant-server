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
# Keep the mandatory skill bundle and local ML models in the immutable application layer. Startup
# reconciles skills into the persistent SAVANT_SERVER_DATA_DIR volume.
COPY --chown=savant:savant data/default_skills /app/data/default_skills
COPY --chown=savant:savant models /app/models
ENV EMBEDDING_MODEL_DIR=/app/models/stsb-distilbert-base/v1 \
    RERANKER_MODEL_DIR=/app/models/bge-reranker-base/v1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_OFFLINE=1 \
    SAVANT_OFFLINE_MODELS=1

RUN python -m pip install --no-cache-dir $(grep -E '^(dulwich|APScheduler)' /app/requirements.txt) \
    && chmod +x /app/docker-entrypoint.sh

USER savant
