#!/bin/sh
set -e

# 時區設定 (若未指定 TZ 預設使用 Asia/Taipei)
export TZ="${TZ:-Asia/Taipei}"
if [ -d /usr/share/zoneinfo ]; then
    ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime 2>/dev/null || true
    echo "$TZ" > /etc/timezone 2>/dev/null || true
fi

echo "=================================================="
echo "🚀 智匯會議錄音與 AI 雲端轉譯系統 (Standalone)"
echo "=================================================="

# 確保持久化目錄存在
mkdir -p /app/data/meetings
mkdir -p /app/data/ToBeDelete

PORT="${PORT:-8080}"
echo "[*] Server Port:        $PORT"
echo "[*] Public URL:         ${PUBLIC_URL:-http://localhost:$PORT}"
echo "[*] Admin Email:        ${ADMIN_EMAIL:-admin@local}"
if [ -n "$GOOGLE_CLIENT_ID" ]; then
    echo "[*] Google Auth:        Configured ($GOOGLE_CLIENT_ID)"
else
    echo "[!] Google Auth:        Not configured (can be set via admin web UI)"
fi

# 啟動後端伺服器
exec python3 server/server.py "$PORT"
