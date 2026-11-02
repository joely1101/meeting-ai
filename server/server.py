#!/usr/bin/env python3
"""
Hawkeye AI Meeting System v2.0 - Core Server
Features:
1. User Authentication & Authorization (Admin / User roles).
2. Domain restriction: Only @hawkeyetech.com.tw allowed to register.
3. Email verification flow.
4. Per-user meeting isolation.
5. Cloud AI Auto-Transcribe with Gemini 3.8 Flash.
6. HTTP 206 Partial Content (Range Requests) for seamless audio seeking.
"""

import os
import sys
import re
import json
import time

# 時區設定 (POSIX/Linux 系統上確保遵循 TZ 環境變數，預設 Asia/Taipei)
if hasattr(time, "tzset"):
    if not os.environ.get("TZ"):
        os.environ["TZ"] = "Asia/Taipei"
    time.tzset()

import datetime
import urllib.parse
import threading
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

import database
import email_service
import ai_transcriber
import teams_bot

RECORDV2_DIR = os.path.abspath(os.path.join(SERVER_DIR, ".."))
WEB_DIR = os.path.join(RECORDV2_DIR, "web")
DATA_DIR = os.path.join(RECORDV2_DIR, "data")
MEETINGS_DIR = os.path.join(DATA_DIR, "meetings")
TO_BE_DELETE_DIR = os.path.join(DATA_DIR, "ToBeDelete")

os.makedirs(WEB_DIR, exist_ok=True)
os.makedirs(MEETINGS_DIR, exist_ok=True)
os.makedirs(TO_BE_DELETE_DIR, exist_ok=True)

# 初始化資料庫
database.init_db()
try:
    database.index_existing_meetings(MEETINGS_DIR)
except Exception as e:
    print(f"Error indexing existing meetings: {e}")

# 全域轉錄任務狀態
TASKS = {}

# 全域 Teams 自動撥入與排程管理器
teams_bot_mgr = teams_bot.get_bot_manager(TASKS)

def read_json_body(handler):
    try:
        length = int(handler.headers.get("Content-Length", 0))
        if length > 0:
            return json.loads(handler.rfile.read(length).decode("utf-8"))
    except Exception:
        pass
    return {}

def get_auth_user(headers):
    # 支援內部 MCP 工具專用金鑰授權
    mcp_key = headers.get("X-MCP-Key") or ""
    auth_header = headers.get("Authorization") or headers.get("X-Auth-Token") or ""
    token = auth_header.replace("Bearer ", "").strip()
    if token == "mcp-teams-secret-key" or mcp_key == "mcp-teams-secret-key":
        return {"id": 1, "email": "admin@hawkeyetech.com.tw", "name": "AI Assistant (MCP)", "role": "admin"}

    if not token:
        return None
    return database.get_user_by_session(token)

def send_json(handler, status_code, data):
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache, no-store, must-revalidate, max-age=0")
    handler.send_header("Pragma", "no-cache")
    handler.send_header("Expires", "0")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.end_headers()
    handler.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

def stream_media(handler, file_path):
    """處理音訊串流並支援 HTTP 206 Partial Content (Range requests)"""
    ext = os.path.splitext(file_path)[1].lower()
    ctype = "audio/mpeg" if ext == ".mp3" else ("audio/mp4" if ext == ".m4a" else "application/octet-stream")

    try:
        f = open(file_path, "rb")
    except OSError:
        handler.send_error(404, "檔案無法讀取")
        return

    try:
        fs = os.fstat(f.fileno())
        file_len = fs.st_size
        range_header = handler.headers.get("Range")

        if range_header and range_header.startswith("bytes="):
            try:
                ranges = range_header[6:].split("-")
                start = int(ranges[0]) if ranges[0].strip() else 0
                end = int(ranges[1]) if ranges[1].strip() else file_len - 1
                if start >= file_len or end >= file_len or start > end:
                    handler.send_error(416, "Requested Range Not Satisfiable")
                    f.close()
                    return

                handler.send_response(206, "Partial Content")
                handler.send_header("Content-Type", ctype)
                handler.send_header("Content-Range", f"bytes {start}-{end}/{file_len}")
                handler.send_header("Content-Length", str(end - start + 1))
                handler.send_header("Accept-Ranges", "bytes")
                handler.send_header("Access-Control-Allow-Origin", "*")
                handler.end_headers()

                f.seek(start)
                bytes_to_send = end - start + 1
                buffer_size = 64 * 1024
                while bytes_to_send > 0:
                    chunk = f.read(min(buffer_size, bytes_to_send))
                    if not chunk:
                        break
                    try:
                        handler.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    bytes_to_send -= len(chunk)
                f.close()
                return
            except ValueError:
                pass

        handler.send_response(200)
        handler.send_header("Content-Type", ctype)
        handler.send_header("Content-Length", str(file_len))
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.end_headers()

        buffer_size = 64 * 1024
        while True:
            chunk = f.read(buffer_size)
            if not chunk:
                break
            try:
                handler.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                break
        f.close()
    except Exception:
        f.close()
        raise

def handle_recordv2_options(handler):
    handler.send_response(200)
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Auth-Token, X-Filename, X-Meeting-Title")
    handler.end_headers()

