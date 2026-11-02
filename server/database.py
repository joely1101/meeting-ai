import os
import sqlite3
import hashlib
import secrets
import datetime

import time
# 時區設定 (POSIX/Linux 系統上確保遵循 TZ 環境變數，預設 Asia/Taipei)
if hasattr(time, "tzset"):
    if not os.environ.get("TZ"):
        os.environ["TZ"] = "Asia/Taipei"
    time.tzset()

import json
import re

DB_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DB_DIR, "recordv2.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def hash_password(password, salt=None):
    if not salt:
        salt = secrets.token_hex(16)
    hashed = hashlib.sha256((password + salt).encode('utf-8')).hexdigest()
    return hashed, salt

def verify_password(password, stored_hash, salt):
    test_hash = hashlib.sha256((password + salt).encode('utf-8')).hexdigest()
    return test_hash == stored_hash

def mask_key(k):
    if not k:
        return ""
    k = k.strip()
    if len(k) <= 10:
        return "••••••••"
    return k[:6] + "••••••••" + k[-4:]

def init_db():
    conn = get_db()
    cursor = conn.cursor()

    # 1. 使用者資料表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        salt TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'user', -- 'admin' 或 'user'
        is_active INTEGER NOT NULL DEFAULT 1,
        email_verified INTEGER NOT NULL DEFAULT 0,
        verify_token TEXT,
        api_key TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        last_login TEXT
    )
    """)

    # 檢查是否需要升級 users 表 (新增 api_key 欄位)
    cursor.execute("PRAGMA table_info(users)")
    cols = [c[1] for c in cursor.fetchall()]
    if "api_key" not in cols:
        cursor.execute("ALTER TABLE users ADD COLUMN api_key TEXT DEFAULT ''")

    # 2. 登入 Session 資料表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """)

    # 3. 會議資料表 (依使用者隔離)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS meetings (
        id TEXT PRIMARY KEY, -- 資料夾名稱 / UUID
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        date TEXT NOT NULL,
        duration_sec INTEGER NOT NULL DEFAULT 0,
        duration_str TEXT NOT NULL DEFAULT '00:00',
        audio_file TEXT NOT NULL,
        summary_file TEXT,
        transcript_file TEXT,
        speakers_json TEXT NOT NULL DEFAULT '[]',
        dialogue_count INTEGER NOT NULL DEFAULT 0,
        share_token TEXT DEFAULT '',
        share_expires_at TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """)

    # 確保 meetings 欄位齊全
    cursor.execute("PRAGMA table_info(meetings)")
    m_cols = [c[1] for c in cursor.fetchall()]
    if "share_token" not in m_cols:
        cursor.execute("ALTER TABLE meetings ADD COLUMN share_token TEXT DEFAULT ''")
    if "share_expires_at" not in m_cols:
        cursor.execute("ALTER TABLE meetings ADD COLUMN share_expires_at TEXT DEFAULT ''")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_meetings_share_token ON meetings(share_token)")
    # 檢查 meetings 欄位升級 (profile_id, profile_name)
    if "profile_id" not in m_cols:
        cursor.execute("ALTER TABLE meetings ADD COLUMN profile_id INTEGER DEFAULT NULL")
    if "profile_name" not in m_cols:
        cursor.execute("ALTER TABLE meetings ADD COLUMN profile_name TEXT DEFAULT ''")

    # 4. 系統 API Key 資源池資料表 (Key Pool)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS system_keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        api_key TEXT UNIQUE NOT NULL,
        note TEXT DEFAULT '',
        is_active INTEGER NOT NULL DEFAULT 1,
        usage_count INTEGER NOT NULL DEFAULT 0,
        last_used_at TEXT,
        created_at TEXT NOT NULL
    )
    """)

    # 5. Teams Bot 預約排程與自動撥入資料表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS teams_bot_tasks (
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        teams_url TEXT NOT NULL,
        bot_name TEXT NOT NULL DEFAULT 'AI 會議記錄助理',
        scheduled_at TEXT NOT NULL, -- 'YYYY-MM-DD HH:MM:SS'
        max_duration_min INTEGER NOT NULL DEFAULT 60,
        notify_email TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'scheduled', -- 'scheduled', 'joining', 'in_lobby', 'recording', 'transcribing', 'completed', 'failed', 'cancelled'
        meeting_id TEXT DEFAULT '',
        error_message TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        started_at TEXT DEFAULT '',
        ended_at TEXT DEFAULT '',
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """)

    # 檢查是否需要升級 teams_bot_tasks 表 (新增 notify_email 欄位)
    cursor.execute("PRAGMA table_info(teams_bot_tasks)")
    bot_cols = [c[1] for c in cursor.fetchall()]
    if "notify_email" not in bot_cols:
        cursor.execute("ALTER TABLE teams_bot_tasks ADD COLUMN notify_email TEXT DEFAULT ''")
    if "profile_id" not in bot_cols:
        cursor.execute("ALTER TABLE teams_bot_tasks ADD COLUMN profile_id INTEGER DEFAULT NULL")

    # 6. 會議 Profile (提示詞範本 / 設定檔)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS meeting_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        speakers TEXT DEFAULT '',
        keywords TEXT DEFAULT '',
        location_context TEXT DEFAULT '',
        prompt_hint TEXT DEFAULT '',
        is_default INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_meeting_profiles_user ON meeting_profiles(user_id)")

    # 檢查是否需要初始化 Key Pool (載入預設 key)
    cursor.execute("SELECT COUNT(*) FROM system_keys")
    if cursor.fetchone()[0] == 0:
        cfg_path = os.path.join(DB_DIR, "config.json")
        default_key = ""
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg_data = json.load(f)
                    default_key = cfg_data.get("gemini_api_key", "").strip()
            except Exception:
                pass
        if not default_key:
            default_key = os.environ.get("GEMINI_API_KEY", "").strip()

        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("""
        INSERT OR IGNORE INTO system_keys (api_key, note, is_active, usage_count, created_at)
        VALUES (?, '系統預設 Gemini Key', 1, 0, ?)
        """, (default_key, now_str))
        print("Initialized system key pool with default Gemini key.")

    conn.commit()

    # 檢查是否需要建立預設 Admin 帳號 (預設密碼 teams)
    cursor.execute("SELECT id FROM users WHERE email = 'admin@hawkeyetech.com.tw'")
    admin = cursor.fetchone()
    if not admin:
        pwd_hash, salt = hash_password("teams")
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("""
        INSERT INTO users (email, name, password_hash, salt, role, is_active, email_verified, created_at)
        VALUES (?, ?, ?, ?, ?, 1, 1, ?)
        """, ("admin@hawkeyetech.com.tw", "系統管理員", pwd_hash, salt, "admin", now))
        conn.commit()
        print("Initialized default admin user: admin@hawkeyetech.com.tw / teams")

    conn.close()

# ----------------- 使用者認證與管理 -----------------

ALLOWED_DOMAIN = "@hawkeyetech.com.tw"

def register_user(name, email, password):
    email = email.strip().lower()
    name = name.strip()

    if not email.endswith(ALLOWED_DOMAIN):
        raise ValueError(f"限制僅能使用 {ALLOWED_DOMAIN} 企業信箱進行註冊！")

    if len(password) < 6:
        raise ValueError("密碼長度至少需 6 個字元以上！")

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
    if cursor.fetchone():
        conn.close()
        raise ValueError("此信箱已被註冊！")

    pwd_hash, salt = hash_password(password)
    verify_token = secrets.token_urlsafe(32)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute("""
    INSERT INTO users (email, name, password_hash, salt, role, is_active, email_verified, verify_token, created_at)
    VALUES (?, ?, ?, ?, 'user', 1, 0, ?, ?)
    """, (email, name or email.split('@')[0], pwd_hash, salt, verify_token, now))
    conn.commit()
    user_id = cursor.lastrowid
    conn.close()

    return {
        "user_id": user_id,
        "email": email,
        "name": name,
        "verify_token": verify_token
    }

def verify_email_token(token):
    if not token:
        return False, "無效的驗證碼"

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT id, email, is_active, email_verified FROM users WHERE verify_token = ?", (token,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False, "驗證連結無效或已過期"

    if row["email_verified"] == 1:
        conn.close()
        return True, "帳號先前已經驗證完成，請直接登入！"

    cursor.execute("UPDATE users SET email_verified = 1, verify_token = NULL WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()
    return True, "帳號驗證成功！您現在可以開始使用系統。"

def authenticate_user(email, password):
    email = email.strip().lower()
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM users WHERE email = ?", (email,))
    user = cursor.fetchone()
    if not user:
        conn.close()
        return None, "帳號或密碼錯誤"

    if user["is_active"] != 1:
        conn.close()
        return None, "此帳號已被管理員停用，請聯絡管理員"

    if user["email_verified"] != 1:
        conn.close()
        return None, "帳號尚未驗證啟用！請檢查您的 hawkeyetech.com.tw 電子郵件信箱並點擊啟用連結"

    if not verify_password(password, user["password_hash"], user["salt"]):
        conn.close()
        return None, "帳號或密碼錯誤"

    # 建立 Session Token
    token = secrets.token_hex(32)
    now = datetime.datetime.now()
    expires = (now + datetime.timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute("INSERT INTO sessions (token, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
                   (token, user["id"], expires, now_str))
    cursor.execute("UPDATE users SET last_login = ? WHERE id = ?", (now_str, user["id"]))
    conn.commit()
    conn.close()

    return {
        "token": token,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "name": user["name"],
            "role": user["role"]
        }
    }, None

def get_user_by_session(token):
    if not token:
        return None
    conn = get_db()
    cursor = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute("""
    SELECT u.id, u.email, u.name, u.role, u.is_active, u.email_verified, u.api_key
    FROM sessions s
    JOIN users u ON s.user_id = u.id
    WHERE s.token = ? AND s.expires_at > ? AND u.is_active = 1
    """, (token, now_str))
    user = cursor.fetchone()
    conn.close()
    if user:
        d = dict(user)
        raw_key = d.get("api_key", "") or ""
        d["has_custom_api_key"] = bool(raw_key.strip())
        d["api_key_masked"] = mask_key(raw_key)
        del d["api_key"] # 不外洩完整 key 給前端
        return d
    return None

def set_user_api_key(user_id, api_key):
    """使用者自訂 Google Gemini API Key"""
    conn = get_db()
    cursor = conn.cursor()
    clean_key = (api_key or "").strip()
    cursor.execute("UPDATE users SET api_key = ? WHERE id = ?", (clean_key, user_id))
    conn.commit()
    conn.close()
    return True

def change_user_password(user_id, current_password, new_password):
    """使用者修改自身登入密碼"""
    if not new_password or len(new_password) < 4:
        return False, "新密碼長度至少需 4 個字元以上！"

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, password_hash, salt, is_active FROM users WHERE id = ?", (user_id,))
    user = cursor.fetchone()
    if not user:
        conn.close()
        return False, "找不到該使用者帳號"

    if user["is_active"] != 1:
        conn.close()
        return False, "帳號已被停用"

    if not verify_password(current_password, user["password_hash"], user["salt"]):
        conn.close()
        return False, "目前密碼輸入錯誤，請重新確認！"

    new_hash, new_salt = hash_password(new_password)
    cursor.execute("UPDATE users SET password_hash = ?, salt = ? WHERE id = ?", (new_hash, new_salt, user_id))
    conn.commit()
    conn.close()
    return True, "密碼已成功更新！"

def get_user_api_key(user_id):
    """取得指定使用者的自訂 API Key"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT api_key FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row and row["api_key"]:
        return row["api_key"].strip()
    return ""

