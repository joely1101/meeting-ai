"""
會議錄音器 Client v2.0
- 純錄音無負擔架構：移除本機轉譯，由伺服器雲端 AI 處理
- 支援 Google (Gmail) 帳號快速登入認證
- 雙向無損混音錄音 (WASAPI Loopback + 麥克風)
- 錄音完成後一鍵自動/手動上傳至雲端伺服器
"""

import os
import sys
import re
import time
import json
import datetime
import threading
import urllib.parse
import urllib.request
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

# Windows 高 DPI 縮放適配
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

from audio_engine import AudioEngine

APP_VERSION = "2.0.0"
DEFAULT_SERVER_URL = "http://localhost:8080"
MUTE_OPTION = "🚫 [不錄音] 關閉此聲道 (靜音)"

# 1. 錄音檔存檔目錄：使用者家目錄下的「會議錄音」資料夾 (例如 C:\Users\Joel\會議錄音)
USER_HOME = os.path.expanduser("~")
RECORDINGS_DIR = os.path.join(USER_HOME, "會議錄音")
os.makedirs(RECORDINGS_DIR, exist_ok=True)

# 2. 客戶端設定檔路徑：存於 %APPDATA%\MeetingRecorder
APP_DATA_DIR = os.path.join(os.environ.get("APPDATA", USER_HOME), "MeetingRecorder")
os.makedirs(APP_DATA_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(APP_DATA_DIR, "client_config.json")

def detect_overlay_server_url():
    """嘗試從目前執行的 EXE 二進位尾端讀取 Overlay 注入的 URL"""
    try:
        exe_path = os.path.abspath(sys.argv[0])
        if os.path.exists(exe_path) and exe_path.lower().endswith(".exe"):
            with open(exe_path, "rb") as f:
                f.seek(max(0, os.path.getsize(exe_path) - 8192))
                tail_data = f.read()
                # 匹配 __SERVER_URL_OVERLAY__:(http...):__END__
                m = re.search(b'__SERVER_URL_OVERLAY__:(https?://.*?):__END__', tail_data)
                if m:
                    url = m.group(1).decode("utf-8", errors="ignore").strip().rstrip("/")
                    return url
    except Exception as e:
        print(f"Error detecting overlay URL: {e}")
    return None

def load_client_config():
    cfg = {
        "server_url": DEFAULT_SERVER_URL,
        "token": "",
        "user_email": "",
        "user_name": "",
        "auto_upload": True
    }

    # 1. 優先檢查同目錄下的 client_config.json (免安裝隨身攜帶包)
    local_cfg = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "client_config.json")
    if os.path.exists(local_cfg):
        try:
            with open(local_cfg, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
                if cfg.get("server_url"):
                    return cfg
        except Exception:
            pass

    # 2. 檢查 EXE 是否帶有 Overlay 注入的 URL (優先套用剛下載的伺服器位址)
    overlay_url = detect_overlay_server_url()
    if overlay_url:
        cfg["server_url"] = overlay_url

    # 3. 檢查 APPDATA (讀取 token 與個人偏好)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                appdata_cfg = json.load(f)
                for k in ["token", "user_name", "user_email", "auto_upload"]:
                    if k in appdata_cfg:
                        cfg[k] = appdata_cfg[k]
                # 若 EXE 內無 Overlay，才採用 APPDATA 記錄的歷史網址
                if not overlay_url and appdata_cfg.get("server_url"):
                    cfg["server_url"] = appdata_cfg["server_url"]
        except Exception:
            pass

    return cfg

def save_client_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving config: {e}")

class ModernMeter(tk.Canvas):
    """自訂雙聲道 VU 聲音音量指示計"""
    def __init__(self, parent, width=280, height=14, **kwargs):
        super().__init__(parent, width=width, height=height, bg="#1e293b", highlightthickness=0, **kwargs)
        self.width = width
        self.height = height
        self.level = 0.0

    def set_level(self, level):
        self.level = max(0.0, min(1.0, level))
        self.delete("all")
        if self.level <= 0.01:
            return

        w = int(self.width * self.level)
        if self.level < 0.65:
            color = "#10b981"
        elif self.level < 0.85:
            color = "#f59e0b"
        else:
            color = "#ef4444"

        self.create_rectangle(0, 0, w, self.height, fill=color, outline="")

class RecorderApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"會議錄音器 v{APP_VERSION} (純錄音·雲端AI轉譯)")
        self.root.geometry("820x760")
        self.root.minsize(760, 700)

        self.cfg = load_client_config()
        self.engine = AudioEngine()
        self.is_recording = False
        self.record_start_time = 0.0
        self.last_audio_file = None
        self.last_task_id = None

        self.spk_devices = []
        self.mic_devices = []

        self._init_theme()
        self._build_ui()
        self._refresh_devices()
        self._check_login_status_bg()
        self._schedule_meter_update()

    def _init_theme(self):
        style = ttk.Style()
        style.theme_use("clam")
        self.root.configure(bg="#f8fafc")

        style.configure(".", background="#f8fafc", foreground="#0f172a", font=("Segoe UI", 9))
        style.configure("Card.TFrame", background="#ffffff", relief="flat")
        style.configure("Header.TLabel", font=("Segoe UI", 12, "bold"), background="#ffffff")
        style.configure("Sub.TLabel", font=("Segoe UI", 9), foreground="#64748b", background="#ffffff")

    def _build_ui(self):
        # 1. 頂部深色導航列
        top_bar = tk.Frame(self.root, bg="#0f172a", height=52, padx=16)
        top_bar.pack(fill="x")

        title_frame = tk.Frame(top_bar, bg="#0f172a")
        title_frame.pack(side="left", pady=10)

        tk.Label(title_frame, text="🎙️ 會議錄音器", font=("Segoe UI", 11, "bold"), fg="#ffffff", bg="#0f172a").pack(side="left")
        tk.Label(title_frame, text="v2.0 雲端版", font=("Segoe UI", 8), fg="#38bdf8", bg="#0f172a", padx=6).pack(side="left")

        # 使用者狀態區
        self.user_bar_frame = tk.Frame(top_bar, bg="#0f172a")
        self.user_bar_frame.pack(side="right", pady=10)

        self.user_status_label = tk.Label(self.user_bar_frame, text="檢查登入中...", font=("Segoe UI", 9), fg="#94a3b8", bg="#0f172a")
        self.user_status_label.pack(side="left", padx=8)

        self.auth_btn = tk.Button(self.user_bar_frame, text="登入帳號", font=("Segoe UI", 8, "bold"),
                                  bg="#2563eb", fg="#ffffff", activebackground="#1d4ed8", activeforeground="#ffffff",
                                  relief="flat", padx=10, pady=2, cursor="hand2", command=self.on_auth_btn_clicked)
        self.auth_btn.pack(side="left")

        # 主體內容區
        main_container = tk.Frame(self.root, bg="#f8fafc", padx=18, pady=12)
        main_container.pack(fill="both", expand=True)

        # 伺服器連線資訊條
        server_bar = tk.Frame(main_container, bg="#e2e8f0", padx=10, pady=4)
        server_bar.pack(fill="x", pady=(0, 10))

        tk.Label(server_bar, text="🌐 伺服器網址:", font=("Segoe UI", 8, "bold"), bg="#e2e8f0", fg="#475569").pack(side="left")
        self.server_url_display = tk.Label(server_bar, text=self.cfg.get("server_url", DEFAULT_SERVER_URL),
                                           font=("Segoe UI", 8), bg="#e2e8f0", fg="#2563eb")
        self.server_url_display.pack(side="left", padx=6)

        tk.Button(server_bar, text="變更網址", font=("Segoe UI", 8), bg="#ffffff", fg="#475569",
                  relief="flat", padx=6, pady=1, cursor="hand2", command=self.open_server_url_dialog).pack(side="right")

        # 卡片 1: 音訊輸入與輸出裝置設定
        card_dev = tk.Frame(main_container, bg="#ffffff", bd=1, relief="solid", padx=16, pady=12)
        card_dev.pack(fill="x", pady=(0, 10))

        tk.Label(card_dev, text="音訊裝置設定 (支援雙向無損混音)", font=("Segoe UI", 10, "bold"), bg="#ffffff").pack(anchor="w")

        dev_grid = tk.Frame(card_dev, bg="#ffffff", pady=8)
        dev_grid.pack(fill="x")

        # 揚聲器選擇
        tk.Label(dev_grid, text="對方說話聲音 (系統揚聲器/耳機):", font=("Segoe UI", 9), bg="#ffffff").grid(row=0, column=0, sticky="w", pady=4)
        self.spk_combo = ttk.Combobox(dev_grid, state="readonly", width=45)
        self.spk_combo.grid(row=0, column=1, sticky="ew", padx=8, pady=4)
        self.spk_meter = ModernMeter(dev_grid, width=160, height=12)
        self.spk_meter.grid(row=0, column=2, padx=4, pady=4)

        # 麥克風選擇
        tk.Label(dev_grid, text="自己發言聲音 (本機麥克風):", font=("Segoe UI", 9), bg="#ffffff").grid(row=1, column=0, sticky="w", pady=4)
        self.mic_combo = ttk.Combobox(dev_grid, state="readonly", width=45)
        self.mic_combo.grid(row=1, column=1, sticky="ew", padx=8, pady=4)
        self.mic_meter = ModernMeter(dev_grid, width=160, height=12)
        self.mic_meter.grid(row=1, column=2, padx=4, pady=4)

        dev_grid.columnconfigure(1, weight=1)

        refresh_btn = tk.Button(card_dev, text="🔄 重新整理裝置清單", font=("Segoe UI", 8),
                                bg="#f1f5f9", fg="#475569", relief="flat", padx=8, pady=3, command=self._refresh_devices)
        refresh_btn.pack(anchor="e")

        # 卡片 2: 錄音控制核心卡片
        card_rec = tk.Frame(main_container, bg="#ffffff", bd=1, relief="solid", padx=16, pady=16)
        card_rec.pack(fill="x", pady=(0, 10))

        # 會議自訂標題輸入
        title_box = tk.Frame(card_rec, bg="#ffffff")
        title_box.pack(fill="x", pady=(0, 12))

        tk.Label(title_box, text="會議標題 (可選):", font=("Segoe UI", 9, "bold"), bg="#ffffff").pack(side="left")
        self.title_entry = tk.Entry(title_box, font=("Segoe UI", 10), relief="solid", bd=1)
        self.title_entry.pack(side="left", fill="x", expand=True, padx=(8, 0))

        # 計時器大字
        self.timer_label = tk.Label(card_rec, text="00:00:00", font=("Consolas", 32, "bold"), fg="#64748b", bg="#ffffff")
        self.timer_label.pack(pady=4)

        # 錄音主按鈕
        self.record_btn = tk.Button(card_rec, text="⏺  開始會議錄音", font=("Segoe UI", 12, "bold"),
                                    bg="#ef4444", fg="#ffffff", activebackground="#dc2626", activeforeground="#ffffff",
                                    relief="flat", padx=24, pady=10, cursor="hand2", command=self.toggle_recording)
        self.record_btn.pack(pady=8)

        # 自動上傳勾選框
        self.auto_upload_var = tk.BooleanVar(value=self.cfg.get("auto_upload", True))
        chk = tk.Checkbutton(card_rec, text="錄音結束後自動上傳至伺服器並啟動雲端 AI 轉錄",
                             variable=self.auto_upload_var, command=self.on_auto_upload_toggled,
                             font=("Segoe UI", 9), bg="#ffffff", activebackground="#ffffff")
        chk.pack(pady=4)

        # 卡片 3: 上傳狀態與雲端進度
        card_status = tk.Frame(main_container, bg="#ffffff", bd=1, relief="solid", padx=16, pady=12)
        card_status.pack(fill="both", expand=True)

        tk.Label(card_status, text="即時狀態與上傳進度", font=("Segoe UI", 10, "bold"), bg="#ffffff").pack(anchor="w")

        self.status_msg_label = tk.Label(card_status, text="準備就緒，請點擊「開始會議錄音」",
                                         font=("Segoe UI", 9), fg="#475569", bg="#ffffff", wraplength=700, justify="left")
        self.status_msg_label.pack(anchor="w", pady=(6, 8))

        self.progress_bar = ttk.Progressbar(card_status, mode="determinate", length=400)
        self.progress_bar.pack(fill="x", pady=(0, 10))

        btn_row = tk.Frame(card_status, bg="#ffffff")
        btn_row.pack(fill="x")

        self.upload_manual_btn = tk.Button(btn_row, text="☁️ 手動上傳最後錄音檔", font=("Segoe UI", 9),
                                           bg="#f1f5f9", fg="#1e293b", relief="flat", padx=12, pady=6,
                                           state="disabled", command=self.manual_upload)
        self.upload_manual_btn.pack(side="left", padx=(0, 8))

        self.open_folder_btn = tk.Button(btn_row, text="📁 開啟本機錄音資料夾", font=("Segoe UI", 9),
                                         bg="#f1f5f9", fg="#1e293b", relief="flat", padx=12, pady=6,
                                         command=self.open_local_folder)
        self.open_folder_btn.pack(side="left", padx=(0, 8))

        self.web_portal_btn = tk.Button(btn_row, text="🌐 開啟網頁會議儀表板", font=("Segoe UI", 9),
                                        bg="#f1f5f9", fg="#2563eb", relief="flat", padx=12, pady=6,
                                        command=self.open_web_portal)
        self.web_portal_btn.pack(side="right")

    def open_server_url_dialog(self):
        """設定伺服器連線網址"""
        dlg = tk.Toplevel(self.root)
        dlg.title("設定伺服器網址")
        dlg.geometry("420x200")
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.configure(bg="#ffffff", padx=20, pady=16)

        tk.Label(dlg, text="伺服器連線網址 (PUBLIC_URL):", font=("Segoe UI", 10, "bold"), bg="#ffffff").pack(anchor="w")
        entry = tk.Entry(dlg, font=("Segoe UI", 10), relief="solid", bd=1)
        entry.pack(fill="x", pady=(6, 12))
        entry.insert(0, self.cfg.get("server_url", DEFAULT_SERVER_URL))

        def save_url():
            u = entry.get().strip().rstrip("/")
            if not u.startswith("http://") and not u.startswith("https://"):
                messagebox.showerror("格式錯誤", "網址必須以 http:// 或 https:// 開頭！")
                return
            self.cfg["server_url"] = u
            save_client_config(self.cfg)
            self.server_url_display.config(text=u)
            dlg.destroy()
            self._check_login_status_bg()
            messagebox.showinfo("成功", f"伺服器網址已更新為:\n{u}")

        btn_row = tk.Frame(dlg, bg="#ffffff")
        btn_row.pack(fill="x", pady=(8, 0))
        tk.Button(btn_row, text="儲存網址", font=("Segoe UI", 9, "bold"), bg="#2563eb", fg="#ffffff",
                  relief="flat", padx=14, pady=6, command=save_url).pack(side="left")
        tk.Button(btn_row, text="取消", font=("Segoe UI", 9), bg="#f1f5f9", relief="flat", padx=12, pady=6,
                  command=dlg.destroy).pack(side="right")

    def _refresh_devices(self):
        try:
            self.spk_devices, self.mic_devices = self.engine.get_devices()

            spk_names = [d["name"] for d in self.spk_devices] + [MUTE_OPTION]
            mic_names = [d["name"] for d in self.mic_devices] + [MUTE_OPTION]

            self.spk_combo["values"] = spk_names
            self.mic_combo["values"] = mic_names

            if self.spk_devices:
                self.spk_combo.current(0)
            else:
                self.spk_combo.set(MUTE_OPTION)

            if self.mic_devices:
                self.mic_combo.current(0)
            else:
                self.mic_combo.set(MUTE_OPTION)
        except Exception as e:
            messagebox.showerror("裝置列舉失敗", f"無法列出音訊裝置: {e}")

    def _schedule_meter_update(self):
        if self.is_recording:
            spk_lvl = getattr(self.engine, "spk_level", 0.0)
            mic_lvl = getattr(self.engine, "mic_level", 0.0)
            self.spk_meter.set_level(spk_lvl)
            self.mic_meter.set_level(mic_lvl)
        else:
            self.spk_meter.set_level(0.0)
            self.mic_meter.set_level(0.0)
        self.root.after(40, self._schedule_meter_update)

    def _check_login_status_bg(self):
        token = self.cfg.get("token")
        server_url = self.cfg.get("server_url", DEFAULT_SERVER_URL).rstrip("/")

        if not token:
            self._update_user_ui_unauth()
            return

        def worker():
            api_url = f"{server_url}/api/auth/me"
            try:
                req = urllib.request.Request(api_url, headers={"Authorization": f"Bearer {token}"})
                with urllib.request.urlopen(req, timeout=5) as res:
                    data = json.loads(res.read().decode("utf-8"))
                    if data.get("status") == "ok":
                        self.root.after(0, lambda: self._update_user_ui_authed(data["user"]))
                        return
            except Exception:
                pass
            self.root.after(0, self._update_user_ui_unauth)

        threading.Thread(target=worker, daemon=True).start()

    def _update_user_ui_authed(self, user):
        name = user.get("name", "同仁")
        email = user.get("email", "")
        self.user_status_label.config(text=f"👤 {name} ({email})", fg="#38bdf8")
        self.auth_btn.config(text="登出", bg="#334155", activebackground="#475569")

    def _update_user_ui_unauth(self):
        self.user_status_label.config(text="⚠️ 尚未登入", fg="#f59e0b")
        self.auth_btn.config(text="Google 登入", bg="#2563eb", activebackground="#1d4ed8")

    def on_auto_upload_toggled(self):
        self.cfg["auto_upload"] = self.auto_upload_var.get()
        save_client_config(self.cfg)

    def open_web_portal(self):
        url = self.cfg.get("server_url", DEFAULT_SERVER_URL)
        webbrowser.open(url)

    def on_auth_btn_clicked(self):
        if self.cfg.get("token"):
            if messagebox.askyesno("登出確認", "確定要登出目前帳號嗎？"):
                self.cfg["token"] = ""
                self.cfg["user_name"] = ""
                self.cfg["user_email"] = ""
                save_client_config(self.cfg)
                self._update_user_ui_unauth()
        else:
            self.open_login_dialog()

    def open_login_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("登入會議系統")
        dlg.geometry("450x360")
        dlg.minsize(420, 340)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.configure(bg="#ffffff", padx=24, pady=20)

        tk.Label(dlg, text="同仁帳號登入", font=("Segoe UI", 13, "bold"), bg="#ffffff").pack(anchor="w", pady=(0, 6))
        tk.Label(dlg, text="點擊下方按鈕以 Google (Gmail) 帳號快速登入：",
                 font=("Segoe UI", 9), fg="#64748b", bg="#ffffff").pack(anchor="w", pady=(0, 14))

        status_label = tk.Label(dlg, text="", font=("Segoe UI", 9), fg="#2563eb", bg="#ffffff", wraplength=400)
        status_label.pack(anchor="w", pady=(0, 10))

        # Google 登入按鈕
        def start_google_login():
            server_url = self.cfg.get("server_url", DEFAULT_SERVER_URL).rstrip("/")
            status_label.config(text="正在建立登入連線，請稍候...", fg="#2563eb")
            dlg.update()

            def ticket_worker():
                try:
                    create_url = f"{server_url}/api/auth/client_ticket/create"
                    req = urllib.request.Request(create_url, data=b"{}", headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=8) as res:
                        data = json.loads(res.read().decode("utf-8"))

                    ticket = data.get("ticket")
                    login_url = data.get("login_url")
                    if not ticket or not login_url:
                        raise ValueError("伺服器回傳票據格式錯誤")

                    # 開啟瀏覽器進行 Google 授權
                    webbrowser.open(login_url)
                    dlg.after(0, lambda: status_label.config(
                        text="已在瀏覽器開啟 Google 授權頁！\n請於瀏覽器登入完成後返回本程式...", fg="#10b981"))

                    # 背景輪詢票據
                    poll_url = f"{server_url}/api/auth/client_ticket/poll?ticket={urllib.parse.quote(ticket)}"
                    for _ in range(120): # 最多等待 2 分鐘
                        time.sleep(1.5)
                        try:
                            p_req = urllib.request.Request(poll_url)
                            with urllib.request.urlopen(p_req, timeout=5) as p_res:
                                p_data = json.loads(p_res.read().decode("utf-8"))
                                if p_data.get("status") == "success":
                                    token = p_data["token"]
                                    user = p_data["user"]
                                    self.cfg["token"] = token
                                    self.cfg["user_name"] = user.get("name", "")
                                    self.cfg["user_email"] = user.get("email", "")
                                    save_client_config(self.cfg)

                                    dlg.after(0, lambda: [
                                        self._update_user_ui_authed(user),
                                        dlg.destroy(),
                                        messagebox.showinfo("登入成功", f"歡迎回來，{user.get('name')}！")
                                    ])
                                    return
                                elif p_data.get("status") == "expired":
                                    dlg.after(0, lambda: status_label.config(text="登入連線逾時，請重新點擊登入。", fg="#ef4444"))
                                    return
                        except Exception:
                            pass
                except Exception as ex:
                    dlg.after(0, lambda: status_label.config(text=f"無法連接伺服器: {ex}", fg="#ef4444"))

            threading.Thread(target=ticket_worker, daemon=True).start()

        google_btn = tk.Button(dlg, text="🌐 使用 Google 帳號一鍵登入", font=("Segoe UI", 10, "bold"),
                               bg="#ffffff", fg="#1e293b", activebackground="#f8fafc", relief="solid", bd=1,
                               padx=16, pady=10, cursor="hand2", command=start_google_login)
        google_btn.pack(fill="x", pady=(0, 16))

        # 手動輸入 Token 或伺服器應急
        sep_frame = tk.Frame(dlg, bg="#ffffff")
        sep_frame.pack(fill="x", pady=(0, 10))
        tk.Label(sep_frame, text="──── 或手動貼上網頁登入 Token ────", font=("Segoe UI", 8), fg="#94a3b8", bg="#ffffff").pack()

        token_entry = tk.Entry(dlg, font=("Segoe UI", 9), relief="solid", bd=1)
        token_entry.pack(fill="x", pady=(0, 10))

        def save_token_manual():
            tk_val = token_entry.get().strip()
            if not tk_val:
                messagebox.showwarning("提示", "請輸入 Token！")
                return
            self.cfg["token"] = tk_val
            save_client_config(self.cfg)
            self._check_login_status_bg()
            dlg.destroy()
            messagebox.showinfo("已套用", "憑證已儲存！")

        tk.Button(dlg, text="套用 Token", font=("Segoe UI", 8), bg="#f1f5f9", relief="flat",
                  padx=10, pady=4, command=save_token_manual).pack(anchor="e")

    # ====== 錄音控制 ======
    def toggle_recording(self):
        if not self.is_recording:
            self.start_recording()
        else:
            self.stop_recording()

    def start_recording(self):
        spk_idx = self.spk_combo.current()
        mic_idx = self.mic_combo.current()
        spk_val = self.spk_combo.get()
        mic_val = self.mic_combo.get()

        spk_dev = None
        if spk_val != MUTE_OPTION and 0 <= spk_idx < len(self.spk_devices):
            spk_dev = self.spk_devices[spk_idx]

        mic_dev = None
        if mic_val != MUTE_OPTION and 0 <= mic_idx < len(self.mic_devices):
            mic_dev = self.mic_devices[mic_idx]

        if not spk_dev and not mic_dev:
            messagebox.showwarning("裝置設定錯誤", "揚聲器與麥克風不可同時設定為「不錄音」！\n請至少選擇一個音訊來源進行錄製。")
            return

        self.record_btn.config(state="disabled")
        self.status_msg_label.config(text="正在啟動音訊錄製引擎...", fg="#2563eb")

        def start_worker():
            success, msg = self.engine.start_recording(spk_dev, mic_dev)
            if success:
                self.is_recording = True
                self.record_start_time = time.time()
                self.root.after(0, self._on_recording_started)
            else:
                self.root.after(0, lambda m=msg: [
                    self.record_btn.config(state="normal"),
                    messagebox.showerror("啟動失敗", f"無法啟動音訊錄製裝置: {m}"),
                    self.status_msg_label.config(text="錄音啟動失敗", fg="#ef4444")
                ])

        threading.Thread(target=start_worker, daemon=True).start()

    def _on_recording_started(self):
        self.record_btn.config(text="⏹  停止會議錄音", bg="#334155", state="normal")
        self.timer_label.config(fg="#ef4444")
        self.status_msg_label.config(text="錄音進行中... 結束時將自動匯出 MP3 並上傳", fg="#10b981")
        self.spk_combo.config(state="disabled")
        self.mic_combo.config(state="disabled")
        self.upload_manual_btn.config(state="disabled")
        self._update_timer()

    def _update_timer(self):
        if self.is_recording:
            elapsed = int(time.time() - self.record_start_time)
            h = elapsed // 3600
            m = (elapsed % 3600) // 60
            s = elapsed % 60
            self.timer_label.config(text=f"{h:02d}:{m:02d}:{s:02d}")
            self.root.after(500, self._update_timer)

    def stop_recording(self):
        self.record_btn.config(state="disabled")
        self.status_msg_label.config(text="正在停止錄音並匯出 MP3 音訊檔...", fg="#2563eb")
        self.is_recording = False

        now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        user_title = self.title_entry.get().strip()
        if user_title:
            clean_title = re.sub(r'[^a-zA-Z0-9_\u4e00-\u9fa5]', '_', user_title)[:30]
            filename = f"會議錄音_{now_str}_{clean_title}.mp3"
        else:
            filename = f"會議錄音_{now_str}.mp3"

        out_path = os.path.join(RECORDINGS_DIR, filename)

        def stop_worker():
            try:
                self.engine.stop_recording()
                self.engine.mix_and_export_mp3(
                    out_path,
                    progress_cb=lambda p, msg: self.root.after(0, lambda pct=int(p * 100), m=msg: [
                        self.progress_bar.config(value=pct),
                        self.status_msg_label.config(text=f"匯出 MP3 ({pct}%): {m}", fg="#2563eb")
                    ])
                )
                self.last_audio_file = out_path
            except Exception as e:
                print(f"Error mixing and exporting MP3: {e}")
                self.last_audio_file = None
                self.root.after(0, lambda err=e: self.status_msg_label.config(text=f"匯出失敗: {err}", fg="#ef4444"))
            finally:
                self.root.after(0, self._on_recording_stopped)

        threading.Thread(target=stop_worker, daemon=True).start()

    def _on_recording_stopped(self):
        self.record_btn.config(text="⏺  開始會議錄音", bg="#ef4444", state="normal")
        self.timer_label.config(fg="#64748b")
        self.spk_combo.config(state="readonly")
        self.mic_combo.config(state="readonly")
        self.upload_manual_btn.config(state="normal")

        if self.last_audio_file and os.path.exists(self.last_audio_file):
            size_mb = os.path.getsize(self.last_audio_file) / (1024 * 1024)
            self.status_msg_label.config(text=f"錄音已儲存本機 ({size_mb:.2f} MB):\n{self.last_audio_file}", fg="#1e293b")

            if self.auto_upload_var.get():
                self.do_upload(self.last_audio_file)
        else:
            self.status_msg_label.config(text="錄音完成，但未偵測到音訊檔", fg="#f59e0b")

    def manual_upload(self):
        if not self.last_audio_file or not os.path.exists(self.last_audio_file):
            messagebox.showinfo("提示", "目前無最近錄音檔案可供上傳！")
            return
        self.do_upload(self.last_audio_file)

    def do_upload(self, file_path):
        token = self.cfg.get("token")
        if not token:
            res = messagebox.askyesno("尚未登入", "尚未登入帳號，無法將音訊上傳至雲端會議庫。\n是否立即登入？")
            if res:
                self.open_login_dialog()
            return

        server_url = self.cfg.get("server_url", DEFAULT_SERVER_URL).rstrip("/")
        user_title = self.title_entry.get().strip()

        self.upload_manual_btn.config(state="disabled")
        self.record_btn.config(state="disabled")
        self.status_msg_label.config(text="正在將音訊上傳至伺服器...", fg="#2563eb")
        self.progress_bar["value"] = 10

        def upload_worker():
            try:
                filename = os.path.basename(file_path)
                file_size = os.path.getsize(file_path)

                api_url = f"{server_url}/api/upload"
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(file_size),
                    "X-Filename": urllib.parse.quote(filename),
                    "X-Meeting-Title": urllib.parse.quote(user_title)
                }

                with open(file_path, "rb") as f:
                    req = urllib.request.Request(api_url, data=f, headers=headers, method="POST")
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        res_data = json.loads(resp.read().decode("utf-8"))

                if res_data.get("status") == "ok":
                    task_id = res_data.get("taskId")
                    self.last_task_id = task_id
                    self.root.after(0, lambda: [
                        self.status_msg_label.config(text="音訊上傳完成！雲端 AI 正在轉譯逐字稿與會議重點整理...", fg="#10b981"),
                        self.progress_bar.configure(value=40),
                        self.record_btn.config(state="normal"),
                        self.upload_manual_btn.config(state="normal")
                    ])
                    # 開始輪詢轉錄狀態
                    self._poll_task_status(task_id)
                else:
                    msg = res_data.get("message", "上傳失敗")
                    self.root.after(0, lambda: [
                        self.status_msg_label.config(text=f"上傳失敗: {msg}", fg="#ef4444"),
                        self.record_btn.config(state="normal"),
                        self.upload_manual_btn.config(state="normal"),
                        self.progress_bar.configure(value=0)
                    ])
            except Exception as e:
                self.root.after(0, lambda: [
                    self.status_msg_label.config(text=f"連線上傳失敗: {e}", fg="#ef4444"),
                    self.record_btn.config(state="normal"),
                    self.upload_manual_btn.config(state="normal"),
                    self.progress_bar.configure(value=0)
                ])

        threading.Thread(target=upload_worker, daemon=True).start()

    def _poll_task_status(self, task_id):
        server_url = self.cfg.get("server_url", DEFAULT_SERVER_URL).rstrip("/")
        status_url = f"{server_url}/api/task_status?taskId={task_id}"

        def poll_worker():
            for _ in range(60): # 輪詢 5 分鐘
                time.sleep(5)
                try:
                    req = urllib.request.Request(status_url)
                    with urllib.request.urlopen(req, timeout=5) as res:
                        data = json.loads(res.read().decode("utf-8"))
                        st = data.get("status")
                        prog = data.get("progress", 50)
                        msg = data.get("message", "")

                        if st == "done":
                            self.root.after(0, lambda: [
                                self.progress_bar.configure(value=100),
                                self.status_msg_label.config(text="🎉 雲端 AI 轉錄與重點摘要已完成！點擊右方可開啟網頁查看。", fg="#10b981")
                            ])
                            return
                        elif st == "error":
                            err_msg = data.get("error", "轉錄錯誤")
                            self.root.after(0, lambda: [
                                self.status_msg_label.config(text=f"轉錄失敗: {err_msg}", fg="#ef4444"),
                                self.progress_bar.configure(value=0)
                            ])
                            return
                        else:
                            self.root.after(0, lambda p=prog, m=msg: [
                                self.progress_bar.configure(value=p),
                                self.status_msg_label.config(text=f"AI 雲端處理中 ({p}%): {m}", fg="#2563eb")
                            ])
                except Exception:
                    pass

        threading.Thread(target=poll_worker, daemon=True).start()

    def open_local_folder(self):
        try:
            os.startfile(RECORDINGS_DIR)
        except Exception as e:
            messagebox.showerror("開啟失敗", f"無法開啟本機資料夾: {e}")

def main():
    root = tk.Tk()
    app = RecorderApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
