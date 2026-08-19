#!/bin/sh
set -e

# Se viene passato un comando specifico, eseguilo direttamente
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

BACKEND_PID=0
FRONTEND_PID=0

cleanup() {
    echo "[entrypoint] Termination signal received. Stopping services..."
    if [ "$FRONTEND_PID" -ne 0 ] && kill -0 "$FRONTEND_PID" 2>/dev/null; then
        echo "[entrypoint] Stopping frontend (PID: $FRONTEND_PID)..."
        kill -TERM "$FRONTEND_PID" 2>/dev/null || true
    fi
    if [ "$BACKEND_PID" -ne 0 ] && kill -0 "$BACKEND_PID" 2>/dev/null; then
        echo "[entrypoint] Stopping backend (PID: $BACKEND_PID)..."
        kill -TERM "$BACKEND_PID" 2>/dev/null || true
    fi
    wait "$FRONTEND_PID" 2>/dev/null || true
    wait "$BACKEND_PID" 2>/dev/null || true
    echo "[entrypoint] All processes terminated."
    exit 0
}

trap cleanup TERM INT QUIT HUP

echo "[entrypoint] Starting unified trading container..."

# 1. Migrazioni database Alembic
if [ -f "/app/backend/alembic.ini" ]; then
    echo "[entrypoint] Running database migrations..."
    (cd /app/backend && alembic upgrade head) || {
        echo "[entrypoint] Warning: Alembic upgrade failed or database not ready yet."
    }
fi

# 2. Avvio Backend FastAPI in background su 127.0.0.1:8000
echo "[entrypoint] Starting FastAPI backend on 127.0.0.1:8000..."
(cd /app/backend && exec uvicorn etoro_bot.api.server:app --host 127.0.0.1 --port 8000) &
BACKEND_PID=$!

sleep 1
if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    echo "[entrypoint] Error: Backend process failed to start."
    exit 1
fi

# 3. Avvio Frontend Next.js in background su 0.0.0.0:3000
echo "[entrypoint] Starting Next.js frontend on 0.0.0.0:3000..."
(cd /app/frontend && exec node server.js) &
FRONTEND_PID=$!

sleep 1
if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
    echo "[entrypoint] Error: Frontend process failed to start."
    kill -TERM "$BACKEND_PID" 2>/dev/null || true
    exit 1
fi

echo "[entrypoint] Services are up and running."

# 4. Monitoraggio processi in background
while true; do
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
        echo "[entrypoint] Backend process exited unexpectedly!"
        cleanup
        exit 1
    fi
    if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
        echo "[entrypoint] Frontend process exited unexpectedly!"
        cleanup
        exit 1
    fi
    sleep 2 &
    wait $!
done
