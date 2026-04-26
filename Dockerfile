FROM python:3.11-slim

WORKDIR /app

# 系統依賴（PIL 字型 + PostgreSQL client）
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-noto-cjk \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p data

# Railway 透過 $PORT 環境變數指定 port
CMD uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}
