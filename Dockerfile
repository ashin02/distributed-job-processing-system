FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --system computegrid && useradd --system --gid computegrid computegrid

COPY pyproject.toml README.md ./
COPY apps/api ./apps/api
COPY alembic.ini ./
COPY migrations ./migrations

RUN pip install --no-cache-dir .

USER computegrid

CMD ["python", "-m", "uvicorn", "computegrid_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
