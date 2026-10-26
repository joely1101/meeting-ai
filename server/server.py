#!/usr/bin/env python3
"""
AI Meeting System - Standalone Core Server
Features:
1. Google OAuth 2.0 (Gmail) User Authentication.
2. Dynamic Server URL injection for Web and Windows EXE Client.
3. Gemini Key Pool dynamic load balancing (Least-Used routing).
4. Audio chunk streaming with HTTP 206 Partial Content.
5. Auto transcription & summary generation via Gemini AI.
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
import io
import zipfile
import urllib.parse
import threading
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

import database
import ai_transcriber
import google_auth

APP_DIR = os.path.abspath(os.path.join(SERVER_DIR, ".."))
WEB_DIR = os.path.join(APP_DIR, "web")
DATA_DIR = os.path.join(APP_DIR, "data")
MEETINGS_DIR = os.path.join(DATA_DIR, "meetings")
TO_BE_DELETE_DIR = os.path.join(DATA_DIR, "ToBeDelete")
CONFIG_PATH = os.path.join(SERVER_DIR, "config.json")

os.makedirs(WEB_DIR, exist_ok=True)
os.makedirs(MEETINGS_DIR, exist_ok=True)
os.makedirs(TO_BE_DELETE_DIR, exist_ok=True)

def load_server_config():
    cfg = {
        "public_url": "",
        "google_client_id": "",
        "google_client_secret": "",
        "admin_email": "",
        "gemini_api_key": ""
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass

    # 環境變數優先覆蓋
    if os.environ.get("PUBLIC_URL"):
        cfg["public_url"] = os.environ.get("PUBLIC_URL").rstrip("/")
    if os.environ.get("GOOGLE_CLIENT_ID"):
        cfg["google_client_id"] = os.environ.get("GOOGLE_CLIENT_ID").strip()
    if os.environ.get("GOOGLE_CLIENT_SECRET"):
        cfg["google_client_secret"] = os.environ.get("GOOGLE_CLIENT_SECRET").strip()
    if os.environ.get("ADMIN_EMAIL"):
        cfg["admin_email"] = os.environ.get("ADMIN_EMAIL").strip().lower()

    return cfg

def save_server_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving config: {e}")

# 初始化資料庫
database.init_db()
try:
    database.index_existing_meetings(MEETINGS_DIR)
except Exception as e:
    print(f"Error indexing existing meetings: {e}")

# 全域轉錄任務狀態
TASKS = {}

def get_base_url(handler):
    cfg = load_server_config()
    if cfg.get("public_url"):
        return cfg["public_url"].rstrip("/")

    # 從請求 Header 自動推導
    host = handler.headers.get("Host", "localhost:8080")
    proto = handler.headers.get("X-Forwarded-Proto", "http")
    return f"{proto}://{host}"

def get_auth_user(headers):
    auth_header = headers.get("Authorization") or headers.get("X-Auth-Token")
    if not auth_header:
        return None
    token = auth_header.replace("Bearer ", "").strip()
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

def send_redirect(handler, target_url):
    handler.send_response(302)
    handler.send_header("Location", target_url)
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()

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

def handle_options(handler):
    handler.send_response(200)
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Auth-Token, X-Filename, X-Meeting-Title")
    handler.end_headers()

def handle_api_get(handler):
    parsed = urllib.parse.urlparse(handler.path)
    clean_path = parsed.path
    params = urllib.parse.parse_qs(parsed.query)

    # 0. 短網址跳轉 /share/<token> -> /?share=<token>
    if clean_path.startswith("/share/"):
        token = clean_path.replace("/share/", "").strip()
        handler.send_response(302)
        handler.send_header("Location", f"/?share={urllib.parse.quote(token)}")
        handler.end_headers()
        return True

    # 1. 取得當前伺服器公開資訊與設定 (給 Web 與 Client)
    if clean_path == "/api/config":
        cfg = load_server_config()
        base_url = get_base_url(handler)
        keys = database.get_all_system_keys()
        active_keys = [k for k in keys if k["is_active"]]

        send_json(handler, 200, {
            "status": "ok",
            "public_url": base_url,
            "google_client_id": cfg.get("google_client_id", ""),
            "has_google_auth": bool(cfg.get("google_client_id") and cfg.get("google_client_secret")),
            "key_pool_count": len(active_keys),
            "admin_email": cfg.get("admin_email", "")
        })
        return True

    # 2. 取得當前登入者資訊
    if clean_path == "/api/auth/me":
        user = get_auth_user(handler.headers)
        if user:
            send_json(handler, 200, {"status": "ok", "user": user})
        else:
            send_json(handler, 401, {"status": "error", "message": "未登入或憑證已過期"})
        return True

    # 3. Google OAuth 登入跳轉
    if clean_path == "/api/auth/google/login":
        cfg = load_server_config()
        client_id = cfg.get("google_client_id", "").strip()
        client_secret = cfg.get("google_client_secret", "").strip()

        if not client_id or not client_secret:
            msg = "系統尚未配置 Google OAuth 憑證 (Client ID / Client Secret)。請系統管理員登入後台完成設定。"
            handler.send_response(400)
            handler.send_header("Content-Type", "text/html; charset=utf-8")
            handler.end_headers()
            html = f"""<!DOCTYPE html><html><head><meta charset='utf-8'><title>尚未配置 Google 登入</title>
            <style>body{{font-family:sans-serif;padding:40px;background:#f8fafc;color:#1e293b;line-height:1.6;max-width:600px;margin:auto;}}
            .card{{background:#fff;border-radius:12px;padding:24px;border:1px solid #e2e8f0;box-shadow:0 4px 6px -1px rgba(0,0,0,0.1);}}
            h2{{color:#ef4444;margin-top:0;}} a{{color:#2563eb;text-decoration:none;font-weight:bold;}}</style></head>
            <body><div class='card'><h2>⚠️ Google 登入尚未啟用</h2><p>{msg}</p><p><a href='/'>返回首頁</a></p></div></body></html>"""
            handler.wfile.write(html.encode("utf-8"))
            return True

        base_url = get_base_url(handler)
        redirect_uri = f"{base_url}/api/auth/google/callback"
        state = params.get("state", [""])[0]
        auth_url = google_auth.get_google_auth_url(client_id, redirect_uri, state=state)
        send_redirect(handler, auth_url)
        return True

    # 4. Google OAuth Callback 回調端點
    if clean_path == "/api/auth/google/callback":
        cfg = load_server_config()
        client_id = cfg.get("google_client_id", "").strip()
        client_secret = cfg.get("google_client_secret", "").strip()
        base_url = get_base_url(handler)
        redirect_uri = f"{base_url}/api/auth/google/callback"

        code = params.get("code", [""])[0]
        state = params.get("state", [""])[0]
        error = params.get("error", [""])[0]

        if error:
            send_redirect(handler, f"/?auth_error={urllib.parse.quote(error)}")
            return True

        if not code:
            send_redirect(handler, "/?auth_error=missing_code")
            return True

        try:
            google_user = google_auth.exchange_code_for_user(code, client_id, client_secret, redirect_uri)
            auth_res = database.get_or_create_google_user(
                email=google_user["email"],
                name=google_user["name"],
                picture=google_user.get("picture", "")
            )
            token = auth_res["token"]
            user = auth_res["user"]

            # 若 state 為 client ticket，代表是來自 Windows EXE 錄音器的授權請求
            if state and (state.startswith("ticket_") or google_auth._find_ticket_key(state)):
                google_auth.bind_client_ticket(state, token, user)
                # 顯示成功授權頁面，提示使用者可返回客戶端
                handler.send_response(200)
                handler.send_header("Content-Type", "text/html; charset=utf-8")
                handler.end_headers()
                html = f"""<!DOCTYPE html><html><head><meta charset='utf-8'><title>登入成功</title>
                <style>body{{font-family:Segoe UI, sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#0f172a;color:#fff;}}
                .box{{text-align:center;background:#1e293b;padding:40px;border-radius:16px;border:1px solid #334155;max-width:400px;}}
                .icon{{font-size:50px;color:#10b981;margin-bottom:16px;}} h2{{margin:0 0 10px 0;}} p{{color:#94a3b8;font-size:14px;}}</style></head>
                <body><div class='box'><div class='icon'>✓</div><h2>登入成功！</h2><p>歡迎回來，{user.get('name')} ({user.get('email')})。<br>您現在可以關閉此瀏覽器分頁，並返回會議錄音器繼續使用。</p></div></body></html>"""
                handler.wfile.write(html.encode("utf-8"))
                return True

            # 一般網頁登入，帶 token 跳轉回前端首頁
            send_redirect(handler, f"/?token={token}")
            return True
        except Exception as e:
            print(f"[Google Auth Error] {e}", flush=True)
            send_redirect(handler, f"/?auth_error={urllib.parse.quote(str(e))}")
            return True

    # 5. 客戶端查詢登入票據狀態 (輪詢)
    if clean_path == "/api/auth/client_ticket/poll":
        ticket = params.get("ticket", [""])[0]
        res = google_auth.poll_client_ticket(ticket)
        send_json(handler, 200, res)
        return True

    # 6. 動態注入 URL 下載用戶端 ZIP 包 (含 會議錄音.exe + client_config.json)
    if clean_path == "/api/download/client":
        base_url = get_base_url(handler)
        exe_src = None
        for candidate in [
            os.path.join(WEB_DIR, "會議錄音.exe"),
            os.path.join(WEB_DIR, "會議錄音器.exe"),
            os.path.join(APP_DIR, "client", "會議錄音.exe"),
            os.path.join(APP_DIR, "client", "dist", "會議錄音.exe"),
            os.path.join(APP_DIR, "client", "會議錄音器.exe"),
            os.path.join(APP_DIR, "client", "dist", "會議錄音器.exe"),
        ]:
            if os.path.exists(candidate):
                exe_src = candidate
                break

        cfg_content = json.dumps({
            "server_url": base_url,
            "token": "",
            "user_email": "",
            "user_name": "",
            "auto_upload": True
        }, ensure_ascii=False, indent=2)

        mem_zip = io.BytesIO()
        with zipfile.ZipFile(mem_zip, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("client_config.json", cfg_content.encode("utf-8"))
            if exe_src and os.path.exists(exe_src):
                with open(exe_src, "rb") as ef:
                    exe_bytes = ef.read()
                    overlay_data = b"\n__SERVER_URL_OVERLAY__:" + base_url.encode("utf-8") + b":__END__\n"
                    zf.writestr("會議錄音.exe", exe_bytes + overlay_data)

        zip_data = mem_zip.getvalue()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/zip")
        filename_utf8 = urllib.parse.quote("會議錄音.zip")
        handler.send_header("Content-Disposition", f'attachment; filename="MeetingRecorder.zip"; filename*=UTF-8\'\'{filename_utf8}')
        handler.send_header("Content-Length", str(len(zip_data)))
        handler.end_headers()
        handler.wfile.write(zip_data)
        return True

    # 7. 單一 EXE 下載 (動態注入 URL Overlay)
    if clean_path == "/api/download/client-exe":
        base_url = get_base_url(handler)
        exe_src = None
        for candidate in [
            os.path.join(WEB_DIR, "會議錄音.exe"),
            os.path.join(WEB_DIR, "會議錄音器.exe"),
            os.path.join(APP_DIR, "client", "會議錄音.exe"),
            os.path.join(APP_DIR, "client", "dist", "會議錄音.exe"),
            os.path.join(APP_DIR, "client", "會議錄音器.exe"),
            os.path.join(APP_DIR, "client", "dist", "會議錄音器.exe"),
        ]:
            if os.path.exists(candidate):
                exe_src = candidate
                break

        if not exe_src or not os.path.exists(exe_src):
            send_json(handler, 404, {"status": "error", "message": "未找到 Windows 用戶端執行檔，請先執行 client/build_exe.py 編譯。"})
            return True

        with open(exe_src, "rb") as ef:
            exe_bytes = ef.read()

        overlay_data = b"\n__SERVER_URL_OVERLAY__:" + base_url.encode("utf-8") + b":__END__\n"
        final_bytes = exe_bytes + overlay_data

        handler.send_response(200)
        handler.send_header("Content-Type", "application/octet-stream")
        filename_utf8 = urllib.parse.quote("會議錄音.exe")
        handler.send_header("Content-Disposition", f'attachment; filename="MeetingRecorder.exe"; filename*=UTF-8\'\'{filename_utf8}')
        handler.send_header("Content-Length", str(len(final_bytes)))
        handler.end_headers()
        handler.wfile.write(final_bytes)
        return True

    # 7.5 安裝版 EXE 下載 (動態注入 URL Overlay)
    if clean_path == "/api/download/client-installer":
        base_url = get_base_url(handler)
        exe_src = None
        for candidate in [
            os.path.join(WEB_DIR, "會議錄音_安裝檔.exe"),
            os.path.join(APP_DIR, "client", "dist_installer", "會議錄音_安裝檔.exe"),
            os.path.join(APP_DIR, "client", "會議錄音_安裝檔.exe"),
        ]:
            if os.path.exists(candidate):
                exe_src = candidate
                break

        if not exe_src or not os.path.exists(exe_src):
            send_json(handler, 404, {"status": "error", "message": "未找到安裝版執行檔，請先編譯安裝程式。"})
            return True

        with open(exe_src, "rb") as ef:
            exe_bytes = ef.read()

        overlay_data = b"\n__SERVER_URL_OVERLAY__:" + base_url.encode("utf-8") + b":__END__\n"
        final_bytes = exe_bytes + overlay_data

        handler.send_response(200)
        handler.send_header("Content-Type", "application/octet-stream")
        filename_utf8 = urllib.parse.quote("會議錄音_安裝檔.exe")
        handler.send_header("Content-Disposition", f'attachment; filename="MeetingRecorder_Setup.exe"; filename*=UTF-8\'\'{filename_utf8}')
        handler.send_header("Content-Length", str(len(final_bytes)))
        handler.end_headers()
        handler.wfile.write(final_bytes)
        return True

    # 8. 取得會議列表 (限登入者)
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

    # 9. 取得特定會議詳細資料
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

        time_pattern = re.compile(r'^\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(?:\[([^\]]+)\]|([^：:\n]+)[：:])\s*(.*)$')
        dialogues = []
        speakers = set()

        for line in transcript_text.splitlines():
            line = line.strip()
            if not line:
                continue
            m = time_pattern.match(line)
            if m:
                t_str, s1, s2, txt = m.groups()
                spk = (s1 or s2).strip()
                speakers.add(spk)
                parts = t_str.split(':')
                sec = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2]) if len(parts) == 3 else int(parts[0]) * 60 + int(parts[1])
                dialogues.append({"sec": sec, "time": t_str, "speaker": spk, "text": txt.strip()})
            else:
                if dialogues:
                    dialogues[-1]["text"] += " " + line

        send_json(handler, 200, {
            "status": "ok",
            "id": meeting_id,
            "audio_url": f"/api/media/{urllib.parse.quote(meeting_id)}/{urllib.parse.quote(audio_file)}",
            "summary_text": summary_text,
            "dialogues": dialogues,
            "speakers": sorted(list(speakers))
        })
        return True

    # 9.1 公開唯讀分享：透過 Token 查詢會議內容 (免登入、唯讀)
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

        time_pattern = re.compile(r'^\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(?:\[([^\]]+)\]|([^：:\n]+)[：:])\s*(.*)$')
        dialogues = []
        speakers = set()

        for line in transcript_text.splitlines():
            line = line.strip()
            if not line:
                continue
            match = time_pattern.match(line)
            if match:
                t_str, s1, s2, txt = match.groups()
                spk = (s1 or s2).strip()
                speakers.add(spk)
                parts = t_str.split(':')
                sec = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2]) if len(parts) == 3 else int(parts[0]) * 60 + int(parts[1])
                dialogues.append({"sec": sec, "time": t_str, "speaker": spk, "text": txt.strip()})
            else:
                if dialogues:
                    dialogues[-1]["text"] += " " + line

        send_json(handler, 200, {
            "status": "ok",
            "id": meeting_id,
            "title": m.get("title", meeting_id),
            "date": m.get("date", ""),
            "duration_str": m.get("duration_str", "00:00"),
            "owner_name": m.get("owner_name", ""),
            "is_shared_view": True,
            "share_expires_at": m.get("share_expires_at", ""),
            "audio_url": f"/api/media/{urllib.parse.quote(meeting_id)}/{urllib.parse.quote(audio_file)}",
            "summary_text": summary_text,
            "dialogues": dialogues,
            "speakers": sorted(list(speakers))
        })
        return True

    # 10. 音訊串流 (支援 HTTP 206 Range requests)
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

    # 11. 轉錄任務狀態查詢
    if clean_path == "/api/task_status":
        task_id = params.get("taskId", [""])[0]
        info = TASKS.get(task_id, {"status": "not_found", "message": "任務不存在"})
        send_json(handler, 200, info)
        return True

    # 12. Admin: 使用者列表
    if clean_path == "/api/admin/users":
        user = get_auth_user(handler.headers)
        if not user or user["role"] != "admin":
            send_json(handler, 403, {"status": "error", "message": "無權限存取管理介面"})
            return True
        users = database.get_all_users()
        send_json(handler, 200, {"status": "ok", "users": users})
        return True

    # 13. Admin: Key Pool 資源池列表
    if clean_path == "/api/admin/keys":
        user = get_auth_user(handler.headers)
        if not user or user["role"] != "admin":
            send_json(handler, 403, {"status": "error", "message": "無權限存取 Key Pool 管理介面"})
            return True
        keys = database.get_all_system_keys()
        send_json(handler, 200, {"status": "ok", "keys": keys})
        return True

    return False

def handle_api_post(handler):
    parsed = urllib.parse.urlparse(handler.path)
    clean_path = parsed.path
    params = urllib.parse.parse_qs(parsed.query)

    # 1. 客戶端建立登入 Ticket (供桌面錄音器跳轉瀏覽器 Google 授權)
    if clean_path == "/api/auth/client_ticket/create":
        ticket = google_auth.create_client_ticket()
        base_url = get_base_url(handler)
        login_url = f"{base_url}/api/auth/google/login?state={ticket}"
        send_json(handler, 200, {
            "status": "ok",
            "ticket": ticket,
            "login_url": login_url
        })
        return True

    # 2. 本地帳密登入 (應急管理員入口)
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

    # 4. 音訊上傳並啟動 AI 轉錄
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
                args=(task_id, audio_dst, user["id"], user_title, TASKS),
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

    # 5. 任務忽略
    if clean_path == "/api/task/dismiss":
        try:
            length = int(handler.headers.get("Content-Length", 0))
            body = json.loads(handler.rfile.read(length).decode("utf-8"))
            tid = body.get("taskId", "")
            if tid in TASKS:
                del TASKS[tid]
            send_json(handler, 200, {"status": "ok"})
        except Exception:
            send_json(handler, 200, {"status": "ok"})
        return True

    # 6. 刪除會議 (搬移至 ToBeDelete 備份目錄)
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

            # 搬移實體檔案至 ToBeDelete 目錄
            moved = False
            possible_dirs = [os.path.join(MEETINGS_DIR, str(user["id"]), meeting_id)]
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
                    moved = True
                    break

            success = database.delete_meeting(meeting_id, user["id"], is_admin=is_admin)
            for tid in list(TASKS.keys()):
                if TASKS[tid].get("folderName") == meeting_id or TASKS[tid].get("meetingId") == meeting_id:
                    del TASKS[tid]

            send_json(handler, 200 if (success or moved) else 400, {
                "status": "ok" if (success or moved) else "error",
                "message": "會議記錄已成功刪除" if (success or moved) else "找不到該會議或無刪除權限"
            })
        except Exception as e:
            send_json(handler, 500, {"status": "error", "message": str(e)})
        return True

    # 7. 更新會議摘要
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
            meeting_folder = None
            possible_dirs = [os.path.join(MEETINGS_DIR, str(user["id"]), meeting_id)]
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

            summary_file_name = None
            for f in os.listdir(meeting_folder):
                if f.endswith("_會議摘要.txt") or f.endswith("summary.txt") or f.endswith("_會議摘要.md"):
                    summary_file_name = f
                    break
            if not summary_file_name:
                summary_file_name = f"{meeting_id}_會議摘要.txt"

            with open(os.path.join(meeting_folder, summary_file_name), "w", encoding="utf-8") as sf:
                sf.write(new_summary)

            send_json(handler, 200, {"status": "ok", "message": "會議摘要已成功更新", "summaryText": new_summary})
        except Exception as e:
            send_json(handler, 500, {"status": "error", "message": str(e)})
        return True

    # 8. 更新會議標題
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
            success = database.update_meeting_title(meeting_id, new_title, user["id"], is_admin=is_admin)

            possible_dirs = [os.path.join(MEETINGS_DIR, str(user["id"]), meeting_id)]
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
                send_json(handler, 200, {"status": "ok", "message": "標題已更新", "newTitle": new_title})
            else:
                send_json(handler, 404, {"status": "error", "message": "找不到該會議或無權限修改"})
        except Exception as e:
            send_json(handler, 500, {"status": "error", "message": str(e)})
        return True

    # 8.1 產生或撤銷會議唯讀分享 Token (含自訂有效期限)
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
                base_url = get_base_url(handler)
                share_url = f"{base_url}/?share={token}"
                send_json(handler, 200, {
                    "status": "ok",
                    "share_token": token,
                    "share_url": share_url,
                    "expires_at": expires_at
                })
        except Exception as e:
            send_json(handler, 500, {"status": "error", "message": str(e)})
        return True

    # 9. 使用者自訂 Google API Key
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

    # 9.5 使用者 / 管理員修改密碼
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
            if ok:
                send_json(handler, 200, {"status": "ok", "message": msg})
            else:
                send_json(handler, 400, {"status": "error", "message": msg})
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

    # 13. Admin: 儲存伺服器設定 (如 Google OAuth 憑證)
    if clean_path == "/api/admin/config":
        user = get_auth_user(handler.headers)
        if not user or user["role"] != "admin":
            send_json(handler, 403, {"status": "error", "message": "無權限執行管理員動作"})
            return True
        try:
            length = int(handler.headers.get("Content-Length", 0))
            body = json.loads(handler.rfile.read(length).decode("utf-8"))
            cfg = load_server_config()
            if "google_client_id" in body:
                cfg["google_client_id"] = body["google_client_id"].strip()
            if "google_client_secret" in body:
                cfg["google_client_secret"] = body["google_client_secret"].strip()
            if "public_url" in body:
                cfg["public_url"] = body["public_url"].strip().rstrip("/")
            save_server_config(cfg)
            send_json(handler, 200, {"status": "ok", "message": "伺服器系統設定已成功更新！"})
        except Exception as e:
            send_json(handler, 400, {"status": "error", "message": str(e)})
        return True

    # 14. Admin: 使用者啟用/停用/刪除
    if clean_path == "/api/admin/users/toggle_active":
        user = get_auth_user(handler.headers)
        if not user or user["role"] != "admin":
            send_json(handler, 403, {"status": "error", "message": "無權限執行管理員動作"})
            return True
        try:
            length = int(handler.headers.get("Content-Length", 0))
            body = json.loads(handler.rfile.read(length).decode("utf-8"))
            database.toggle_user_active(body.get("userId"), body.get("active", True))
            send_json(handler, 200, {"status": "ok", "message": "狀態更新成功"})
        except Exception as e:
            send_json(handler, 400, {"status": "error", "message": str(e)})
        return True

    if clean_path == "/api/admin/users/delete":
        user = get_auth_user(handler.headers)
        if not user or user["role"] != "admin":
            send_json(handler, 403, {"status": "error", "message": "無權限執行管理員動作"})
            return True
        try:
            length = int(handler.headers.get("Content-Length", 0))
            body = json.loads(handler.rfile.read(length).decode("utf-8"))
            target_id = body.get("userId")
            if target_id == user["id"]:
                send_json(handler, 400, {"status": "error", "message": "無法刪除目前登入中的 Admin 帳號"})
                return True
            database.delete_user(target_id)
            send_json(handler, 200, {"status": "ok", "message": "使用者已刪除"})
        except Exception as e:
            send_json(handler, 400, {"status": "error", "message": str(e)})
        return True

    return False

class StandaloneHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory=None, **kwargs):
        if directory is None:
            directory = WEB_DIR
        super().__init__(*args, directory=directory, **kwargs)

    def do_OPTIONS(self):
        handle_options(self)

    def do_HEAD(self):
        if handle_api_get(self):
            return
        super().do_HEAD()

    def do_GET(self):
        if handle_api_get(self):
            return
        if self.path.startswith("/api/"):
            send_json(self, 404, {"status": "error", "message": "API 端點不存在 (404)"})
            return
        super().do_GET()

    def do_POST(self):
        if handle_api_post(self):
            return
        send_json(self, 404, {"status": "error", "message": "API 端點不存在 (404)"})

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

def run_server(port=8080):
    cfg = load_server_config()
    server = ThreadedHTTPServer(("0.0.0.0", port), StandaloneHandler)
    pub_url = cfg.get("public_url") or f"http://localhost:{port}"
    print(f"==================================================")
    print(f"🚀 AI Meeting System Server started!")
    print(f"   Port:       {port}")
    print(f"   Public URL: {pub_url}")
    print(f"   Google Auth:{'Enabled' if cfg.get('google_client_id') else 'Not configured yet'}")
    print(f"==================================================")
    server.serve_forever()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    run_server(port)
