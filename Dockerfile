FROM python:3.12.7-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md alembic.ini ./
COPY app ./app
COPY migrations ./migrations
COPY projects.json ./projects.json

RUN python -m pip install --upgrade pip \
    && python -m pip install .

RUN mkdir -p /data

ENV DEVCOCKPIT_DATABASE_URL=sqlite+pysqlite:////data/devcockpit.db \
    DEVCOCKPIT_PROJECTS_CONFIG_PATH=/app/projects.json

EXPOSE 8000

CMD ["sh", "-c", "python -m app.infrastructure.database && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
