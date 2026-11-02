FROM python:3.11-slim

LABEL maintainer="Meeting AI System"
LABEL description="Standalone Meeting Recording & Gemini AI Transcribe Platform"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Taipei \
    PORT=8080

RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 安裝 ffmpeg 支援長會議音訊切片轉錄
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安裝 Python 依賴
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製應用程式檔案
COPY server/ ./server/
COPY web/ ./web/
COPY client/ ./client/
COPY entrypoint.sh .

RUN chmod +x ./entrypoint.sh

# 掛載目錄
VOLUME ["/app/data"]

EXPOSE 8080

ENTRYPOINT ["/bin/sh", "/app/entrypoint.sh"]
