# Multi-stage Dockerfile unificato per Trading Bot (Backend FastAPI + Frontend Next.js Standalone)

# Stage 1: Dipendenze Frontend
FROM node:22-alpine AS frontend-deps
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

# Stage 2: Build Frontend Next.js
FROM node:22-alpine AS frontend-builder
WORKDIR /app/frontend
COPY --from=frontend-deps /app/frontend/node_modules ./node_modules
COPY frontend/ ./
ENV NEXT_TELEMETRY_DISABLED=1 \
    NODE_ENV=production \
    BACKEND_URL=http://127.0.0.1:8000
RUN npm run build

# Stage 3: Dipendenze Backend Python
FROM python:3.12-slim AS backend-builder
WORKDIR /app/backend
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential gcc && \
    rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY backend/pyproject.toml ./
COPY backend/alembic.ini ./
COPY backend/etoro_bot ./etoro_bot
RUN pip install --no-cache-dir .[api,rag]

# Stage 4: Runtime Unificato (Non-Root 1000:1000)
FROM python:3.12-slim AS runner

# Installazione curl per healthcheck e Node.js runtime da node:22-slim
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*
COPY --from=node:22-slim /usr/local/bin/node /usr/local/bin/node

# Creazione utente non-root (UID 1000)
RUN useradd --create-home --uid 1000 --shell /bin/bash trading

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0 \
    BACKEND_URL=http://127.0.0.1:8000 \
    HOME=/home/trading

# Copia venv Python
COPY --from=backend-builder --chown=1000:1000 /opt/venv /opt/venv

# Copia codice Backend e configurazione alembic
COPY --chown=1000:1000 backend /app/backend

# Copia standalone Frontend
COPY --from=frontend-builder --chown=1000:1000 /app/frontend/public /app/frontend/public
COPY --from=frontend-builder --chown=1000:1000 /app/frontend/.next/standalone /app/frontend/
COPY --from=frontend-builder --chown=1000:1000 /app/frontend/.next/static /app/frontend/.next/static

# Directory per volumi di runtime
RUN mkdir -p /app/config /app/state /app/knowledge_base /app/reports && \
    chown -R 1000:1000 /app/config /app/state /app/knowledge_base /app/reports

# Copia entrypoint
COPY --chown=1000:1000 entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

USER 1000:1000

EXPOSE 3000

ENTRYPOINT ["/app/entrypoint.sh"]
