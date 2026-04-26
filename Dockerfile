FROM python:3.11-slim

WORKDIR /app

# 系統依賴
RUN apt-get update && apt-get install -y --no-install-recommends \
    # 中文字型（Rich Menu 圖片生成）
    fonts-noto-cjk \
    # Pillow 圖片處理
    libjpeg-dev \
    zlib1g-dev \
    libpng-dev \
    # PostgreSQL client（asyncpg 需要）
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 建立 data 目錄（SQLite fallback）
RUN mkdir -p data

# Railway 透過 $PORT 環境變數指定 port
CMD uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}
