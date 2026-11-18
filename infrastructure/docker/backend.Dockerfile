# One image, three roles (ADR-001): api | worker | browser-worker. Select with the container command.
#   api:            uvicorn apna.api.main:app --host 0.0.0.0 --port 8800
#   worker:         python -m apna.workflows.worker
#   browser-worker: python -m apna.workflows.worker --browser   (use the playwright target below)
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN useradd --create-home --uid 10001 apna
WORKDIR /app
COPY services/api/pyproject.toml ./
COPY services/api/src ./src
COPY services/api/alembic.ini ./
COPY services/api/migrations ./migrations
RUN pip install --upgrade pip && pip install .
USER apna
EXPOSE 8800
CMD ["uvicorn", "apna.api.main:app", "--host", "0.0.0.0", "--port", "8800"]

FROM base AS browser
USER root
RUN python -m playwright install --with-deps chromium && mkdir -p /data && chown apna /data
ENV BROWSER_PROFILE_ROOT=/data/profiles BROWSER_ARTIFACT_ROOT=/data/artifacts
USER apna
CMD ["python", "-m", "apna.workflows.worker", "--browser"]