def delete_session(token):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    conn.close()

# ----------------- Admin 管理介面 -----------------

def set_admin_password(new_password="teams", email="admin@hawkeyetech.com.tw"):
    """更新或重設 Admin 密碼"""
    conn = get_db()
    cursor = conn.cursor()
    pwd_hash, salt = hash_password(new_password)
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
    row = cursor.fetchone()
    if row:
        cursor.execute("""
        UPDATE users
        SET password_hash = ?, salt = ?, is_active = 1, email_verified = 1
        WHERE id = ?
        """, (pwd_hash, salt, row["id"]))
        # 清除既有 session 迫使以新密碼登入
        cursor.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
        conn.commit()
        conn.close()
        return True, f"管理員帳號 ({email}) 密碼已成功設定為: {new_password}"
    else:
        cursor.execute("""
        INSERT INTO users (email, name, password_hash, salt, role, is_active, email_verified, created_at)
        VALUES (?, '系統管理員', ?, ?, 'admin', 1, 1, ?)
        """, (email, pwd_hash, salt, now_str))
        conn.commit()
        conn.close()
        return True, f"管理員帳號 ({email}) 已建立，密碼為: {new_password}"

def get_all_users():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT u.id, u.email, u.name, u.role, u.is_active, u.email_verified, u.api_key, u.created_at, u.last_login,
           (SELECT COUNT(*) FROM meetings m WHERE m.user_id = u.id) AS meeting_count
    FROM users u
    ORDER BY u.id DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["has_custom_api_key"] = bool((d.get("api_key") or "").strip())
        d["api_key_masked"] = mask_key(d.get("api_key") or "")
        del d["api_key"]
        result.append(d)
    return result

