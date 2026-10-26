#!/usr/bin/env python3
"""
管理員密碼設定與重設工具 (Standalone CLI)
使用方式:
    python set_admin_password.py [新密碼] [管理員Email]
例如:
    python set_admin_password.py mynewpassword admin@example.com
"""

import os
import sys

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

import database

def main():
    new_password = "admin"
    email = None

    if len(sys.argv) > 1:
        new_password = sys.argv[1].strip()
    if len(sys.argv) > 2:
        email = sys.argv[2].strip().lower()

    if not new_password:
        print("[!] 密碼不能為空！")
        sys.exit(1)

    database.init_db()
    success, msg = database.set_admin_password(new_password=new_password, email=email)
    print(f"[*] {msg}")

if __name__ == "__main__":
    main()
