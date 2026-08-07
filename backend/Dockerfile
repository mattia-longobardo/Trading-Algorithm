FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY etoro_bot ./etoro_bot
COPY alembic.ini ./

RUN pip install --no-cache-dir -e ".[api,rag]"

EXPOSE 8000

# Migrazioni all'avvio, poi API. Il kill switch resta azionabile anche a DB giù.
# MAI --workers > 1 (e mai più di una replica): scheduler, lock in-process e
# circuit breaker singleton presuppongono UN SOLO processo — due processi
# significano due scheduler e quindi ordini duplicati. Un secondo processo si
# ritroverebbe comunque senza advisory lock Postgres e senza scheduler.
CMD ["sh", "-c", "alembic upgrade head && uvicorn etoro_bot.api.server:app --host 0.0.0.0 --port 8000"]