def handle_recordv2_get(handler):
    parsed = urllib.parse.urlparse(handler.path)
    path = parsed.path
    clean_path = path[9:] if path.startswith("/recordv2") else path
    prefix = "/recordv2" if path.startswith("/recordv2") else ""
    params = urllib.parse.parse_qs(parsed.query)

    # 0. 短網址跳轉 /share/<token> -> ?share=<token>
    if clean_path.startswith("/share/"):
        token = clean_path.replace("/share/", "").strip()
        handler.send_response(302)
        handler.send_header("Location", f"{prefix}/?share={urllib.parse.quote(token)}")
        handler.end_headers()
        return True

    # 1. 取得當前登入者資訊
    if clean_path == "/api/auth/me":
        user = get_auth_user(handler.headers)
        if user:
            send_json(handler, 200, {"status": "ok", "user": user})
        else:
            send_json(handler, 401, {"status": "error", "message": "未登入或憑證已過期"})
        return True

    # 2. Email 驗證啟用
    if clean_path == "/api/auth/verify":
        token = params.get("token", [""])[0]
        success, msg = database.verify_email_token(token)
        send_json(handler, 200 if success else 400, {"status": "ok" if success else "error", "message": msg})
        return True

    # 3. 取得會議列表 (限登入者)
        # 會議分類 Profile 列表
    if clean_path == "/api/profiles":
        user = get_auth_user(handler.headers)
        if not user:
            send_json(handler, 401, {"status": "error", "message": "請先登入"})
            return True
        try:
            profiles = database.get_user_profiles(user["id"])
            send_json(handler, 200, {"status": "ok", "profiles": profiles})
        except Exception as e:
            send_json(handler, 500, {"status": "error", "message": str(e)})
        return True

    if clean_path == "/api/meetings":
        user = get_auth_user(handler.headers)
        if not user:
            send_json(handler, 401, {"status": "error", "message": "請先登入後檢視會議"})
            return True
        is_admin = (user["role"] == "admin")
        meetings = database.get_user_meetings(user["id"], is_admin=is_admin)

        active_tasks = []
        for tid, tinfo in TASKS.items():
            st = tinfo.get("status")
            if st in ("processing", "upload_done", "queued", "error"):
                if is_admin or tinfo.get("userId") == user["id"]:
                    active_tasks.append({
                        "taskId": tid,
                        "title": tinfo.get("meetingTitle", "新上傳會議錄音"),
                        "status": st,
                        "error": tinfo.get("error", ""),
                        "progress": tinfo.get("progress", 30),
                        "message": tinfo.get("message", "雲端 AI 轉錄與摘要生成中..."),
                        "created_at": tinfo.get("created_at", "")
                    })

        send_json(handler, 200, {"status": "ok", "meetings": meetings, "active_tasks": active_tasks})
        return True

    # 3.1 取得 Teams Bot 預約排程與執行中任務 (限登入者)
    if clean_path == "/api/bot/tasks":
        user = get_auth_user(handler.headers)
        if not user:
            send_json(handler, 401, {"status": "error", "message": "請先登入後檢視排程任務"})
            return True
        is_admin = (user["role"] == "admin")
        bot_tasks = database.get_user_bot_tasks(user["id"], is_admin=is_admin)
        send_json(handler, 200, {"status": "ok", "tasks": bot_tasks})
        return True

    # 3.2 取得單一 Teams Bot 任務狀態與結果
    if clean_path == "/api/bot/task_status":
        user = get_auth_user(handler.headers)
        if not user:
            send_json(handler, 401, {"status": "error", "message": "請先登入"})
            return True
        task_id = params.get("task_id", [""])[0].strip()
        if not task_id:
            send_json(handler, 400, {"status": "error", "message": "缺少 task_id"})
            return True
        task = database.get_bot_task(task_id)
        if not task:
            send_json(handler, 404, {"status": "error", "message": "找不到該任務"})
            return True

        summary_text = ""
        transcript_text = ""
        if task.get("meeting_id"):
            for root, dirs, files in os.walk(MEETINGS_DIR):
                if os.path.basename(root) == task["meeting_id"]:
                    for f in files:
                        if f.endswith("_會議摘要.txt") or f.endswith("summary.txt"):
                            try:
                                with open(os.path.join(root, f), "r", encoding="utf-8") as sf:
                                    summary_text = sf.read()
                            except Exception:
                                pass
                        elif f.endswith("_逐字稿.txt") or f.endswith("transcript.txt"):
                            try:
                                with open(os.path.join(root, f), "r", encoding="utf-8") as tf:
                                    transcript_text = tf.read()
                            except Exception:
                                pass
                    break

        send_json(handler, 200, {
            "status": "ok",
            "task": task,
            "summary_text": summary_text,
            "transcript_text": transcript_text
        })
        return True

    # 4. 取得特定會議詳細資料 (逐字稿與摘要)
    if clean_path.startswith("/api/meeting/"):
        user = get_auth_user(handler.headers)
        if not user:
            send_json(handler, 401, {"status": "error", "message": "請先登入後檢視會議"})
            return True

        raw_id = clean_path.replace("/api/meeting/", "").strip()
        meeting_id = urllib.parse.unquote(raw_id)
        meeting_folder = None
        for root, dirs, files in os.walk(MEETINGS_DIR):
            if os.path.basename(root) == meeting_id:
                meeting_folder = root
                break

        if not meeting_folder or not os.path.exists(meeting_folder):
            send_json(handler, 404, {"status": "error", "message": "找不到該會議檔案"})
            return True

        # 讀取摘要與逐字稿
        summary_text = ""
        transcript_text = ""
        audio_file = ""

        for f in os.listdir(meeting_folder):
            if f.endswith("_會議摘要.txt") or f.endswith("summary.txt") or f.endswith("_會議摘要.md"):
                try:
                    with open(os.path.join(meeting_folder, f), "r", encoding="utf-8") as sf:
                        summary_text = sf.read()
                except Exception:
                    pass
            elif f.endswith("_逐字稿.txt") or f.endswith("transcript.txt") or f.endswith("_逐字稿.md"):
                try:
                    with open(os.path.join(meeting_folder, f), "r", encoding="utf-8") as tf:
                        transcript_text = tf.read()
                except Exception:
                    pass
            elif f.lower().endswith(('.mp3', '.m4a', '.wav', '.aac', '.flac')):
                audio_file = f

        time_pattern = re.compile(r'^\[(\d+(?::\d{2}){1,2})\]\s*(?:\[([^\]]+)\]|\(([^)]+)\)|([^：:\n\[\(]{1,30})[：:])?\s*(.*)$')
        dialogues = []
        speakers = set()

        for line in transcript_text.splitlines():
            line = line.strip()
            if not line:
                continue
            m = time_pattern.match(line)
            if m:
                t_str, s1, s2, s3, txt = m.groups()
                spk = (s1 or s2 or s3 or '').strip()
                if not spk:
                    spk = '說話者'
                else:
                    speakers.add(spk)
                parts = [int(x) for x in t_str.split(':')]
                if len(parts) == 3:
                    sec = parts[0] * 3600 + parts[1] * 60 + parts[2]
                elif len(parts) == 2:
                    sec = parts[0] * 60 + parts[1]
                else:
                    sec = parts[0]
                dialogues.append({"sec": sec, "time": t_str, "speaker": spk, "text": (txt or '').strip()})
            else:
                if dialogues:
                    dialogues[-1]["text"] += " " + line

        if not speakers and dialogues:
            speakers.add('說話者')

        send_json(handler, 200, {
            "status": "ok",
            "id": meeting_id,
            "audio_url": f"{prefix}/api/media/{urllib.parse.quote(meeting_id)}/{urllib.parse.quote(audio_file)}",
            "summary_text": summary_text,
            "transcript_text": transcript_text,
            "dialogues": dialogues,
            "speakers": sorted(list(speakers))
        })
        return True

    # 4.1 公開唯讀分享：透過 Token 查詢會議內容 (免登入、唯讀，含過期檢查)
    if clean_path == "/api/share/meeting":
        token = params.get("token", [""])[0].strip()
        if not token:
            send_json(handler, 400, {"status": "error", "message": "缺少分享 Token"})
            return True
        m, err = database.get_meeting_by_share_token(token)
        if not m:
            send_json(handler, 404, {"status": "error", "message": err or "此分享連結無效、不存在或已被原作者取消分享"})
            return True

        meeting_id = m["id"]
        meeting_folder = None
        for root, dirs, files in os.walk(MEETINGS_DIR):
            if os.path.basename(root) == meeting_id:
                meeting_folder = root
                break

        if not meeting_folder or not os.path.exists(meeting_folder):
            send_json(handler, 404, {"status": "error", "message": "找不到該會議檔案"})
            return True

        summary_text = ""
        transcript_text = ""
        audio_file = ""

        for f in os.listdir(meeting_folder):
            if f.endswith("_會議摘要.txt") or f.endswith("summary.txt") or f.endswith("_會議摘要.md"):
                try:
                    with open(os.path.join(meeting_folder, f), "r", encoding="utf-8") as sf:
                        summary_text = sf.read()
                except Exception:
                    pass
            elif f.endswith("_逐字稿.txt") or f.endswith("transcript.txt") or f.endswith("_逐字稿.md"):
                try:
                    with open(os.path.join(meeting_folder, f), "r", encoding="utf-8") as tf:
                        transcript_text = tf.read()
                except Exception:
                    pass
            elif f.lower().endswith(('.mp3', '.m4a', '.wav', '.aac', '.flac')):
                audio_file = f

        time_pattern = re.compile(r'^\[(\d+(?::\d{2}){1,2})\]\s*(?:\[([^\]]+)\]|\(([^)]+)\)|([^：:\n\[\(]{1,30})[：:])?\s*(.*)$')
        dialogues = []
        speakers = set()

        for line in transcript_text.splitlines():
            line = line.strip()
            if not line:
                continue
            match = time_pattern.match(line)
            if match:
                t_str, s1, s2, s3, txt = match.groups()
                spk = (s1 or s2 or s3 or '').strip()
                if not spk:
                    spk = '說話者'
                else:
                    speakers.add(spk)
                parts = [int(x) for x in t_str.split(':')]
                if len(parts) == 3:
                    sec = parts[0] * 3600 + parts[1] * 60 + parts[2]
                elif len(parts) == 2:
                    sec = parts[0] * 60 + parts[1]
                else:
                    sec = parts[0]
                dialogues.append({"sec": sec, "time": t_str, "speaker": spk, "text": (txt or '').strip()})
            else:
                if dialogues:
                    dialogues[-1]["text"] += " " + line

        if not speakers and dialogues:
            speakers.add('說話者')

        send_json(handler, 200, {
            "status": "ok",
            "id": meeting_id,
            "title": m.get("title", meeting_id),
            "date": m.get("date", ""),
            "duration_str": m.get("duration_str", "00:00"),
            "owner_name": m.get("owner_name", ""),
            "is_shared_view": True,
            "share_expires_at": m.get("share_expires_at", ""),
            "audio_url": f"{prefix}/api/media/{urllib.parse.quote(meeting_id)}/{urllib.parse.quote(audio_file)}",
            "summary_text": summary_text,
            "transcript_text": transcript_text,
            "dialogues": dialogues,
            "speakers": sorted(list(speakers))
        })
        return True

    # 5. 音訊串流 (支援 HTTP 206 Range requests)
    if clean_path.startswith("/api/media/"):
        parts = clean_path.replace("/api/media/", "").split("/", 1)
        if len(parts) == 2:
            meeting_id = urllib.parse.unquote(parts[0])
            filename = urllib.parse.unquote(parts[1])
            for root, dirs, files in os.walk(MEETINGS_DIR):
                if os.path.basename(root) == meeting_id:
                    file_path = os.path.join(root, filename)
                    if os.path.exists(file_path):
                        stream_media(handler, file_path)
                        return True
        handler.send_error(404, "音訊檔案未找到")
        return True

    # 6. 轉錄任務狀態查詢
    if clean_path == "/api/task_status":
        task_id = params.get("taskId", [""])[0]
        info = TASKS.get(task_id, {"status": "not_found", "message": "任務不存在"})
        send_json(handler, 200, info)
        return True

    # 7. Admin: 使用者列表
    if clean_path == "/api/admin/users":
        user = get_auth_user(handler.headers)
        if not user or user["role"] != "admin":
            send_json(handler, 403, {"status": "error", "message": "無權限存取管理介面"})
            return True
        users = database.get_all_users()
        send_json(handler, 200, {"status": "ok", "users": users})
        return True

    # 8. Admin: Key Pool 資源池列表
    if clean_path == "/api/admin/keys":
        user = get_auth_user(handler.headers)
        if not user or user["role"] != "admin":
            send_json(handler, 403, {"status": "error", "message": "無權限存取 Key Pool 管理介面"})
            return True
        keys = database.get_all_system_keys()
        send_json(handler, 200, {"status": "ok", "keys": keys})
        return True

    # 9. Admin: 伺服器即時 Log 查詢
    if clean_path == "/api/admin/logs":
        user = get_auth_user(handler.headers)
        if not user or user["role"] != "admin":
            send_json(handler, 403, {"status": "error", "message": "無權限查看伺服器 Log"})
            return True
        log_path = "/home/joel/www/server.log"
        lines = []
        if os.path.exists(log_path):
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as lf:
                    lines = lf.readlines()[-250:]
            except Exception as e:
                lines = [f"讀取 Log 錯誤: {e}"]
        send_json(handler, 200, {"status": "ok", "logs": "".join(lines)})
        return True

    return False

