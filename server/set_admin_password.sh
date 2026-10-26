#!/usr/bin/env bash
# 重設管理員密碼指令
NEW_PWD="${1:-admin888}"
EMAIL="${2:-}"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "=================================================="
echo "  AI 會議系統 - 管理員密碼重設工具"
echo "=================================================="
python3 set_admin_password.py "$NEW_PWD" "$EMAIL"