def toggle_user_active(user_id, active):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_active = ? WHERE id = ?", (1 if active else 0, user_id))
    conn.commit()
    conn.close()

def manually_verify_user(user_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET email_verified = 1, verify_token = NULL WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()

def delete_user(user_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()

# ----------------- 系統 API Key 資源池管理 (Key Pool) -----------------

def get_all_system_keys():
    """取得資源池中所有 API Key"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT id, api_key, note, is_active, usage_count, last_used_at, created_at
    FROM system_keys
    ORDER BY id ASC
    """)
    rows = cursor.fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["api_key_masked"] = mask_key(d["api_key"])
        result.append(d)
    return result

def add_system_key(api_key, note=""):
    """新增 Key 至資源池"""
    clean_key = (api_key or "").strip()
    if not clean_key:
        raise ValueError("API Key 不得為空！")
    conn = get_db()
    cursor = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
    INSERT INTO system_keys (api_key, note, is_active, usage_count, created_at)
    VALUES (?, ?, 1, 0, ?)
    """, (clean_key, (note or "").strip(), now_str))
    new_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return new_id

def delete_system_key(key_id):
    """自資源池刪除 Key"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM system_keys WHERE id = ?", (key_id,))
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def toggle_system_key(key_id, active):
    """啟用或停用指定 Key"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE system_keys SET is_active = ? WHERE id = ?", (1 if active else 0, key_id))
    conn.commit()
    conn.close()
    return True

def get_next_balanced_api_key(exclude_keys=None):
    """
    動態負載平衡 (Least-Used Dynamic Balancing):
    從啟用的系統 Key Pool 中選取使用次數最少 (usage_count 最低) 的 Key，
    次數相同時優先選取最久未使用 (或未曾使用) 者，分散並發請求。
    """
    conn = get_db()
    cursor = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    query = "SELECT id, api_key, usage_count FROM system_keys WHERE is_active = 1"
    params = []
    if exclude_keys:
        placeholders = ",".join(["?"] * len(exclude_keys))
        query += f" AND api_key NOT IN ({placeholders})"
        params.extend(exclude_keys)

    query += " ORDER BY usage_count ASC, (last_used_at IS NULL) DESC, last_used_at ASC LIMIT 1"
    cursor.execute(query, params)
    row = cursor.fetchone()

    if row:
        key_id = row["id"]
        api_key = row["api_key"]
        cursor.execute("UPDATE system_keys SET usage_count = usage_count + 1, last_used_at = ? WHERE id = ?",
                       (now_str, key_id))
        conn.commit()
        conn.close()
        return {"id": key_id, "api_key": api_key, "usage_count": row["usage_count"] + 1}

    conn.close()
    return None

# ----------------- 會議紀錄管理 -----------------

def save_meeting_record(meeting_data):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT OR REPLACE INTO meetings (
        id, user_id, title, date, duration_sec, duration_str,
        audio_file, summary_file, transcript_file, speakers_json,
        dialogue_count, profile_id, profile_name, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        meeting_data["id"],
        meeting_data["user_id"],
        meeting_data["title"],
        meeting_data["date"],
        meeting_data.get("duration_sec", 0),
        meeting_data.get("duration_str", "00:00"),
        meeting_data["audio_file"],
        meeting_data.get("summary_file"),
        meeting_data.get("transcript_file"),
        json.dumps(meeting_data.get("speakers", []), ensure_ascii=False),
        meeting_data.get("dialogue_count", 0),
        meeting_data.get("profile_id"),
        meeting_data.get("profile_name", ""),
        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ))
    conn.commit()
    conn.close()

def get_user_meetings(user_id, is_admin=False):
    conn = get_db()
    cursor = conn.cursor()
    if is_admin:
        cursor.execute("""
        SELECT m.*, u.email as owner_email, u.name as owner_name
        FROM meetings m
        JOIN users u ON m.user_id = u.id
        ORDER BY m.created_at DESC
        """)
    else:
        cursor.execute("""
        SELECT m.*, u.email as owner_email, u.name as owner_name
        FROM meetings m
        JOIN users u ON m.user_id = u.id
        WHERE m.user_id = ?
        ORDER BY m.created_at DESC
        """, (user_id,))
    rows = cursor.fetchall()
    conn.close()

    result = []
    for r in rows:
        d = dict(r)
        d["speakers"] = json.loads(d["speakers_json"]) if d["speakers_json"] else []
        del d["speakers_json"]
        result.append(d)
    return result

def delete_meeting(meeting_id, user_id, is_admin=False):
    conn = get_db()
    cursor = conn.cursor()
    if is_admin:
        cursor.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
    else:
        cursor.execute("DELETE FROM meetings WHERE id = ? AND user_id = ?", (meeting_id, user_id))
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def update_meeting_title(meeting_id, new_title, user_id, is_admin=False):
    conn = get_db()
    cursor = conn.cursor()
    if is_admin:
        cursor.execute("UPDATE meetings SET title = ? WHERE id = ?", (new_title.strip(), meeting_id))
    else:
        cursor.execute("UPDATE meetings SET title = ? WHERE id = ? AND user_id = ?", (new_title.strip(), meeting_id, user_id))
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def get_or_create_share_token(meeting_id, user_id, is_admin=False, expire_hours=24):
    """取得或產生會議的唯讀分享 Token，並設定有效到期時間"""
    conn = get_db()
    cursor = conn.cursor()
    if is_admin:
        cursor.execute("SELECT id, share_token, share_expires_at FROM meetings WHERE id = ?", (meeting_id,))
    else:
        cursor.execute("SELECT id, share_token, share_expires_at FROM meetings WHERE id = ? AND user_id = ?", (meeting_id, user_id))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None, None, "找不到該會議或無權限分享"

    existing_token = row["share_token"]
    existing_expires = row["share_expires_at"] or ""
    now = datetime.datetime.now()

    # 計算到期時間
    if expire_hours is not None:
        if expire_hours > 0:
            calc_expires = (now + datetime.timedelta(hours=expire_hours)).strftime("%Y-%m-%d %H:%M:%S")
        else:
            calc_expires = ""  # 0 代表永久有效
    else:
        if existing_expires:
            calc_expires = existing_expires
        else:
            calc_expires = (now + datetime.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")

    # 若已有 Token 則沿用原 Token 更新過期時間；若無則產生新 Token
    if existing_token and existing_token.strip():
        token = existing_token.strip()
    else:
        token = secrets.token_urlsafe(16)

    cursor.execute("UPDATE meetings SET share_token = ?, share_expires_at = ? WHERE id = ?", (token, calc_expires, meeting_id))
    conn.commit()
    conn.close()
    return token, calc_expires, None

def revoke_share_token(meeting_id, user_id, is_admin=False):
    """撤銷會議的分享 Token"""
    conn = get_db()
    cursor = conn.cursor()
    if is_admin:
        cursor.execute("UPDATE meetings SET share_token = '', share_expires_at = '' WHERE id = ?", (meeting_id,))
    else:
        cursor.execute("UPDATE meetings SET share_token = '', share_expires_at = '' WHERE id = ? AND user_id = ?", (meeting_id, user_id))
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def get_meeting_by_share_token(share_token):
    """透過分享 Token 取得會議基本資料 (公開唯讀，含過期檢查)"""
    if not share_token or not share_token.strip():
        return None, "缺少有效分享 Token"
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT m.*, u.name as owner_name, u.email as owner_email
    FROM meetings m
    JOIN users u ON m.user_id = u.id
    WHERE m.share_token = ? AND m.share_token != ''
    """, (share_token.strip(),))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None, "此分享連結無效、不存在或已被原作者取消分享"

    d = dict(row)
    expires_at = (d.get("share_expires_at") or "").strip()
    if expires_at:
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if now_str > expires_at:
            return None, f"此會議分享連結已於 {expires_at} 到期失效"

    if d.get("speakers_json"):
        try:
            d["speakers"] = json.loads(d["speakers_json"])
        except Exception:
            d["speakers"] = []
    else:
        d["speakers"] = []
    return d, None

