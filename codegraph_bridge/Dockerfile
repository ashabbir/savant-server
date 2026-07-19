FROM node:22.17.0-bookworm-slim
RUN groupadd --gid 999 savant \
    && useradd --uid 504 --gid 999 --no-create-home --shell /usr/sbin/nologin savant \
    && mkdir -p /run/savant \
    && chown savant:savant /run/savant
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts
COPY src ./src
USER savant
CMD ["node", "src/server.js"]
