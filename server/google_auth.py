import os
import json
import time
import secrets
import urllib.parse
import requests

# 記憶體內儲存客戶端登入 Ticket
# 格式: { ticket: { "status": "pending"|"success"|"expired", "token": str, "user": dict, "created_at": float } }
CLIENT_TICKETS = {}

def clean_expired_tickets():
    now = time.time()
    expired = [t for t, v in CLIENT_TICKETS.items() if now - v["created_at"] > 600]
    for t in expired:
        CLIENT_TICKETS.pop(t, None)

def _find_ticket_key(ticket):
    if not ticket:
        return None
    if ticket in CLIENT_TICKETS:
        return ticket
    if f"ticket_{ticket}" in CLIENT_TICKETS:
        return f"ticket_{ticket}"
    if ticket.startswith("ticket_") and ticket[7:] in CLIENT_TICKETS:
        return ticket[7:]
    return None

def create_client_ticket():
    clean_expired_tickets()
    ticket = f"ticket_{secrets.token_urlsafe(24)}"
    CLIENT_TICKETS[ticket] = {
        "status": "pending",
        "token": None,
        "user": None,
        "created_at": time.time()
    }
    return ticket

def bind_client_ticket(ticket, token, user):
    clean_expired_tickets()
    key = _find_ticket_key(ticket)
    if key and key in CLIENT_TICKETS:
        CLIENT_TICKETS[key]["status"] = "success"
        CLIENT_TICKETS[key]["token"] = token
        CLIENT_TICKETS[key]["user"] = user
        return True
    return False

def poll_client_ticket(ticket):
    clean_expired_tickets()
    key = _find_ticket_key(ticket)
    if not key:
        return {"status": "expired", "message": "票據已過期或不存在"}
    info = CLIENT_TICKETS.get(key)
    if info and info["status"] == "success":
        data = {
            "status": "success",
            "token": info["token"],
            "user": info["user"]
        }
        CLIENT_TICKETS.pop(key, None)
        return data
    return {"status": "pending", "message": "等待 Google 授權完成"}

def get_google_auth_url(client_id, redirect_uri, state=""):
    """產生 Google OAuth 2.0 授權登入網址"""
    base_auth_url = "https://accounts.google.com/o/oauth2/v2/auth"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "select_account"
    }
    if state:
        params["state"] = state
    return f"{base_auth_url}?{urllib.parse.urlencode(params)}"

def exchange_code_for_user(code, client_id, client_secret, redirect_uri):
    """
    向 Google OAuth 端點交換 Access Token 並取得使用者 Email 與個人資訊
    """
    token_url = "https://oauth2.googleapis.com/token"
    token_data = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code"
    }

    token_res = requests.post(token_url, data=token_data, timeout=15)
    if token_res.status_code != 200:
        raise ValueError(f"Google 授權驗證失敗 ({token_res.status_code}): {token_res.text}")

    token_json = token_res.json()
    access_token = token_json.get("access_token")
    if not access_token:
        raise ValueError("無法自 Google 取得 access_token")

    userinfo_url = "https://www.googleapis.com/oauth2/v2/userinfo"
    user_res = requests.get(userinfo_url, headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
    if user_res.status_code != 200:
        raise ValueError(f"取得 Google 使用者資訊失敗 ({user_res.status_code}): {user_res.text}")

    user_info = user_res.json()
    email = user_info.get("email", "").strip().lower()
    name = user_info.get("name") or user_info.get("given_name") or (email.split("@")[0] if email else "User")
    picture = user_info.get("picture", "")

    if not email:
        raise ValueError("Google 回傳的使用者資料中缺少 Email")

    return {
        "email": email,
        "name": name,
        "picture": picture,
        "google_id": user_info.get("id", "")
    }