def index_existing_meetings(meetings_base_dir):
    """將現有資料夾中的會議自動建檔索引至資料庫"""
    if not os.path.exists(meetings_base_dir):
        return
    time_pattern = re.compile(r'^\[(\d+(?::\d{2}){1,2})\]\s*(?:\[([^\]]+)\]|\(([^)]+)\)|([^：:\n\[\(]{1,30})[：:])?\s*(.*)$')
    
    for user_entry in os.listdir(meetings_base_dir):
        if user_entry.lower() in ('tobedelete', 'to_be_delete', '.git', 'temp', 'tmp'):
            continue
        user_folder = os.path.join(meetings_base_dir, user_entry)
        if not os.path.isdir(user_folder):
            continue
        try:
            user_id = int(user_entry)
        except ValueError:
            user_id = 1 # 預設分配給 Admin
        
        for m_folder_name in os.listdir(user_folder):
            if m_folder_name.lower() in ('tobedelete', 'to_be_delete', '.git', 'temp', 'tmp'):
                continue
            m_path = os.path.join(user_folder, m_folder_name)
            if not os.path.isdir(m_path):
                continue
            
            # 檢查是否已在 DB
            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT id FROM meetings WHERE id = ?", (m_folder_name,))
            if c.fetchone():
                conn.close()
                continue
            conn.close()

            audio_files = [f for f in os.listdir(m_path) if f.lower().endswith(('.mp3', '.m4a', '.wav', '.aac', '.flac'))]
            summary_files = [f for f in os.listdir(m_path) if ('摘要' in f or 'summary' in f.lower()) and f.endswith(('.txt', '.md'))]
            transcript_files = [f for f in os.listdir(m_path) if ('逐字稿' in f or 'transcript' in f.lower()) and f.endswith(('.txt', '.md'))]

            if not audio_files or not transcript_files:
                continue

            audio_file = audio_files[0]
            summary_file = summary_files[0] if summary_files else None
            transcript_file = transcript_files[0]

            summary_text = ""
            if summary_file:
                try:
                    with open(os.path.join(m_path, summary_file), "r", encoding="utf-8") as sf:
                        summary_text = sf.read()
                except Exception:
                    pass

            transcript_text = ""
            try:
                with open(os.path.join(m_path, transcript_file), "r", encoding="utf-8") as tf:
                    transcript_text = tf.read()
            except Exception:
                pass

            speakers = set()
            max_sec = 0
            dialogue_count = 0
            for line in transcript_text.splitlines():
                line = line.strip()
                if not line:
                    continue
                m = time_pattern.match(line)
                if m:
                    t_str, s1, s2, s3, txt = m.groups()
                    spk = (s1 or s2 or s3 or '').strip()
                    if spk:
                        speakers.add(spk)
                    parts = [int(x) for x in t_str.split(':')]
                    if len(parts) == 3:
                        sec = parts[0] * 3600 + parts[1] * 60 + parts[2]
                    elif len(parts) == 2:
                        sec = parts[0] * 60 + parts[1]
                    else:
                        sec = parts[0]
                    if sec > max_sec:
                        max_sec = sec
                    dialogue_count += 1

            if not speakers and dialogue_count > 0:
                speakers.add('說話者')

            dur_h = max_sec // 3600
            dur_m = (max_sec % 3600) // 60
            dur_s = max_sec % 60
            if dur_h > 0:
                dur_str = f"{dur_h:02d}:{dur_m:02d}:{dur_s:02d}"
            else:
                dur_str = f"{dur_m:02d}:{dur_s:02d}"

            # 智慧標題 (優先讀取自訂 title.txt)
            title = m_folder_name
            title_file = os.path.join(m_path, "title.txt")
            if os.path.exists(title_file):
                try:
                    with open(title_file, "r", encoding="utf-8") as tf:
                        t = tf.read().strip()
                        if t:
                            title = t
                except Exception:
                    pass
            elif "Dell" in summary_text or "Hawkeye" in summary_text:
                title = "Dell 與 Hawkeye (Foxconn) DPU 專案商務與樣品交付技術會議"
            elif "Ribbon" in summary_text or "RBBN" in m_folder_name.upper() or "6WIND" in summary_text:
                title = "Ribbon (RBBN) 團隊工程架構同步會議 (D2 / 6WIND / 8200 / 8500)"
            elif summary_text:
                for sl in summary_text.splitlines():
                    clean_sl = sl.strip().lstrip('#-*• \t')
                    if clean_sl and len(clean_sl) > 4 and "摘要" not in clean_sl:
                        title = clean_sl
                        break

            date_str = datetime.datetime.now().strftime("%Y/%m/%d")
            date_m = re.search(r'202\d[01]\d[0-3]\d', m_folder_name + ' ' + audio_file)
            if date_m:
                raw_d = date_m.group(0)
                date_str = f"{raw_d[:4]}/{raw_d[4:6]}/{raw_d[6:8]}"

            save_meeting_record({
                "id": m_folder_name,
                "user_id": user_id,
                "title": title,
                "date": date_str,
                "duration_sec": max_sec,
                "duration_str": dur_str,
                "audio_file": audio_file,
                "summary_file": summary_file,
                "transcript_file": transcript_file,
                "speakers": sorted(list(speakers)),
                "dialogue_count": dialogue_count
            })