def handle_recordv2_post(handler):
    parsed = urllib.parse.urlparse(handler.path)
    path = parsed.path
    clean_path = path[9:] if path.startswith("/recordv2") else path
    prefix = "/recordv2" if path.startswith("/recordv2") else ""
    params = urllib.parse.parse_qs(parsed.query)

    # 1. 註冊
    if clean_path == "/api/auth/register":
        try:
            length = int(handler.headers.get("Content-Length", 0))
            body = json.loads(handler.rfile.read(length).decode("utf-8"))
            name = body.get("name", "")
            email = body.get("email", "")
            password = body.get("password", "")

            reg_result = database.register_user(name, email, password)
            email_service.send_verification_email(
                reg_result["email"],
                reg_result["name"],
                reg_result["verify_token"]
            )

            send_json(handler, 200, {
                "status": "ok",
                "message": "註冊成功！驗證信件已寄出，請至信箱點擊啟用連結。",
                "email": email,
                "verify_token": reg_result["verify_token"]
            })
        except Exception as e:
            send_json(handler, 400, {"status": "error", "message": str(e)})
        return True

    # 2. 登入
    if clean_path == "/api/auth/login":
        try:
            length = int(handler.headers.get("Content-Length", 0))
            body = json.loads(handler.rfile.read(length).decode("utf-8"))
            email = body.get("email", "")
            password = body.get("password", "")

            auth_result, err = database.authenticate_user(email, password)
            if err:
                send_json(handler, 400, {"status": "error", "message": err})
                return True

            send_json(handler, 200, {
                "status": "ok",
                "token": auth_result["token"],
                "user": auth_result["user"]
            })
        except Exception as e:
            send_json(handler, 400, {"status": "error", "message": str(e)})
        return True

    # 3. 登出
    if clean_path == "/api/auth/logout":
        token = (handler.headers.get("Authorization") or "").replace("Bearer ", "").strip()
        if token:
            database.delete_session(token)
        send_json(handler, 200, {"status": "ok", "message": "已成功登出"})
        return True

    # 3.1 Teams Bot 預約排程或立即撥入
    if clean_path == "/api/bot/schedule":
        user = get_auth_user(handler.headers)
        if not user:
            send_json(handler, 401, {"status": "error", "message": "請先登入後預約 Teams 撥入"})
            return True
        try:
            body = read_json_body(handler)
            title = body.get("title", "").strip() or "Teams 會議錄音"
            teams_url = body.get("teams_url", "").strip()
            scheduled_at = body.get("scheduled_at", "").strip()
            max_duration = int(body.get("max_duration_min", 60))
            bot_name = body.get("bot_name", "AI 會議記錄助理").strip()
            notify_email = body.get("notify_email", "").strip()
            join_now = bool(body.get("join_now", False))

            if not teams_url:
                send_json(handler, 400, {"status": "error", "message": "請提供有效的 Teams 會議連結"})
                return True

            if join_now or not scheduled_at:
                scheduled_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            profile_id = body.get("profile_id")
            if profile_id:
                try:
                    profile_id = int(profile_id)
                except Exception:
                    profile_id = None

            task = database.create_bot_task(
                user_id=user["id"],
                title=title,
                teams_url=teams_url,
                scheduled_at=scheduled_at,
                max_duration_min=max_duration,
                bot_name=bot_name,
                notify_email=notify_email,
                profile_id=profile_id
            )

            if join_now:
                # 立即觸發執行
                teams_bot_mgr.trigger_task_immediately(task["id"])
                msg = "已成功啟動立即撥入 Teams 會議！"
            else:
                msg = f"已成功建立預約排程，AI 助理將於 {scheduled_at} 自動撥入！"

            send_json(handler, 200, {"status": "ok", "task": task, "message": msg})
        except Exception as e:
            send_json(handler, 500, {"status": "error", "message": str(e)})
        return True

    # 3.2 手動結束進行中的會議錄音或取消任務
    if clean_path == "/api/bot/stop":
        user = get_auth_user(handler.headers)
        if not user:
            send_json(handler, 401, {"status": "error", "message": "請先登入"})
            return True
        try:
            body = read_json_body(handler)
            task_id = body.get("task_id", "").strip()
            is_admin = (user["role"] == "admin")
            success, msg = teams_bot_mgr.stop_task(task_id, user["id"], is_admin=is_admin)
            send_json(handler, 200 if success else 400, {"status": "ok" if success else "error", "message": msg})
        except Exception as e:
            send_json(handler, 500, {"status": "error", "message": str(e)})
        return True

    # 3.3 刪除任務記錄
    if clean_path == "/api/bot/delete":
        user = get_auth_user(handler.headers)
        if not user:
            send_json(handler, 401, {"status": "error", "message": "請先登入"})
            return True
        try:
            body = read_json_body(handler)
            task_id = body.get("task_id", "").strip()
            is_admin = (user["role"] == "admin")
            affected = database.delete_bot_task(task_id, user["id"], is_admin=is_admin)
            send_json(handler, 200 if affected else 400, {
                "status": "ok" if affected else "error",
                "message": "已成功刪除任務記錄" if affected else "刪除失敗或任務不存在"
            })
        except Exception as e:
            send_json(handler, 500, {"status": "error", "message": str(e)})
        return True

    # 4. 音訊上傳並啟動 AI 轉錄
        # 轉錄任務忽略/關閉
    if clean_path == "/api/task/dismiss":
        data = read_json_body(handler)
        tid = data.get("taskId", "")
        if tid in TASKS:
            del TASKS[tid]
        send_json(handler, 200, {"status": "ok"})
        return True

        # 會議分類 Profile 儲存與刪除
    if clean_path == "/api/profiles/save":
        user = get_auth_user(handler.headers)
        if not user:
            send_json(handler, 401, {"status": "error", "message": "請先登入"})
            return True
        try:
            body = read_json_body(handler)
            pid = body.get("id")
            if pid:
                try:
                    pid = int(pid)
                except Exception:
                    pid = None
            name = body.get("name", "").strip()
            speakers = body.get("speakers", "").strip()
            keywords = body.get("keywords", "").strip()
            loc = body.get("location_context", "").strip()
            hint = body.get("prompt_hint", "").strip()
            is_def = bool(body.get("is_default", False))

            target_id = database.save_user_profile(
                user["id"], pid, name, speakers, keywords, loc, hint, is_def
            )
            profiles = database.get_user_profiles(user["id"])
            send_json(handler, 200, {
                "status": "ok",
                "profileId": target_id,
                "profiles": profiles,
                "message": "會議分類設定已儲存！"
            })
        except Exception as e:
            send_json(handler, 500, {"status": "error", "message": str(e)})
        return True

    if clean_path == "/api/profiles/delete":
        user = get_auth_user(handler.headers)
        if not user:
            send_json(handler, 401, {"status": "error", "message": "請先登入"})
            return True
        try:
            body = read_json_body(handler)
            pid = body.get("id")
            if not pid:
                send_json(handler, 400, {"status": "error", "message": "缺少 profile id"})
                return True
            database.delete_user_profile(user["id"], int(pid))
            profiles = database.get_user_profiles(user["id"])
            send_json(handler, 200, {
                "status": "ok",
                "profiles": profiles,
                "message": "會議分類已刪除"
            })
        except Exception as e:
            send_json(handler, 500, {"status": "error", "message": str(e)})
        return True

    if clean_path == "/api/upload":
        user = get_auth_user(handler.headers)
        if not user:
            send_json(handler, 401, {"status": "error", "message": "請先登入後上傳音訊"})
            return True

        try:
            raw_filename = handler.headers.get("X-Filename") or params.get("filename", ["meeting_audio.mp3"])[0]
            filename = urllib.parse.unquote(raw_filename)
            raw_title = handler.headers.get("X-Meeting-Title") or params.get("title", [""])[0]
            user_title = urllib.parse.unquote(raw_title)
            raw_profile_id = handler.headers.get("X-Profile-Id") or params.get("profile_id", [""])[0]
            profile_id = int(raw_profile_id) if raw_profile_id and raw_profile_id.isdigit() else None
            raw_custom_prompt = handler.headers.get("X-Custom-Prompt") or params.get("custom_prompt", [""])[0]
            custom_prompt = urllib.parse.unquote(raw_custom_prompt) if raw_custom_prompt else ""

            content_len = int(handler.headers.get("Content-Length", 0))
            if content_len <= 0:
                send_json(handler, 400, {"status": "error", "message": "音訊檔案大小無效"})
                return True

            now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            stem_clean = re.sub(r'[^a-zA-Z0-9_\u4e00-\u9fa5]', '_', Path(filename).stem)[:25]
            folder_name = f"{now_str}_{stem_clean}" if stem_clean else now_str

            user_meetings_dir = os.path.join(MEETINGS_DIR, str(user["id"]))
            meeting_folder = os.path.join(user_meetings_dir, folder_name)
            os.makedirs(meeting_folder, exist_ok=True)

            audio_dst = os.path.join(meeting_folder, filename)

            bytes_left = content_len
            buffer_size = 64 * 1024
            with open(audio_dst, "wb") as f_out:
                while bytes_left > 0:
                    chunk = handler.rfile.read(min(bytes_left, buffer_size))
                    if not chunk:
                        break
                    f_out.write(chunk)
                    bytes_left -= len(chunk)

            task_id = f"task_{int(time.time() * 1000)}"
            TASKS[task_id] = {
                "id": task_id,
                "status": "processing",
                "userId": user["id"],
                "meetingTitle": user_title or filename,
                "progress": 20,
                "message": "音訊已儲存，正在送往 Google 雲端 AI 處理...",
                "folderName": folder_name,
                "meetingId": None,
                "error": None,
                "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }

            threading.Thread(
                target=ai_transcriber.transcribe_audio_task,
                args=(task_id, audio_dst, user["id"], user_title, TASKS, profile_id, custom_prompt),
                daemon=True
            ).start()

            send_json(handler, 200, {
                "status": "ok",
                "taskId": task_id,
                "folderName": folder_name,
                "message": "音訊上傳成功，雲端轉錄處理中"
            })
        except Exception as e:
            send_json(handler, 500, {"status": "error", "message": str(e)})
        return True

    # 5. 刪除會議 (搬移至 ToBeDelete 備份目錄，不再重新掃描)
    if clean_path == "/api/meeting/delete":
        user = get_auth_user(handler.headers)
        if not user:
            send_json(handler, 401, {"status": "error", "message": "請先登入"})
            return True
        try:
            length = int(handler.headers.get("Content-Length", 0))
            body = json.loads(handler.rfile.read(length).decode("utf-8"))
            meeting_id = body.get("meetingId")
            if not meeting_id:
                send_json(handler, 400, {"status": "error", "message": "缺少 meetingId"})
                return True

            is_admin = (user["role"] == "admin")

            # 1. 搬移磁碟檔案至 ToBeDelete 備份目錄
            moved = False
            possible_dirs = [
                os.path.join(MEETINGS_DIR, str(user["id"]), meeting_id)
            ]
            if is_admin and os.path.exists(MEETINGS_DIR):
                for uid in os.listdir(MEETINGS_DIR):
                    p = os.path.join(MEETINGS_DIR, uid, meeting_id)
                    if p not in possible_dirs:
                        possible_dirs.append(p)

            for m_path in possible_dirs:
                if os.path.exists(m_path) and os.path.isdir(m_path):
                    user_sub = os.path.basename(os.path.dirname(m_path))
                    dest_user_dir = os.path.join(TO_BE_DELETE_DIR, user_sub)
                    os.makedirs(dest_user_dir, exist_ok=True)

                    dest_meeting_dir = os.path.join(dest_user_dir, meeting_id)
                    if os.path.exists(dest_meeting_dir):
                        stamp = datetime.datetime.now().strftime("_del_%Y%m%d_%H%M%S")
                        dest_meeting_dir = os.path.join(dest_user_dir, f"{meeting_id}{stamp}")

                    import shutil
                    shutil.move(m_path, dest_meeting_dir)
                    print(f"[Meeting Delete] 會議 {meeting_id} 實體檔案已安全搬移至備份目錄: {dest_meeting_dir}", flush=True)
                    moved = True
                    break

            # 2. 自 SQLite 資料庫刪除
            success = database.delete_meeting(meeting_id, user["id"], is_admin=is_admin)

            # 3. 若有進行中的任務暫存，一併清除
            for tid in list(TASKS.keys()):
                if TASKS[tid].get("folderName") == meeting_id or TASKS[tid].get("meetingId") == meeting_id:
                    del TASKS[tid]

            send_json(handler, 200 if (success or moved) else 400, {
                "status": "ok" if (success or moved) else "error",
                "message": "會議記錄已成功刪除並移至 ToBeDelete 目錄備份" if (success or moved) else "找不到該會議或無刪除權限"
            })
        except Exception as e:
            send_json(handler, 500, {"status": "error", "message": str(e)})
        return True


    # 5.1 更新會議摘要
    if clean_path == "/api/meeting/update_summary":
        user = get_auth_user(handler.headers)
        if not user:
            send_json(handler, 401, {"status": "error", "message": "請先登入"})
            return True
        try:
            length = int(handler.headers.get("Content-Length", 0))
            body = json.loads(handler.rfile.read(length).decode("utf-8"))
            meeting_id = body.get("meetingId")
            new_summary = body.get("summaryText", "")
            if not meeting_id:
                send_json(handler, 400, {"status": "error", "message": "缺少 meetingId"})
                return True

            is_admin = (user["role"] == "admin")

            # 尋找該會議所在的實體目錄
            meeting_folder = None
            possible_dirs = [
                os.path.join(MEETINGS_DIR, str(user["id"]), meeting_id)
            ]
            if is_admin and os.path.exists(MEETINGS_DIR):
                for uid in os.listdir(MEETINGS_DIR):
                    p = os.path.join(MEETINGS_DIR, uid, meeting_id)
                    if p not in possible_dirs:
                        possible_dirs.append(p)

            for m_path in possible_dirs:
                if os.path.exists(m_path) and os.path.isdir(m_path):
                    meeting_folder = m_path
                    break

            if not meeting_folder:
                send_json(handler, 404, {"status": "error", "message": "找不到該會議或無編輯權限"})
                return True

            # 尋找現有的摘要檔案
            summary_file_name = None
            for f in os.listdir(meeting_folder):
                if f.endswith("_會議摘要.txt") or f.endswith("summary.txt") or f.endswith("_會議摘要.md"):
                    summary_file_name = f
                    break

            if not summary_file_name:
                summary_file_name = f"{meeting_id}_會議摘要.txt"

            target_file_path = os.path.join(meeting_folder, summary_file_name)
            with open(target_file_path, "w", encoding="utf-8") as sf:
                sf.write(new_summary)

            print(f"[Meeting Summary Update] 使用者 {user['email']} 已更新會議 {meeting_id} 的摘要: {target_file_path}", flush=True)

            send_json(handler, 200, {
                "status": "ok",
                "message": "會議摘要已成功更新",
                "summaryText": new_summary
            })
        except Exception as e:
            send_json(handler, 500, {"status": "error", "message": str(e)})
        return True

    # 5.2 更新會議標題
    if clean_path == "/api/meeting/update_title":
        user = get_auth_user(handler.headers)
        if not user:
            send_json(handler, 401, {"status": "error", "message": "請先登入"})
            return True
        try:
            length = int(handler.headers.get("Content-Length", 0))
            body = json.loads(handler.rfile.read(length).decode("utf-8"))
            meeting_id = body.get("meetingId")
            new_title = (body.get("newTitle") or "").strip()
            if not meeting_id or not new_title:
                send_json(handler, 400, {"status": "error", "message": "缺少 meetingId 或 newTitle"})
                return True

            is_admin = (user["role"] == "admin")

            # 1. 更新資料庫
            success = database.update_meeting_title(meeting_id, new_title, user["id"], is_admin=is_admin)

            # 2. 同步將標題寫入會議資料夾內的 title.txt 作為持久實體備份
            possible_dirs = [
                os.path.join(MEETINGS_DIR, str(user["id"]), meeting_id)
            ]
            if is_admin and os.path.exists(MEETINGS_DIR):
                for uid in os.listdir(MEETINGS_DIR):
                    p = os.path.join(MEETINGS_DIR, uid, meeting_id)
                    if p not in possible_dirs:
                        possible_dirs.append(p)

            for m_path in possible_dirs:
                if os.path.exists(m_path) and os.path.isdir(m_path):
                    try:
                        with open(os.path.join(m_path, "title.txt"), "w", encoding="utf-8") as tf:
                            tf.write(new_title)
                    except Exception:
                        pass
                    break

            if success:
                print(f"[Meeting Title Update] 使用者 {user['email']} 已更新會議 {meeting_id} 標題為: {new_title}", flush=True)
                send_json(handler, 200, {
                    "status": "ok",
                    "message": "會議標題已成功更新",
                    "meetingId": meeting_id,
                    "newTitle": new_title
                })
            else:
                send_json(handler, 404, {"status": "error", "message": "找不到該會議或無權限修改"})
        except Exception as e:
            send_json(handler, 500, {"status": "error", "message": str(e)})
        return True

    # 5.3 產生或撤銷會議唯讀分享 Token (含自訂有效期限)
    if clean_path == "/api/meeting/share":
        user = get_auth_user(handler.headers)
        if not user:
            send_json(handler, 401, {"status": "error", "message": "請先登入後操作"})
            return True
        try:
            length = int(handler.headers.get("Content-Length", 0))
            body = json.loads(handler.rfile.read(length).decode("utf-8")) if length > 0 else {}
            meeting_id = body.get("meetingId")
            action = body.get("action", "get")
            raw_expire = body.get("expireHours")
            if raw_expire is not None:
                try:
                    expire_hours = int(raw_expire)
                except ValueError:
                    expire_hours = 24
            else:
                expire_hours = 24

            if not meeting_id:
                send_json(handler, 400, {"status": "error", "message": "缺少 meetingId"})
                return True

            is_admin = (user["role"] == "admin")
            if action == "revoke":
                database.revoke_share_token(meeting_id, user["id"], is_admin=is_admin)
                send_json(handler, 200, {"status": "ok", "message": "已成功停止分享", "share_token": "", "expires_at": ""})
            else:
                token, expires_at, err = database.get_or_create_share_token(
                    meeting_id, user["id"], is_admin=is_admin, expire_hours=expire_hours
                )
                if err:
                    send_json(handler, 403, {"status": "error", "message": err})
                    return True

                host = handler.headers.get("Host", "localhost:8080")
                proto = handler.headers.get("X-Forwarded-Proto", "http")
                base_url = f"{proto}://{host}"
                share_url = f"{base_url}{prefix}/?share={token}"
                send_json(handler, 200, {
                    "status": "ok",
                    "share_token": token,
                    "share_url": share_url,
                    "expires_at": expires_at
                })
        except Exception as e:
            send_json(handler, 500, {"status": "error", "message": str(e)})
        return True

    # 6. Admin: 切換使用者狀態
    if clean_path == "/api/admin/users/toggle_active":
        user = get_auth_user(handler.headers)
        if not user or user["role"] != "admin":
            send_json(handler, 403, {"status": "error", "message": "無權限執行管理員動作"})
            return True
        try:
            length = int(handler.headers.get("Content-Length", 0))
            body = json.loads(handler.rfile.read(length).decode("utf-8"))
            target_user_id = body.get("userId")
            active = body.get("active", True)
            database.toggle_user_active(target_user_id, active)
            send_json(handler, 200, {"status": "ok", "message": "狀態更新成功"})
        except Exception as e:
            send_json(handler, 400, {"status": "error", "message": str(e)})
        return True

    # 7. Admin: 手動啟用驗證使用者
    if clean_path == "/api/admin/users/verify":
        user = get_auth_user(handler.headers)
        if not user or user["role"] != "admin":
            send_json(handler, 403, {"status": "error", "message": "無權限執行管理員動作"})
            return True
        try:
            length = int(handler.headers.get("Content-Length", 0))
            body = json.loads(handler.rfile.read(length).decode("utf-8"))
            target_user_id = body.get("userId")
            database.manually_verify_user(target_user_id)
            send_json(handler, 200, {"status": "ok", "message": "使用者已手動啟用完成"})
        except Exception as e:
            send_json(handler, 400, {"status": "error", "message": str(e)})
        return True

    # 8. Admin: 刪除使用者
    if clean_path == "/api/admin/users/delete":
        user = get_auth_user(handler.headers)
        if not user or user["role"] != "admin":
            send_json(handler, 403, {"status": "error", "message": "無權限執行管理員動作"})
            return True
        try:
            length = int(handler.headers.get("Content-Length", 0))
            body = json.loads(handler.rfile.read(length).decode("utf-8"))
            target_user_id = body.get("userId")
            if target_user_id == user["id"]:
                send_json(handler, 400, {"status": "error", "message": "無法刪除目前登入中的 Admin 帳號"})
                return True
            database.delete_user(target_user_id)
            send_json(handler, 200, {"status": "ok", "message": "使用者已刪除"})
        except Exception as e:
            send_json(handler, 400, {"status": "error", "message": str(e)})
        return True

    # 9. 使用者設定: 自訂個人 Google API Key
    if clean_path == "/api/user/api_key":
        user = get_auth_user(handler.headers)
        if not user:
            send_json(handler, 401, {"status": "error", "message": "請先登入"})
            return True
        try:
            length = int(handler.headers.get("Content-Length", 0))
            body = json.loads(handler.rfile.read(length).decode("utf-8"))
            api_key = (body.get("api_key") or "").strip()
            database.set_user_api_key(user["id"], api_key)
            send_json(handler, 200, {
                "status": "ok",
                "message": "自訂 Google API Key 已成功儲存！您的錄音轉錄將優先使用個人 Key。" if api_key else "已清除個人 API Key，將自動使用系統共用 Key Pool 進行轉錄。",
                "has_custom_api_key": bool(api_key),
                "api_key_masked": database.mask_key(api_key)
            })
        except Exception as e:
            send_json(handler, 400, {"status": "error", "message": str(e)})
        return True

    # 10. 使用者設定: 修改自身密碼
    if clean_path == "/api/user/change_password":
        user = get_auth_user(handler.headers)
        if not user:
            send_json(handler, 401, {"status": "error", "message": "請先登入"})
            return True
        try:
            length = int(handler.headers.get("Content-Length", 0))
            body = json.loads(handler.rfile.read(length).decode("utf-8"))
            curr_pwd = body.get("current_password", "")
            new_pwd = body.get("new_password", "")
            ok, msg = database.change_user_password(user["id"], curr_pwd, new_pwd)
            send_json(handler, 200 if ok else 400, {"status": "ok" if ok else "error", "message": msg})
        except Exception as e:
            send_json(handler, 400, {"status": "error", "message": str(e)})
        return True

    # 10. Admin: Key Pool 新增 API Key
    if clean_path == "/api/admin/keys/add":
        user = get_auth_user(handler.headers)
        if not user or user["role"] != "admin":
            send_json(handler, 403, {"status": "error", "message": "無權限執行管理員動作"})
            return True
        try:
            length = int(handler.headers.get("Content-Length", 0))
            body = json.loads(handler.rfile.read(length).decode("utf-8"))
            api_key = body.get("api_key", "").strip()
            note = body.get("note", "").strip()
            new_id = database.add_system_key(api_key, note)
            send_json(handler, 200, {"status": "ok", "message": "API Key 已成功加入 Key Pool 資源池！", "keyId": new_id})
        except Exception as e:
            send_json(handler, 400, {"status": "error", "message": str(e)})
        return True

    # 11. Admin: Key Pool 刪除 API Key
    if clean_path == "/api/admin/keys/delete":
        user = get_auth_user(handler.headers)
        if not user or user["role"] != "admin":
            send_json(handler, 403, {"status": "error", "message": "無權限執行管理員動作"})
            return True
        try:
            length = int(handler.headers.get("Content-Length", 0))
            body = json.loads(handler.rfile.read(length).decode("utf-8"))
            key_id = body.get("keyId")
            database.delete_system_key(key_id)
            send_json(handler, 200, {"status": "ok", "message": "API Key 已自 Key Pool 移除"})
        except Exception as e:
            send_json(handler, 400, {"status": "error", "message": str(e)})
        return True

    # 12. Admin: Key Pool 切換啟用狀態
    if clean_path == "/api/admin/keys/toggle":
        user = get_auth_user(handler.headers)
        if not user or user["role"] != "admin":
            send_json(handler, 403, {"status": "error", "message": "無權限執行管理員動作"})
            return True
        try:
            length = int(handler.headers.get("Content-Length", 0))
            body = json.loads(handler.rfile.read(length).decode("utf-8"))
            key_id = body.get("keyId")
            active = body.get("active", True)
            database.toggle_system_key(key_id, active)
            send_json(handler, 200, {"status": "ok", "message": "Key 狀態已更新"})
        except Exception as e:
            send_json(handler, 400, {"status": "error", "message": str(e)})
        return True

    return False

class RecordV2Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory=None, **kwargs):
        if directory is None:
            directory = WEB_DIR
        super().__init__(*args, directory=directory, **kwargs)

    def do_OPTIONS(self):
        handle_recordv2_options(self)

    def do_GET(self):
        if handle_recordv2_get(self):
            return
        super().do_GET()

    def do_POST(self):
        if handle_recordv2_post(self):
            return
        self.send_error(404, "Endpoint Not Found")

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

def run_server(port=8080):
    server = ThreadedHTTPServer(("0.0.0.0", port), RecordV2Handler)
    print(f"🚀 RecordV2 Server running on http://0.0.0.0:{port}")
    server.serve_forever()

if __name__ == "__main__":
    port = 8080
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    run_server(port)
