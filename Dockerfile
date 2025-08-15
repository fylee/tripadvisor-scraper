# python-web/Dockerfile
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# 系統相依套件（playwright --with-deps 會再補）
RUN apt-get update && apt-get install -y curl wget gnupg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# requirements.txt 至少要包含：flask、playwright
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium

# 複製你的 app 程式
COPY . .

EXPOSE 5001
# 用 gunicorn 起服務（比 python app.py 穩定）
#CMD ["gunicorn", "-b", "0.0.0.0:5002", "app:app"]
CMD ["python", "./app.py"]
