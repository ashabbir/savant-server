FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV RUNNING_IN_DOCKER=1
ENV SAVANT_API_ONLY=1
ENV SAVANT_SERVER_DATA_DIR=/data/savant
ENV MCP_CONFIG=/app/mcp-config.json
ENV FLASK_HOST=0.0.0.0
ENV FLASK_PORT=8090
ENV GUNICORN_WORKERS=2
ENV GUNICORN_THREADS=4
# Default seed bundle location inside image (full abilities dataset).
# Runtime can override via docker-compose env.
ENV SAVANT_ABILITIES_SEED_DIR=/app/migrations/data
# Ensure Python can find modules within /app (which includes server/)
ENV PYTHONPATH=/app

WORKDIR /app

ARG SAVANT_UID=501
ARG SAVANT_GID=20

RUN groupadd -r -g ${SAVANT_GID} savant 2>/dev/null || \
    groupadd -r savant 2>/dev/null || true && \
    useradd -r -u ${SAVANT_UID} -g savant savant 2>/dev/null || \
    useradd -r -u ${SAVANT_UID} savant 2>/dev/null || true

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY mcp/requirements.txt /app/mcp/requirements.txt
RUN pip install --no-cache-dir -r /app/mcp/requirements.txt

# Copy the entire application code with runtime ownership to avoid an extra
# chown layer over bundled model files.
COPY --chown=savant:savant . /app

# Create and set permissions for data directories, ensuring writable target for abilities
# Ensure /data/savant is created and owned by savant user.
RUN mkdir -p /data/savant && chown -R savant:savant /data/savant

# Create abilities directories — seed data is embedded in bootstrap.py
# and materialized at runtime, so no file copies needed here.
RUN mkdir -p /data/savant/abilities/personas /data/savant/abilities/rules /data/savant/abilities/policies /data/savant/abilities/repos && \
    chown -R savant:savant /data/savant/abilities

# Bundle embedding model into the image so indexing works without downloading
RUN mkdir -p /app/models/stsb-distilbert-base/v1
# Model files are copied by COPY . /app above; set fallback env var
ENV EMBEDDING_MODEL_DIR=/app/models/stsb-distilbert-base/v1

RUN chmod +x /app/docker-entrypoint.sh

USER savant

EXPOSE 8090
EXPOSE 8091
EXPOSE 8092
EXPOSE 8093
EXPOSE 8094

CMD ["/app/docker-entrypoint.sh"]
