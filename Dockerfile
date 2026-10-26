FROM python:3.11-slim

LABEL maintainer="Meeting AI System"
LABEL description="Standalone Meeting Recording & Gemini AI Transcribe Platform"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Taipei \
    PORT=8080

RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# 安裝基本依賴
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製應用程式檔案
COPY server/ ./server/
COPY web/ ./web/
COPY client/ ./client/
COPY entrypoint.sh .

RUN chmod +x ./entrypoint.sh

# 持久化儲存目錄 (會議音訊、資料庫 record.db、摘要逐字稿)
VOLUME ["/app/data"]

EXPOSE 8080

ENTRYPOINT ["/bin/sh", "/app/entrypoint.sh"]
