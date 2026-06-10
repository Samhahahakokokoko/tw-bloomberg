FROM python:3.11-slim

WORKDIR /app

# asyncpg needs libpq-dev + gcc at build time
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc git fonts-noto-cjk curl gnupg2 \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
       | gpg --dearmor -o /usr/share/keyrings/postgresql.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/postgresql.gpg] \
       http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
       > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends \
       postgresql-client-16 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# tvDatafeed 只在 GitHub（不在 PyPI），需單獨安裝
RUN pip install --no-cache-dir "git+https://github.com/rongardF/tvdatafeed.git"

ARG CACHEBUST=1
COPY . .

RUN mkdir -p data

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PORT=8080

EXPOSE 8080

CMD python -m uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8080} --log-level info
