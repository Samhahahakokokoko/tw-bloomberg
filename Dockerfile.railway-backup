FROM python:3.11-slim

WORKDIR /app

# 最小必要依賴（asyncpg 需要 libpq-dev + gcc）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p data

# Railway 透過 PORT 環境變數告知要用哪個 port
EXPOSE 8080
ENV PORT=8080

CMD uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}
