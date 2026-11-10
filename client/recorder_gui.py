"""
Hawkeye 會議錄音器 Client v2.0
- 純錄音無負擔架構：移除本機轉譯，由伺服器雲端 AI 處理
- 企業帳號登入整合 (@hawkeyetech.com.tw)
- 雙向無損混音錄音 (WASAPI Loopback + 麥克風)
- 錄音完成後一鍵自動/手動上傳至雲端伺服器
"""

import os
import sys
import time
import json
import datetime
import tempfile
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
try:
    from version import BUILD_VERSION
except Exception:
    BUILD_VERSION = "2026/11/05-19:15"

DEFAULT_SERVER_URL = "https://tools.jumper.cloudns.ch/recordv2"
MUTE_OPTION = "🚫 [不錄音] 關閉此聲道 (靜音)"

# 1. 錄音檔存檔目錄：使用者家目錄下的「會議錄音」資料夾 (例如 C:\Users\Joel\會議錄音)
USER_HOME = os.path.expanduser("~")
RECORDINGS_DIR = os.path.join(USER_HOME, "會議錄音")
os.makedirs(RECORDINGS_DIR, exist_ok=True)

# 2. 客戶端設定檔路徑：存於 %APPDATA%\HawkeyeRecorder，確保 EXE 重啟或單一執行檔解壓後仍長久記住登入狀態
APP_DATA_DIR = os.path.join(os.environ.get("APPDATA", USER_HOME), "HawkeyeRecorder")
os.makedirs(APP_DATA_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(APP_DATA_DIR, "client_config.json")

def load_client_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    # 嘗試遷移同目錄下的舊設定 (若有的話)
    local_cfg = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "client_config.json")
    if os.path.exists(local_cfg):
        try:
            with open(local_cfg, "r", encoding="utf-8") as f:
                data = json.load(f)
                save_client_config(data)
                return data
        except Exception:
            pass
    return {
        "server_url": DEFAULT_SERVER_URL,
        "token": "",
        "user_email": "",
        "user_name": "",
        "auto_upload": True
    }

def save_client_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving config: {e}")

class ModernMeter(tk.Canvas):
    """自訂雙聲道動態 VU 音量電平計 (含綠/黃/紅三段色彩與最佳飽和虛線參考)"""
    def __init__(self, parent, width=160, height=14, **kwargs):
        super().__init__(parent, width=width, height=height, bg="#e2e8f0", highlightthickness=1, highlightbackground="#cbd5e1", **kwargs)
        self.width = width
        self.height = height
        self.level = 0.0

    def set_level(self, level, lbl_widget=None):
        self.level = max(0.0, min(1.0, level))
        self.delete("all")
        w = self.winfo_width() if self.winfo_width() > 1 else self.width
        h = self.winfo_height() if self.winfo_height() > 1 else self.height

        fill_w = int(w * self.level)
        if fill_w > 0:
            if self.level > 0.85:
                color = "#dc2626"  # 紅色 (過載/破音警示)
            elif self.level > 0.65:
                color = "#d97706"  # 金黃色 (最佳飽和區)
            else:
                color = "#16a34a"  # 綠色 (正常)
            self.create_rectangle(0, 0, fill_w, h, fill=color, outline="")

        # 繪製最佳收音建議區間參考虛線 (65% ~ 85%)
        ref_x1 = int(w * 0.65)
        ref_x2 = int(w * 0.85)
        self.create_line(ref_x1, 0, ref_x1, h, fill="#94a3b8", dash=(2, 2))
        self.create_line(ref_x2, 0, ref_x2, h, fill="#f87171", dash=(2, 2))

        if lbl_widget:
            pct_int = int(self.level * 100)
            if pct_int > 85:
                lbl_widget.config(text=f"{pct_int}% ⚠️", fg="#dc2626")
            elif pct_int > 65:
                lbl_widget.config(text=f"{pct_int}% ★", fg="#d97706")
            else:
                lbl_widget.config(text=f"{pct_int}%", fg="#555555")

class RecorderApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"Hawkeye 會議錄音器 [v{BUILD_VERSION}] (純錄音·雲端AI轉譯)")
        self.root.geometry("840x820")
        self.root.minsize(760, 700)

        self.cfg = load_client_config()
        self.engine = AudioEngine()
        self.is_recording = False
        self.record_start_time = 0.0
        self.last_audio_file = None
        self.last_task_id = None

        self.spk_gain_var = tk.DoubleVar(value=1.0)
        self.mic_gain_var = tk.DoubleVar(value=1.0)

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

        tk.Label(title_frame, text="🎙️ Hawkeye 會議錄音器", font=("Segoe UI", 11, "bold"), fg="#ffffff", bg="#0f172a").pack(side="left")
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

        # 主體內容區 (適度邊界留白，留出足夠垂直空間)
        main_container = tk.Frame(self.root, bg="#f8fafc", padx=18, pady=12)
        main_container.pack(fill="both", expand=True)

        self._check_and_render_recovery_banner(main_container)

        # 2. 會議主題資訊卡片
        info_card = tk.LabelFrame(main_container, text=" 會議主題設定 ", font=("Segoe UI", 9, "bold"),
                                  bg="#ffffff", fg="#334155", padx=14, pady=10, relief="groove")
        info_card.pack(fill="x", pady=(0, 8))

        tk.Label(info_card, text="會議名稱 / 主題：", font=("Segoe UI", 9, "bold"), bg="#ffffff").grid(row=0, column=0, sticky="w", pady=4)
        self.title_entry = tk.Entry(info_card, font=("Segoe UI", 10), bg="#f8fafc", relief="solid", bd=1)
        self.title_entry.grid(row=0, column=1, sticky="ew", padx=(8, 0), pady=4)
        self.title_entry.insert(0, f"會議錄音_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}")
        info_card.columnconfigure(1, weight=1)

        # 3. 裝置選取與音量計卡片
        dev_card = tk.LabelFrame(main_container, text=" 音訊硬體與即時動態監測 (未錄音時即可試音調整收音) ", font=("Segoe UI", 9, "bold"),
                                 bg="#ffffff", fg="#334155", padx=14, pady=8, relief="groove")
        dev_card.pack(fill="x", pady=(0, 8))
        dev_card.columnconfigure(1, weight=1)

        # 揚聲器 (系統音訊)
        tk.Label(dev_card, text="🔊 揚聲器 (對方):", font=("Segoe UI", 9, "bold"), bg="#ffffff").grid(row=0, column=0, sticky="w", pady=2)
        self.spk_combo = ttk.Combobox(dev_card, state="readonly", font=("Segoe UI", 9))
        self.spk_combo.grid(row=0, column=1, sticky="ew", padx=8, pady=2)
        self.spk_combo.bind("<<ComboboxSelected>>", self._on_device_changed)

        refresh_dev_btn = tk.Button(dev_card, text="🔄 重新整理", font=("Segoe UI", 8), bg="#f1f5f9", fg="#475569",
                                    relief="flat", padx=6, pady=1, cursor="hand2", command=self._refresh_devices)
        refresh_dev_btn.grid(row=0, column=2, sticky="e", pady=2)

        # 揚聲器增益與電平列
        spk_ctrl_row = tk.Frame(dev_card, bg="#ffffff")
        spk_ctrl_row.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(2, 6))

        tk.Label(spk_ctrl_row, text="收音增益：", font=("Segoe UI", 8), bg="#ffffff", fg="#64748b").pack(side="left")
        self.spk_gain_slider = ttk.Scale(spk_ctrl_row, from_=0.0, to=3.0, variable=self.spk_gain_var, orient="horizontal", length=160)
        self.spk_gain_slider.pack(side="left", padx=(2, 4))
        self.spk_gain_lbl = tk.Label(spk_ctrl_row, text="100%", font=("Segoe UI", 8), bg="#ffffff", fg="#64748b", width=5)
        self.spk_gain_lbl.pack(side="left")
        self.spk_gain_slider.configure(command=lambda v: self.spk_gain_lbl.config(text=f"{int(float(v)*100)}%"))

        tk.Button(spk_ctrl_row, text="↺ 100%", font=("Segoe UI", 8), bg="#f1f5f9", fg="#475569",
                  relief="flat", padx=4, pady=1, cursor="hand2",
                  command=lambda: (self.spk_gain_var.set(1.0), self.spk_gain_lbl.config(text="100%"))).pack(side="left", padx=(2, 10))

        tk.Label(spk_ctrl_row, text="收音動態：", font=("Segoe UI", 8), bg="#ffffff", fg="#64748b").pack(side="left")
        self.spk_meter = ModernMeter(spk_ctrl_row, width=180, height=14)
        self.spk_meter.pack(side="left", fill="x", expand=True, padx=(2, 4))
        self.spk_meter_val_lbl = tk.Label(spk_ctrl_row, text="0%", font=("Consolas", 8), bg="#ffffff", fg="#64748b", width=5)
        self.spk_meter_val_lbl.pack(side="left")

        # 麥克風 (自己聲音)
        tk.Label(dev_card, text="🎤 麥克風 (自己):", font=("Segoe UI", 9, "bold"), bg="#ffffff").grid(row=2, column=0, sticky="w", pady=2)
        self.mic_combo = ttk.Combobox(dev_card, state="readonly", font=("Segoe UI", 9))
        self.mic_combo.grid(row=2, column=1, sticky="ew", padx=8, pady=2)
        self.mic_combo.bind("<<ComboboxSelected>>", self._on_device_changed)

        # 麥克風增益與電平列
        mic_ctrl_row = tk.Frame(dev_card, bg="#ffffff")
        mic_ctrl_row.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(2, 4))

        tk.Label(mic_ctrl_row, text="收音增益：", font=("Segoe UI", 8), bg="#ffffff", fg="#64748b").pack(side="left")
        self.mic_gain_slider = ttk.Scale(mic_ctrl_row, from_=0.0, to=3.0, variable=self.mic_gain_var, orient="horizontal", length=160)
        self.mic_gain_slider.pack(side="left", padx=(2, 4))
        self.mic_gain_lbl = tk.Label(mic_ctrl_row, text="100%", font=("Segoe UI", 8), bg="#ffffff", fg="#64748b", width=5)
        self.mic_gain_lbl.pack(side="left")
        self.mic_gain_slider.configure(command=lambda v: self.mic_gain_lbl.config(text=f"{int(float(v)*100)}%"))

        tk.Button(mic_ctrl_row, text="↺ 100%", font=("Segoe UI", 8), bg="#f1f5f9", fg="#475569",
                  relief="flat", padx=4, pady=1, cursor="hand2",
                  command=lambda: (self.mic_gain_var.set(1.0), self.mic_gain_lbl.config(text="100%"))).pack(side="left", padx=(2, 10))

        tk.Label(mic_ctrl_row, text="收音動態：", font=("Segoe UI", 8), bg="#ffffff", fg="#64748b").pack(side="left")
        self.mic_meter = ModernMeter(mic_ctrl_row, width=180, height=14)
        self.mic_meter.pack(side="left", fill="x", expand=True, padx=(2, 4))
        self.mic_meter_val_lbl = tk.Label(mic_ctrl_row, text="0%", font=("Consolas", 8), bg="#ffffff", fg="#64748b", width=5)
        self.mic_meter_val_lbl.pack(side="left")

        # 4. 錄音計時與控制卡片
        ctrl_card = tk.Frame(main_container, bg="#ffffff", padx=16, pady=12, relief="groove", bd=1)
        ctrl_card.pack(fill="x", pady=(0, 8))

        self.timer_label = tk.Label(ctrl_card, text="00:00:00", font=("Consolas", 32, "bold"), fg="#1e293b", bg="#ffffff")
        self.timer_label.pack(pady=(0, 6))

        self.record_btn = tk.Button(ctrl_card, text="🔴  開始錄製雙向會議", font=("Segoe UI", 12, "bold"),
                                    bg="#ef4444", fg="#ffffff", activebackground="#dc2626", activeforeground="#ffffff",
                                    relief="flat", padx=30, pady=8, cursor="hand2", command=self.toggle_recording)
        self.record_btn.pack(pady=4)

        # 上傳到 AI 伺服器選項 (僅登入後可設定，預設選取；未登入時停用且不選取)
        self.upload_to_server_var = tk.BooleanVar(value=False)
        self.upload_check = tk.Checkbutton(
            ctrl_card,
            text=" 上傳到 AI 伺服器 (雲端生成逐字稿與會議摘要)",
            variable=self.upload_to_server_var,
            font=("Segoe UI", 9, "bold"),
            bg="#ffffff",
            activebackground="#ffffff",
            selectcolor="#ffffff",
            state="disabled",
            command=self._on_upload_option_changed
        )
        self.upload_check.pack(pady=(6, 2))

        self.upload_hint_label = tk.Label(
            ctrl_card,
            text="⚠️ 需先登入帳號方可勾選上傳至雲端伺服器 (未登入僅儲存本機音檔)",
            font=("Segoe UI", 8),
            fg="#94a3b8",
            bg="#ffffff"
        )
        self.upload_hint_label.pack(pady=(0, 2))

        # 5. 狀態與雲端上傳卡片 (expand=True 確保底部區域完整展開)
        status_card = tk.LabelFrame(main_container, text=" 雲端 AI 處理狀態與操作 ", font=("Segoe UI", 9, "bold"),
                                    bg="#ffffff", fg="#334155", padx=14, pady=12, relief="groove")
        status_card.pack(fill="both", expand=True)

        self.status_msg_label = tk.Label(status_card, text="就緒。請確認登入帳號後開始錄音。", font=("Segoe UI", 9), fg="#475569", bg="#ffffff", anchor="w")
        self.status_msg_label.pack(fill="x", pady=(0, 6))

        self.progress_bar = ttk.Progressbar(status_card, mode="determinate")
        self.progress_bar.pack(fill="x", pady=(0, 8))

        btn_row = tk.Frame(status_card, bg="#ffffff")
        btn_row.pack(fill="x", pady=4)

        self.open_web_btn = tk.Button(btn_row, text="🌐 開啟網頁檢視會議總結與逐字稿", font=("Segoe UI", 9, "bold"),
                                      bg="#f1f5f9", fg="#2563eb", activebackground="#e2e8f0", relief="groove",
                                      padx=12, pady=5, cursor="hand2", state="disabled", command=self.open_web_platform)
        self.open_web_btn.pack(side="left")

        self.retry_upload_btn = tk.Button(btn_row, text="🔄 重新上傳最後錄音", font=("Segoe UI", 9),
                                          bg="#f1f5f9", fg="#475569", relief="groove",
                                          padx=10, pady=5, cursor="hand2", state="disabled", command=self.manual_reupload)
        self.retry_upload_btn.pack(side="right")

        self.open_folder_btn = tk.Button(btn_row, text="📁 開啟本機錄音夾", font=("Segoe UI", 9),
                                         bg="#f1f5f9", fg="#475569", relief="groove",
                                         padx=10, pady=5, cursor="hand2", command=self.open_local_folder)
        self.open_folder_btn.pack(side="right", padx=(0, 8))

    # ====== 裝置偵測 ======
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
            self._on_device_changed()
        except Exception as e:
            self.status_msg_label.config(text=f"裝置讀取失敗: {e}", fg="#ef4444")

    def _on_device_changed(self, event=None):
        if self.is_recording:
            return
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

        self.engine.start_preview_monitoring(spk_dev, mic_dev)

    def _check_and_render_recovery_banner(self, container):
        """檢查是否有上次因異常中斷遺留的錄音暫存檔並提供一鍵救援"""
        temp_dir = tempfile.gettempdir()
        recoverable_pairs = []
        try:
            mic_files = [f for f in os.listdir(temp_dir) if f.startswith("teams_rec_mic_") and f.endswith(".wav")]
            for mf in mic_files:
                mp = os.path.join(temp_dir, mf)
                suffix = mf.replace("teams_rec_mic_", "")
                sp = os.path.join(temp_dir, f"teams_rec_spk_{suffix}")
                m_size = os.path.getsize(mp) if os.path.exists(mp) else 0
                s_size = os.path.getsize(sp) if os.path.exists(sp) else 0
                total_mb = (m_size + s_size) / (1024 * 1024)
                if total_mb >= 3.0:
                    recoverable_pairs.append({
                        "suffix": suffix,
                        "mic": mp if m_size > 44 else None,
                        "spk": sp if s_size > 44 else None,
                        "size_mb": total_mb,
                        "mtime": os.path.getmtime(mp)
                    })
        except Exception:
            pass

        if not recoverable_pairs:
            return

        latest = max(recoverable_pairs, key=lambda x: x["mtime"])
        est_min = int(latest["size_mb"] / 10.5)
        est_str = f"約 {est_min} 分鐘" if est_min > 0 else f"{latest['size_mb']:.1f} MB"

        banner = tk.Frame(container, bg="#fef3c7", padx=12, pady=8, bd=1, relief="solid")
        banner.pack(fill="x", pady=(0, 8))

        tk.Label(
            banner,
            text=f"⚠️ 偵測到上次有未正常完成混音的錄音暫存檔 ({est_str})！",
            font=("Segoe UI", 9, "bold"),
            fg="#92400e",
            bg="#fef3c7"
        ).pack(side="left")

        def _do_recover():
            now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            rescue_name = f"救援會議錄音_{now_str}.mp3"
            rescue_path = os.path.join(RECORDINGS_DIR, rescue_name)
            banner.destroy()
            self.status_msg_label.config(text=f"正在為您救援匯出 {est_str} 錄音檔...", fg="#2563eb")

            def _rec_worker():
                try:
                    self.engine.temp_spk_path = latest["spk"]
                    self.engine.temp_mic_path = latest["mic"]
                    self.engine.mix_and_export_mp3(
                        rescue_path,
                        spk_gain=self.spk_gain_var.get(),
                        mic_gain=self.mic_gain_var.get(),
                        progress_cb=lambda p, msg: self.root.after(0, lambda pct=int(p*100), m=msg: [
                            self.progress_bar.config(value=pct),
                            self.status_msg_label.config(text=f"救援匯出中 ({pct}%): {m}", fg="#2563eb")
                        ])
                    )
                    self.last_audio_file = rescue_path
                    self.root.after(0, lambda: [
                        self.status_msg_label.config(text=f"🎉 救援成功！已匯出至：{rescue_path}", fg="#10b981"),
                        messagebox.showinfo("救援成功", f"已成功救援並匯出錄音檔：\n{rescue_path}")
                    ])
                except Exception as ex:
                    self.root.after(0, lambda: messagebox.showerror("救援失敗", f"救援匯出時發生錯誤: {ex}"))
            threading.Thread(target=_rec_worker, daemon=True).start()

        tk.Button(
            banner,
            text="立即一鍵救援匯出",
            font=("Segoe UI", 8, "bold"),
            bg="#d97706",
            fg="#ffffff",
            relief="flat",
            padx=10,
            pady=2,
            cursor="hand2",
            command=_do_recover
        ).pack(side="right", padx=(8, 0))

        tk.Button(
            banner,
            text="忽略",
            font=("Segoe UI", 8),
            bg="#fef3c7",
            fg="#78350f",
            relief="flat",
            command=banner.destroy
        ).pack(side="right")

    def on_closing(self):
        if self.is_recording:
            if not messagebox.askyesno("正在錄音中", "錄音進行中，確定要離開嗎？"):
                return
        self.engine.cleanup()
        self.root.destroy()

    # ====== VU 音量監聽輪詢 ======
    def _schedule_meter_update(self):
        spk_gain = self.spk_gain_var.get()
        mic_gain = self.mic_gain_var.get()

        spk_lvl = getattr(self.engine, "spk_level", 0.0) * spk_gain
        mic_lvl = getattr(self.engine, "mic_level", 0.0) * mic_gain

        self.spk_meter.set_level(spk_lvl, getattr(self, "spk_meter_val_lbl", None))
        self.mic_meter.set_level(mic_lvl, getattr(self, "mic_meter_val_lbl", None))

        if self.is_recording:
            elapsed = int(time.time() - self.record_start_time)
            h = elapsed // 3600
            m = (elapsed % 3600) // 60
            s = elapsed % 60
            self.timer_label.config(text=f"{h:02d}:{m:02d}:{s:02d}")

        self.root.after(40, self._schedule_meter_update)

    # ====== 登入與伺服器認證 ======
    def _check_login_status_bg(self):
        # 若本機已保存上次登入的 Token 與帳號資訊，立即於 UI 恢復已登入狀態，避免開啟時變成未登入
        if self.cfg.get("token") and self.cfg.get("user_name"):
            self._update_user_ui_authed({
                "name": self.cfg.get("user_name", "同仁"),
                "email": self.cfg.get("user_email", "")
            })
        threading.Thread(target=self._verify_token_with_server, daemon=True).start()

    def _verify_token_with_server(self):
        token = self.cfg.get("token", "")
        server_url = self.cfg.get("server_url", DEFAULT_SERVER_URL).rstrip("/")
        if not token:
            self.root.after(0, self._update_user_ui_unauth)
            return

        api_url = f"{server_url}/api/auth/me"
        try:
            req = urllib.request.Request(api_url, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=5) as res:
                data = json.loads(res.read().decode("utf-8"))
                if data.get("status") == "ok":
                    user = data.get("user", {})
                    self.cfg["user_name"] = user.get("name", "")
                    self.cfg["user_email"] = user.get("email", "")
                    save_client_config(self.cfg)
                    self.root.after(0, lambda: self._update_user_ui_authed(user))
                    return
        except urllib.error.HTTPError as he:
            # 僅當伺服器明確回傳 401 或 403 時，才視為登入失效需清除
            if he.code in (401, 403):
                self.cfg["token"] = ""
                save_client_config(self.cfg)
                self.root.after(0, self._update_user_ui_unauth)
                return
        except Exception as e:
            # 暫時性網路斷線或逾時，保留本機已登入狀態
            print("Token verify network error, keeping session:", e)
            return

    def _update_user_ui_authed(self, user):
        name = user.get("name", "")
        email = user.get("email", "")
        self.user_status_label.config(text=f"👤 {name} ({email})", fg="#38bdf8")
        self.auth_btn.config(text="登出 / 切換", bg="#334155")
        self.open_web_btn.config(state="normal")

        # 登入成功：啟用勾選選項，預設勾選 (或維持先前偏好)
        self.upload_check.config(state="normal")
        is_auto = self.cfg.get("auto_upload", True)
        self.upload_to_server_var.set(is_auto)
        self.upload_hint_label.config(
            text="✓ 已登入，可自由切換是否上傳至 AI 伺服器 (本機均會完整存檔)",
            fg="#10b981"
        )

    def _update_user_ui_unauth(self):
        self.user_status_label.config(text="⚠️ 尚未登入", fg="#fca5a5")
        self.auth_btn.config(text="登入帳號", bg="#2563eb")
        self.open_web_btn.config(state="disabled")

        # 未登入：停用勾選選項，強制不選取且無法修改
        self.upload_to_server_var.set(False)
        self.upload_check.config(state="disabled")
        self.upload_hint_label.config(
            text="⚠️ 需先登入帳號方可勾選上傳至雲端伺服器 (未登入僅儲存本機音檔)",
            fg="#94a3b8"
        )

    def _on_upload_option_changed(self):
        val = self.upload_to_server_var.get()
        self.cfg["auto_upload"] = val
        save_client_config(self.cfg)
        if self.is_recording:
            if val and self.cfg.get("token"):
                self.record_btn.config(text="⏹  停止錄音並上傳雲端")
            else:
                self.record_btn.config(text="⏹  停止錄音並儲存本機")

    def open_local_folder(self):
        if not os.path.exists(RECORDINGS_DIR):
            os.makedirs(RECORDINGS_DIR, exist_ok=True)
        try:
            os.startfile(RECORDINGS_DIR)
        except Exception as e:
            messagebox.showerror("開啟失敗", f"無法開啟本機資料夾: {e}")

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
        dlg.title("登入 Hawkeye 會議系統")
        dlg.geometry("420x330")
        dlg.minsize(400, 310)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()

        dlg.configure(bg="#ffffff", padx=24, pady=20)

        tk.Label(dlg, text="同仁帳號登入", font=("Segoe UI", 13, "bold"), bg="#ffffff").pack(anchor="w", pady=(0, 14))

        tk.Label(dlg, text="企業信箱 (限 @hawkeyetech.com.tw):", font=("Segoe UI", 9, "bold"), bg="#ffffff").pack(anchor="w")
        email_entry = tk.Entry(dlg, font=("Segoe UI", 10), relief="solid", bd=1)
        email_entry.pack(fill="x", pady=(3, 12))
        email_entry.insert(0, self.cfg.get("user_email", ""))

        tk.Label(dlg, text="密碼:", font=("Segoe UI", 9, "bold"), bg="#ffffff").pack(anchor="w")
        pwd_entry = tk.Entry(dlg, font=("Segoe UI", 10), relief="solid", bd=1, show="•")
        pwd_entry.pack(fill="x", pady=(3, 12))

        err_label = tk.Label(dlg, text="", font=("Segoe UI", 9), fg="#ef4444", bg="#ffffff")
        err_label.pack(anchor="w", pady=(0, 10))

        def do_login():
            server_url = self.cfg.get("server_url", DEFAULT_SERVER_URL).rstrip("/")
            email = email_entry.get().strip()
            password = pwd_entry.get()

            if not email or not password:
                err_label.config(text="請輸入完整信箱與密碼！")
                return

            err_label.config(text="連線驗證中...", fg="#2563eb")
            dlg.update()

            def login_worker():
                api_url = f"{server_url}/api/auth/login"
                try:
                    payload = json.dumps({"email": email, "password": password}).encode("utf-8")
                    req = urllib.request.Request(api_url, data=payload, headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=10) as res:
                        data = json.loads(res.read().decode("utf-8"))
                        if data.get("status") == "ok":
                            self.cfg["server_url"] = server_url
                            self.cfg["token"] = data["token"]
                            self.cfg["user_name"] = data["user"].get("name", "")
                            self.cfg["user_email"] = data["user"].get("email", "")
                            save_client_config(self.cfg)

                            dlg.after(0, lambda: [
                                self._update_user_ui_authed(data["user"]),
                                dlg.destroy(),
                                messagebox.showinfo("登入成功", f"歡迎回來，{data['user'].get('name')}！")
                            ])
                            return
                        else:
                            msg = data.get("message", "登入失敗")
                            dlg.after(0, lambda: err_label.config(text=msg, fg="#ef4444"))
                except urllib.error.HTTPError as he:
                    try:
                        err_data = json.loads(he.read().decode("utf-8"))
                        msg = err_data.get("message", str(he))
                    except Exception:
                        msg = str(he)
                    dlg.after(0, lambda: err_label.config(text=f"錯誤: {msg}", fg="#ef4444"))
                except Exception as ex:
                    dlg.after(0, lambda: err_label.config(text=f"連線失敗: {ex}", fg="#ef4444"))

            threading.Thread(target=login_worker, daemon=True).start()

        # 按鍵綁定 Enter 鍵快速登入
        email_entry.bind("<Return>", lambda e: pwd_entry.focus_set())
        pwd_entry.bind("<Return>", lambda e: do_login())

        btn_row = tk.Frame(dlg, bg="#ffffff")
        btn_row.pack(fill="x", pady=(4, 0))

        login_btn = tk.Button(btn_row, text="登入帳號", font=("Segoe UI", 9, "bold"), bg="#2563eb", fg="#ffffff",
                              activebackground="#1d4ed8", relief="flat", padx=16, pady=8, cursor="hand2", command=do_login)
        login_btn.pack(side="left", fill="x", expand=True, padx=(0, 8))

        tk.Button(btn_row, text="前往網頁註冊", font=("Segoe UI", 9), bg="#f1f5f9", fg="#475569",
                  relief="groove", padx=12, pady=8, cursor="hand2",
                  command=lambda: webbrowser.open(self.cfg.get("server_url", DEFAULT_SERVER_URL))).pack(side="right")

        if not email_entry.get():
            email_entry.focus_set()
        else:
            pwd_entry.focus_set()

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

        self.engine.stop_preview_monitoring()
        self.record_btn.config(state="disabled")
        self.status_msg_label.config(text="正在啟動音訊錄製引擎...", fg="#2563eb")

        def start_worker():
            if sys.platform == "win32":
                try:
                    import ctypes
                    ctypes.windll.ole32.CoInitialize(None)
                except Exception:
                    pass
            success, msg = self.engine.start_recording(spk_dev, mic_dev)
            if success:
                self.is_recording = True
                self.record_start_time = time.time()
                self.root.after(0, self._on_record_started)
            else:
                self.root.after(0, lambda m=msg: [
                    self.record_btn.config(state="normal"),
                    messagebox.showerror("啟動失敗", f"無法啟動音訊錄製裝置: {m}")
                ])

        threading.Thread(target=start_worker, daemon=True).start()

    def _on_record_started(self):
        if self.upload_to_server_var.get() and self.cfg.get("token"):
            self.record_btn.config(text="⏹  停止錄音並上傳雲端", bg="#0f172a", state="normal")
        else:
            self.record_btn.config(text="⏹  停止錄音並儲存本機", bg="#0f172a", state="normal")
        self.status_msg_label.config(text="⏺ 錄音進行中 (正在混音錄製)...", fg="#ef4444")
        self.progress_bar["value"] = 0

    def stop_recording(self):
        self.record_btn.config(state="disabled")
        self.status_msg_label.config(text="正在停止錄音並匯出 MP3...", fg="#2563eb")
        self.is_recording = False

        # 輸出檔案路徑
        now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        title_raw = self.title_entry.get().strip() or f"會議錄音_{now_str}"
        safe_title = "".join(c for c in title_raw if c.isalnum() or c in (' ', '_', '-')).strip()[:35]
        mp3_filename = f"{safe_title}_{now_str}.mp3"
        mp3_path = os.path.join(RECORDINGS_DIR, mp3_filename)

        def stop_worker():
            if sys.platform == "win32":
                try:
                    import ctypes
                    ctypes.windll.ole32.CoInitialize(None)
                except Exception:
                    pass
            try:
                # 1. 停止音訊串流執行緒
                self.engine.stop_recording()
                # 2. 混音並編碼為 MP3 (低記憶體串流分塊混音)
                self.engine.mix_and_export_mp3(
                    mp3_path,
                    spk_gain=self.spk_gain_var.get(),
                    mic_gain=self.mic_gain_var.get(),
                    progress_cb=lambda p, msg: self.root.after(0, lambda pct=int(p * 100), m=msg: [
                        self.progress_bar.config(value=pct),
                        self.status_msg_label.config(text=f"匯出 MP3 ({pct}%): {m}", fg="#2563eb")
                    ])
                )
                self.last_audio_file = mp3_path
            except Exception as e:
                print(f"Error mixing and exporting MP3: {e}")
                self.last_audio_file = None
                self.root.after(0, lambda err=e: self.status_msg_label.config(text=f"匯出失敗: {err}", fg="#ef4444"))
            finally:
                self.root.after(0, self._on_record_stopped)
                self.root.after(500, self._on_device_changed)

        threading.Thread(target=stop_worker, daemon=True).start()

    def _on_record_stopped(self):
        self.record_btn.config(text="🔴  開始錄製雙向會議", bg="#ef4444", state="normal")
        self.retry_upload_btn.config(state="normal")

        if not self.last_audio_file or not os.path.exists(self.last_audio_file):
            self.status_msg_label.config(text="錄音檔輸出失敗或錄音時長過短", fg="#ef4444")
            return

        file_sz_mb = os.path.getsize(self.last_audio_file) / 1024 / 1024

        # 不論有無上傳，錄音檔均已完整儲存於本機 local
        if self.upload_to_server_var.get() and self.cfg.get("token"):
            self.status_msg_label.config(text=f"本機「會議錄音」已存檔 ({file_sz_mb:.1f} MB)，正在自動上傳至雲端 AI 伺服器...", fg="#10b981")
            # 自動上傳
            self.upload_audio_to_server(self.last_audio_file)
        else:
            self.status_msg_label.config(text=f"錄音完成！音檔已儲存至本機「會議錄音」資料夾 ({file_sz_mb:.1f} MB)，未上傳至雲端伺服器。", fg="#10b981")

    # ====== 雲端上傳與轉錄輪詢 ======
    def manual_reupload(self):
        if self.last_audio_file and os.path.exists(self.last_audio_file):
            self.upload_audio_to_server(self.last_audio_file)
        else:
            messagebox.showinfo("無檔案", "目前沒有可重試的錄音檔案。")

    def upload_audio_to_server(self, file_path):
        token = self.cfg.get("token")
        if not token:
            res = messagebox.askyesno("尚未登入", "上傳音訊至雲端 AI 需先登入帳號。\n是否現在立即登入？")
            if res:
                self.open_login_dialog()
            return

        server_url = self.cfg.get("server_url", DEFAULT_SERVER_URL).rstrip("/")
        title = self.title_entry.get().strip()
        filename = os.path.basename(file_path)

        self.status_msg_label.config(text="正在連線伺服器並上傳音訊...", fg="#2563eb")
        self.progress_bar["value"] = 20

        def upload_worker():
            upload_url = f"{server_url}/api/upload?filename={urllib.parse.quote(filename)}&title={urllib.parse.quote(title)}"
            try:
                with open(file_path, "rb") as f_in:
                    data_bytes = f_in.read()

                req = urllib.request.Request(
                    upload_url,
                    data=data_bytes,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "audio/mpeg",
                        "X-Filename": urllib.parse.quote(filename),
                        "X-Meeting-Title": urllib.parse.quote(title)
                    }
                )
                with urllib.request.urlopen(req, timeout=120) as res:
                    resp = json.loads(res.read().decode("utf-8"))
                    if resp.get("status") == "ok":
                        task_id = resp.get("taskId")
                        self.last_task_id = task_id
                        self.root.after(0, lambda: [
                            self.progress_bar.config(value=100),
                            self.status_msg_label.config(
                                text="🎉 音檔已成功上傳！雲端 AI 正在背景處理中，預計 1~3 分鐘後完成，請至網頁檢視。",
                                fg="#10b981"
                            ),
                            self.open_web_btn.config(state="normal"),
                            messagebox.showinfo(
                                "音檔上傳成功",
                                "會議音檔已成功上傳至 AI 伺服器！\n\n雲端正在背景進行語音辨識與摘要生成，\n您不需要在電腦前等待，預計 1~3 分鐘後即可於網頁中瀏覽會議記錄。"
                            )
                        ])
                    else:
                        err = resp.get("message", "上傳失敗")
                        self.root.after(0, lambda: [
                            self.status_msg_label.config(text=f"伺服器回應錯誤: {err}", fg="#ef4444"),
                            messagebox.showerror("上傳錯誤", err)
                        ])
            except urllib.error.HTTPError as he:
                if he.code == 401:
                    self.root.after(0, lambda: [
                        self.status_msg_label.config(text="登入憑證過期，請重新登入", fg="#ef4444"),
                        self.open_login_dialog()
                    ])
                else:
                    self.root.after(0, lambda: self.status_msg_label.config(text=f"HTTP 錯誤: {he.code}", fg="#ef4444"))
            except Exception as ex:
                self.root.after(0, lambda: self.status_msg_label.config(text=f"上傳失敗: {ex}", fg="#ef4444"))

        threading.Thread(target=upload_worker, daemon=True).start()

    def open_web_platform(self):
        server_url = self.cfg.get("server_url", DEFAULT_SERVER_URL).rstrip("/")
        webbrowser.open(server_url)

def main():
    root = tk.Tk()
    app = RecorderApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()

if __name__ == "__main__":
    main()
