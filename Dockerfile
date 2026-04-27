FROM python:3.11-slim

WORKDIR /app

# asyncpg needs libpq-dev + gcc at build time
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p data

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PORT=8080

EXPOSE 8080

CMD python -m uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8080} --log-level info
