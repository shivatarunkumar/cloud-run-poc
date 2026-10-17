FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

RUN useradd --create-home appuser
USER appuser

# Cloud Run sets $PORT. One worker; concurrent requests run in FastAPI's threadpool.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --no-access-log