# ==========================================================
# Teams Bot 預約排程與撥入任務資料庫操作
# ==========================================================

def create_bot_task(user_id, title, teams_url, scheduled_at, max_duration_min=60, bot_name="AI 會議記錄助理", notify_email="", profile_id=None):
    conn = get_db()
    cursor = conn.cursor()
    task_id = f"bot_{int(time.time() * 1000)}_{secrets.token_hex(4)}"
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
    INSERT INTO teams_bot_tasks (id, user_id, title, teams_url, bot_name, scheduled_at, max_duration_min, notify_email, profile_id, status, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', ?)
    """, (task_id, user_id, title.strip(), teams_url.strip(), bot_name.strip() or "AI 會議記錄助理", scheduled_at.strip(), max_duration_min, (notify_email or "").strip(), profile_id, now_str))
    conn.commit()
    conn.close()
    return get_bot_task(task_id)

def get_bot_task(task_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM teams_bot_tasks WHERE id = ?", (task_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def get_user_bot_tasks(user_id, is_admin=False):
    conn = get_db()
    cursor = conn.cursor()
    if is_admin:
        cursor.execute("SELECT t.*, u.name as user_name, u.email as user_email FROM teams_bot_tasks t JOIN users u ON t.user_id = u.id ORDER BY t.created_at DESC")
    else:
        cursor.execute("SELECT * FROM teams_bot_tasks WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_due_bot_tasks():
    conn = get_db()
    cursor = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("SELECT * FROM teams_bot_tasks WHERE status = 'scheduled' AND scheduled_at <= ? ORDER BY scheduled_at ASC", (now_str,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def update_bot_task(task_id, **kwargs):
    conn = get_db()
    cursor = conn.cursor()
    fields = []
    values = []
    for k, v in kwargs.items():
        fields.append(f"{k} = ?")
        values.append(v)
    values.append(task_id)
    cursor.execute(f"UPDATE teams_bot_tasks SET {', '.join(fields)} WHERE id = ?", tuple(values))
    conn.commit()
    conn.close()
    return get_bot_task(task_id)

def cancel_bot_task(task_id, user_id, is_admin=False):
    conn = get_db()
    cursor = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if is_admin:
        cursor.execute("UPDATE teams_bot_tasks SET status = 'cancelled', ended_at = ? WHERE id = ? AND status IN ('scheduled', 'joining', 'in_lobby', 'recording')", (now_str, task_id))
    else:
        cursor.execute("UPDATE teams_bot_tasks SET status = 'cancelled', ended_at = ? WHERE id = ? AND user_id = ? AND status IN ('scheduled', 'joining', 'in_lobby', 'recording')", (now_str, task_id, user_id))
    affected = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return affected

def delete_bot_task(task_id, user_id, is_admin=False):
    conn = get_db()
    cursor = conn.cursor()
    if is_admin:
        cursor.execute("DELETE FROM teams_bot_tasks WHERE id = ?", (task_id,))
    else:
        cursor.execute("DELETE FROM teams_bot_tasks WHERE id = ? AND user_id = ?", (task_id, user_id))
    affected = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return affected


# ----------------- 會議 Profile (提示詞範本 / 設定檔) -----------------

DEFAULT_PROFILES = [
    {
        "name": "👔 主管會議 / 營運週會",
        "speakers": "Joel, Iris, Steven, Eric, 董事長",
        "keywords": "瑞祺, 澔楷, 董事長, 營收, 毛利, 開票, 盤點, 職場霸凌, SMQ, 倉租",
        "location_context": "台灣總部會議室，部分人員遠端連線",
        "prompt_hint": "著重在各部門營運進度、決策方針、待追蹤事項與責任歸屬。",
        "is_default": 1
    },
    {
        "name": "🤝 客戶會議 (Ribbon / Adam)",
        "speakers": "Adam, Iris, Steven, Joel, Rumus",
        "keywords": "8300, 8200, 5512, QBR, Cost Gap 15%, Little Fuse, PPV, ICT, A-Team, 瑞祺, 澔楷, Rumus",
        "location_context": "跨國連線視訊會議 (台灣/美國)，中英雙語混合",
        "prompt_hint": "著重在客戶需求、QBR 行程安排、商務報價談判與技術架構決策。",
        "is_default": 0
    }
]

def init_user_default_profiles(user_id, cursor=None):
    """若使用者尚無任何 Profile，自動初始化預設範本"""
    need_close = False
    if cursor is None:
        conn = get_db()
        cursor = conn.cursor()
        need_close = True
    else:
        conn = None

    cursor.execute("SELECT COUNT(*) FROM meeting_profiles WHERE user_id = ?", (user_id,))
    count = cursor.fetchone()[0]
    if count == 0:
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for p in DEFAULT_PROFILES:
            cursor.execute("""
            INSERT INTO meeting_profiles (user_id, name, speakers, keywords, location_context, prompt_hint, is_default, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (user_id, p["name"], p["speakers"], p["keywords"], p["location_context"], p["prompt_hint"], p["is_default"], now_str, now_str))
        if conn:
            conn.commit()

    if need_close and conn:
        conn.close()

def get_user_profiles(user_id):
    """取得指定使用者的所有 Profile（若無則自動建置預設）"""
    init_user_default_profiles(user_id)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT id, user_id, name, speakers, keywords, location_context, prompt_hint, is_default, created_at, updated_at
    FROM meeting_profiles
    WHERE user_id = ?
    ORDER BY is_default DESC, id ASC
    """, (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_profile_by_id(profile_id, user_id=None):
    """根據 ID 取得特定 Profile"""
    if not profile_id:
        return None
    conn = get_db()
    cursor = conn.cursor()
    if user_id is not None:
        cursor.execute("SELECT * FROM meeting_profiles WHERE id = ? AND user_id = ?", (profile_id, user_id))
    else:
        cursor.execute("SELECT * FROM meeting_profiles WHERE id = ?", (profile_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def get_default_profile_for_user(user_id):
    """取得使用者的預設 Profile"""
    init_user_default_profiles(user_id)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM meeting_profiles WHERE user_id = ? AND is_default = 1 LIMIT 1", (user_id,))
    row = cursor.fetchone()
    if not row:
        cursor.execute("SELECT * FROM meeting_profiles WHERE user_id = ? ORDER BY id ASC LIMIT 1", (user_id,))
        row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def save_user_profile(user_id, profile_id, name, speakers="", keywords="", location_context="", prompt_hint="", is_default=0):
    """新增或修改使用者的 Profile"""
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("Profile 名稱不可為空")

    conn = get_db()
    cursor = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if is_default:
        cursor.execute("UPDATE meeting_profiles SET is_default = 0 WHERE user_id = ?", (user_id,))

    if profile_id:
        cursor.execute("""
        UPDATE meeting_profiles
        SET name = ?, speakers = ?, keywords = ?, location_context = ?, prompt_hint = ?, is_default = ?, updated_at = ?
        WHERE id = ? AND user_id = ?
        """, (clean_name, (speakers or "").strip(), (keywords or "").strip(), (location_context or "").strip(), (prompt_hint or "").strip(), 1 if is_default else 0, now_str, profile_id, user_id))
        target_id = profile_id
    else:
        cursor.execute("""
        INSERT INTO meeting_profiles (user_id, name, speakers, keywords, location_context, prompt_hint, is_default, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, clean_name, (speakers or "").strip(), (keywords or "").strip(), (location_context or "").strip(), (prompt_hint or "").strip(), 1 if is_default else 0, now_str, now_str))
        target_id = cursor.lastrowid

    conn.commit()
    conn.close()
    return target_id

def delete_user_profile(user_id, profile_id):
    """刪除指定 Profile"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM meeting_profiles WHERE id = ? AND user_id = ?", (profile_id, user_id))
    affected = cursor.rowcount
    cursor.execute("SELECT id FROM meeting_profiles WHERE user_id = ? AND is_default = 1", (user_id,))
    has_default = cursor.fetchone()
    if not has_default:
        cursor.execute("UPDATE meeting_profiles SET is_default = 1 WHERE id = (SELECT id FROM meeting_profiles WHERE user_id = ? ORDER BY id ASC LIMIT 1)", (user_id,))
    conn.commit()
    conn.close()
    return affected > 0

if __name__ == "__main__":
    init_db()
    print("Database initialized successfully at:", DB_PATH)
