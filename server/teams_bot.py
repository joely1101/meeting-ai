#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Teams AI 會議自動撥入與錄音排程模組
- 支援排程或立即撥入 Teams 會議
- 使用 Headless Chromium 與獨立 PulseAudio 虛擬聲道截錄會議音訊
- 會議結束後自動調度 Gemini 3.8 Flash 進行語音轉錄與決策摘要
"""

import os
import sys
import time
import re
import json
import secrets
import datetime
import subprocess
import threading
import signal

# 載入路徑與資料庫模組
SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
RECORDV2_DIR = os.path.dirname(SERVER_DIR)
DATA_DIR = os.path.join(RECORDV2_DIR, "data")
MEETINGS_DIR = os.path.join(DATA_DIR, "meetings")

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

import database
import ai_transcriber

def ensure_pulseaudio():
    """確保 PulseAudio 伺服器正在執行"""
    try:
        subprocess.run(["pactl", "info"], capture_output=True, check=True)
    except Exception:
        try:
            subprocess.run(["pulseaudio", "--start", "--exit-idle-time=-1"], capture_output=True)
            time.sleep(1)
        except Exception as e:
            print(f"[TeamsBot] Warning: Failed to autostart pulseaudio: {e}")

def create_virtual_sink(sink_name):
    """為單次會議建立專屬的 PulseAudio Null-Sink (避免多會議音訊互相干擾)"""
    ensure_pulseaudio()
    try:
        cmd = [
            "pactl", "load-module", "module-null-sink",
            f"sink_name={sink_name}",
            f"sink_properties=device.description={sink_name}"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        mod_id = res.stdout.strip()
        print(f"[TeamsBot] Loaded virtual audio sink: {sink_name} (Module ID: {mod_id})")
        return mod_id
    except Exception as e:
        print(f"[TeamsBot] Error creating virtual sink {sink_name}: {e}")
        return None

def unload_virtual_sink(mod_id):
    """移除 PulseAudio Null-Sink"""
    if not mod_id:
        return
    try:
        subprocess.run(["pactl", "unload-module", str(mod_id)], capture_output=True)
        print(f"[TeamsBot] Unloaded virtual audio sink module ID: {mod_id}")
    except Exception as e:
        print(f"[TeamsBot] Error unloading sink module {mod_id}: {e}")

def sanitize_filename(name):
    clean = re.sub(r'[\\/*?:"<>|#&% ]+', '_', name).strip('._')
    return clean[:40] if clean else "TeamsMeeting"

def sanitize_bot_name(name):
    # Teams 訪客名稱僅允許英數字、繁簡中文、空格與 - ' . _ @，禁止包含 ( ) [ ] 等符號
    clean = re.sub(r'[^\w\s\u4e00-\u9fff\-\'\.\_\@]', ' ', name)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean[:45] if clean else "AI Meeting Bot"

def check_visible(locator, timeout_ms=1000) -> bool:
    """安全等待 locator 可見，避免 is_visible() 接受 timeout 引發 TypeError"""
    try:
        locator.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False

def mute_prejoin_devices(page):
    """確保在 Prejoin 畫面徹底關閉視訊鏡頭與麥克風"""
    try:
        vid_loc = page.locator('input[data-tid="toggle-video"]').first
        if vid_loc.is_visible() and vid_loc.is_checked():
            vid_loc.click(force=True)
            print("[TeamsBot] 成功關閉入會視訊鏡頭")
    except Exception as e:
        print(f"[TeamsBot] 關閉視訊鏡頭例外: {e}")

    try:
        mic_loc = page.locator('input[data-tid="toggle-mute"]').first
        if mic_loc.is_visible():
            title = (mic_loc.get_attribute("title") or "").lower()
            cid = mic_loc.get_attribute("data-cid") or ""
            if mic_loc.is_checked() or "toggle-mute-true" in cid or "mute mic" in title:
                mic_loc.click(force=True)
                print("[TeamsBot] 成功切換入會麥克風為靜音狀態 (toggle-mute -> false)")
    except Exception as e:
        print(f"[TeamsBot] 切換入會麥克風例外: {e}")

def mute_incall_microphone(page):
    """通話中主動確認並強制靜音麥克風"""
    try:
        mic_selectors = [
            'button[data-tid="microphone-button"]',
            'button[data-tid="calling-microphone-button"]',
            'button[aria-label*="Mute mic" i]',
            'button[aria-label*="靜音" i]',
            'button:has-text("Mic")',
            'button:has-text("麥克風")'
        ]
        for sel in mic_selectors:
            loc = page.locator(sel).first
            if loc.is_visible():
                aria = (loc.get_attribute("aria-label") or "").lower()
                title = (loc.get_attribute("title") or "").lower()
                if "unmute" not in aria and "取消" not in aria and "unmute" not in title and "取消" not in title:
                    loc.click()
                    print("[TeamsBot] 通話中偵測到麥克風未靜音，已點擊通話列麥克風按鈕強制靜音！")
                    return True
                else:
                    return True
    except Exception:
        pass
    try:
        page.keyboard.press("Control+Shift+M")
    except Exception:
        pass
    return False

class TeamsBotSession:
    """單一 Teams 撥入與錄音工作階段"""
    def __init__(self, task_info, tasks_dict=None, stop_event=None):
        self.task_id = task_info["id"]
        self.user_id = task_info["user_id"]
        self.title = task_info["title"]
        self.teams_url = task_info["teams_url"]
        self.bot_name = sanitize_bot_name(task_info.get("bot_name", "AI 會議記錄助理 - Hawkeye AI"))
        self.max_duration_min = task_info.get("max_duration_min", 60)
        self.notify_email = (task_info.get("notify_email") or "").strip()
        self.profile_id = task_info.get("profile_id")
        self.tasks_dict = tasks_dict or {}
        self.stop_event = stop_event or threading.Event()
        self.sink_name = f"teams_sink_{self.task_id[:12]}"
        self.sink_mod_id = None
        self.ffmpeg_proc = None
        self.audio_path = None
        self.folder_name = None

    def run(self):
        print(f"[TeamsBot] ===== 開始執行 Teams 撥入任務: {self.task_id} ({self.title}) =====")
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        database.update_bot_task(self.task_id, status="joining", started_at=now_str, error_message="")

        # 1. 建立錄音存檔資料夾
        safe_title = sanitize_filename(self.title)
        self.folder_name = f"{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_title}"
        target_dir = os.path.join(MEETINGS_DIR, str(self.user_id), self.folder_name)
        os.makedirs(target_dir, exist_ok=True)
        self.audio_path = os.path.join(target_dir, f"{safe_title}.mp3")

        # 2. 建立專屬 PulseAudio Null-Sink
        self.sink_mod_id = create_virtual_sink(self.sink_name)

        # 3. 啟動 FFmpeg 於背景持續截錄該 Null-Sink
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-f", "pulse",
            "-i", f"{self.sink_name}.monitor",
            "-ac", "2",
            "-ar", "44100",
            "-b:a", "128k",
            self.audio_path
        ]
        try:
            self.ffmpeg_proc = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            print(f"[TeamsBot] FFmpeg 錄音處理常式已啟動 (PID: {self.ffmpeg_proc.pid}) -> {self.audio_path}")
        except Exception as e:
            err_msg = f"無法啟動 FFmpeg 錄音: {e}"
            print(f"[TeamsBot] Error: {err_msg}")
            database.update_bot_task(self.task_id, status="failed", error_message=err_msg, ended_at=now_str)
            unload_virtual_sink(self.sink_mod_id)
            return

        # 4. 透過 Playwright 啟動 Chromium 撥入會議
        context = None
        playwright_obj = None
        joined_meeting = False

        try:
            from playwright.sync_api import sync_playwright
            playwright_obj = sync_playwright().start()

            # 設定環境變數指定 Chromium 輸出音訊至專屬 sink，並確保支援 WSLg GUI 視窗顯示
            env = os.environ.copy()
            env["PULSE_SINK"] = self.sink_name
            if not env.get("DISPLAY"):
                env["DISPLAY"] = ":0"
            if not env.get("WAYLAND_DISPLAY"):
                env["WAYLAND_DISPLAY"] = "wayland-0"
            if not env.get("XDG_RUNTIME_DIR"):
                env["XDG_RUNTIME_DIR"] = "/run/user/1000"

            # 確保有純靜音音訊檔提供給 Chromium 虛擬麥克風 (防止產生嗡嗡聲雜音)
            silence_wav = os.path.join(os.path.dirname(os.path.abspath(__file__)), "silence.wav")
            if not os.path.exists(silence_wav):
                silence_wav = "/tmp/silence.wav"
                try:
                    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "2", "-c:a", "pcm_s16le", silence_wav], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass

            # 確保 persistent browser profile 目錄存在並預先設定協定信任 (完全根除 xdg-open 彈窗)
            profile_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "browser_profile")
            os.makedirs(os.path.join(profile_dir, "Default"), exist_ok=True)
            pref_file = os.path.join(profile_dir, "Default", "Preferences")
            try:
                prefs = {}
                if os.path.exists(pref_file):
                    with open(pref_file, "r") as f:
                        prefs = json.load(f)
                if "protocol_handler" not in prefs:
                    prefs["protocol_handler"] = {}
                prefs["protocol_handler"]["allowed_origin_protocol_pairs"] = {
                    "https://teams.live.com": {"ms-teams": True, "msteams": True},
                    "https://teams.microsoft.com": {"ms-teams": True, "msteams": True}
                }
                with open(pref_file, "w") as f:
                    json.dump(prefs, f, indent=2)
            except Exception as e:
                print(f"[TeamsBot] 寫入 Profile Preferences 警告: {e}")

            context = playwright_obj.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=False,
                args=[
                    "--use-fake-ui-for-media-stream",
                    "--use-fake-device-for-media-stream",
                    f"--use-file-for-fake-audio-capture={silence_wav}",
                    "--autoplay-policy=no-user-gesture-required",
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-infobars",
                    "--window-size=1280,720"
                ],
                permissions=["microphone"],
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 720},
                env=env
            )

            # 雙重防護：攔截 ms-teams 協定呼叫，杜絕任何系統層級彈窗阻斷
            context.route("ms-teams:**", lambda route: route.abort())
            context.route("**/*ms-teams*", lambda route: route.abort())

            page = context.pages[0] if context.pages else context.new_page()

            # 導向 Teams 會議連結 (自動加上 launchType=web&anon=true 確保 Teams 預設走 Web 流程)
            nav_url = self.teams_url
            if ("teams.live.com" in nav_url or "teams.microsoft.com" in nav_url) and "launchType=" not in nav_url:
                delim = "&" if "?" in nav_url else "?"
                nav_url = f"{nav_url}{delim}launchType=web&anon=true"

            print(f"[TeamsBot] 導向會議網址: {nav_url}")
            page.goto(nav_url, wait_until="load", timeout=45000)

            # 等候預入會介面載入並處理名稱與音訊提示
            print(f"[TeamsBot] 等候入會介面，準備以「{self.bot_name}」加入...")
            web_btn = page.locator('button[data-tid="joinOnWeb"], a[data-tid="joinOnWeb"], button:has-text("Continue on this browser"), button:has-text("在瀏覽器上繼續")').first
            no_av_btn = page.locator('button:has-text("Continue without audio or video"), button:has-text("在不使用音訊或視訊的情況下繼續"), button:has-text("在沒有音訊或視訊的情況下繼續")').first
            inp = page.locator('input[data-tid="prejoin-display-name-input"], input[aria-label*="name" i], input[placeholder*="name" i], input[placeholder*="名稱" i], input[placeholder*="姓名" i]').first
            join_btn = page.locator('button[data-tid="prejoin-join-button"], button:has-text("Join now"), button:has-text("立即加入")').first

            # 輪詢等待進入 Prejoin 畫面並自動填寫
            for sec in range(1, 40):
                page.wait_for_timeout(1000)
                
                # 1. 若處於啟動跳轉頁，優先點選「在瀏覽器上繼續」
                if web_btn.is_visible():
                    print("[TeamsBot] 偵測到跳轉頁面，點擊「在瀏覽器上繼續」(Continue on this browser)...")
                    try:
                        web_btn.click()
                    except Exception as e:
                        print(f"[TeamsBot] web_btn 點擊例外: {e}")
                    page.wait_for_timeout(1000)
                    continue
                
                # 關閉無音訊視訊提示視窗
                if no_av_btn.is_visible():
                    print(f"[TeamsBot] 關閉「在不使用音訊或視訊的情況下繼續」提示視窗")
                    try:
                        no_av_btn.click()
                    except Exception:
                        pass
                    page.wait_for_timeout(500)

                # 填入乾淨暱稱
                if inp.is_visible():
                    try:
                        val = inp.input_value()
                        if val != self.bot_name:
                            inp.fill(self.bot_name)
                            print(f"[TeamsBot] 成功填入入會暱稱: {self.bot_name}")
                            page.wait_for_timeout(500)
                    except Exception:
                        pass

                # 確保鏡頭與麥克風在入會前徹底關閉 (不分享視訊畫面與維持靜音)
                mute_prejoin_devices(page)

                # 點選「立即加入」
                if join_btn.is_visible() and join_btn.is_enabled():
                    mute_prejoin_devices(page)
                    print("[TeamsBot] 點選「立即加入」按鈕...")
                    try:
                        join_btn.click()
                        break
                    except Exception as e:
                        print(f"[TeamsBot] 點選加入按鈕重試: {e}")

            # 等候進入會議或進入大廳 (Lobby)
            in_lobby = False
            joined_call = False

            print("[TeamsBot] 等待入會或大廳許可...")
            start_wait = time.time()
            while time.time() - start_wait < 300: # 最多等 5 分鐘審核
                if self.stop_event.is_set():
                    break

                body_text = ""
                try:
                    if page.locator("body").is_visible():
                        body_text = page.inner_text("body").lower()
                except Exception:
                    pass

                # 檢查是否在等候大廳 (Waiting in Lobby)
                if any(k in body_text for k in [
                    "waiting in the lobby", "someone will let you in", "someone in the meeting should let you in",
                    "已通知會議中的其他人", "等候主持人", "等候室", "等候"
                ]):
                    if not in_lobby:
                        in_lobby = True
                        print("[TeamsBot] 目前處於會議等候室 (Lobby)，等待主持人准許加入...")
                        database.update_bot_task(self.task_id, status="in_lobby")

                # 檢查是否已正式進入會議 (出現通話工具列、掛斷、名單等按鈕，或大廳等待訊息消失)
                has_call_controls = False
                for sel in [
                    'button[data-tid="hangup-button"]',
                    'button[data-tid="call-hangup"]',
                    'button[data-tid="calling-hangup-button"]',
                    'button[data-tid="roster-button"]',
                    'button[data-tid="microphone-button"]',
                    'button[aria-label*="Leave" i]',
                    'button[aria-label*="離開" i]',
                    'button[aria-label*="Hang up" i]',
                    'button[aria-label*="掛斷" i]'
                ]:
                    try:
                        if page.locator(sel).first.is_visible():
                            has_call_controls = True
                            break
                    except Exception:
                        pass

                lobby_text_present = any(k in body_text for k in [
                    "waiting in the lobby", "someone will let you in", "someone in the meeting should let you in",
                    "已通知會議中的其他人", "等候主持人", "等候室"
                ])

                if has_call_controls or (in_lobby and not lobby_text_present and not any(k in body_text for k in ["you've been removed", "已被移出", "會議已結束"])):
                    joined_call = True
                    print("[TeamsBot] 🎉 成功進入 Teams 會議，通話錄音正式開始！")
                    database.update_bot_task(self.task_id, status="recording")
                    # 入會後立即確認通話麥克風強制靜音
                    try:
                        page.wait_for_timeout(1000)
                        mute_incall_microphone(page)
                    except Exception:
                        pass
                    break

                # 檢查是否被主持人拒絕或會議已結束
                if any(k in body_text for k in [
                    "you've been removed", "已被移出", "主持人已拒絕", "meeting has ended", "會議已結束", "you've left the meeting", "您已離開會議"
                ]):
                    print("[TeamsBot] 偵測到已被主持人移出或會議已結束，結束等待準備轉譯。")
                    break

                page.wait_for_timeout(3000)

            if not joined_call:
                if in_lobby:
                    raise Exception("在大廳等候超過時限，主持人未准許入會")
                else:
                    raise Exception("未能成功加入 Teams 會議 (可能是連結無效或需要特定組織帳號)")

            joined_meeting = True
            meeting_start_time = time.time()
            max_seconds = self.max_duration_min * 60
            solo_participant_counter = 0
            no_control_counter = 0

            # 5. 會議監聽與錄音中迴圈
            print(f"[TeamsBot] 進入會議錄音監聽迴圈 (最長時限: {self.max_duration_min} 分鐘)...")
            while not self.stop_event.is_set():
                elapsed = time.time() - meeting_start_time
                if elapsed >= max_seconds:
                    print(f"[TeamsBot] 已達預約最長錄音時限 ({self.max_duration_min} 分鐘)，準備自動離開會議。")
                    break

                page_content = page.content().lower()

                # 1. 檢查通話是否被主持人結束、被踢出或移出
                if any(k in page_content for k in [
                    "you were removed", "you've been removed", "you have been removed", "someone removed you",
                    "已被移出", "已從會議中移除", "已將您從會議中移除", "主持人已拒絕",
                    "the meeting has ended", "meeting has ended", "會議已結束",
                    "您已離開會議", "you've left the meeting", "通話已結束", "call ended"
                ]):
                    print("[TeamsBot] 偵測到會議已結束或被主持人移出，立即結束錄音並轉譯。")
                    break

                # 2. 檢查是否已跳離會議頁面 (例如被踢出後頁面跳轉回 launcher 或首頁)
                cur_url = page.url.lower()
                if not any(k in cur_url for k in ["light-meetings", "/meet/"]):
                    print(f"[TeamsBot] 偵測到已離開會議頁面 (當前網址: {cur_url})，結束錄音並轉譯。")
                    break

                # 3. 微微移動滑鼠防止 Teams 工具列休眠淡出
                try:
                    page.mouse.move(640, 360)
                except Exception:
                    pass

                # 4. 通話中持續確保麥克風靜音
                mute_incall_microphone(page)

                # 5. 檢查是否只剩下 Bot 一個人在會議室 (自動離席偵測)
                # 方式 A: 偵測會議畫面中出現「Waiting for others to join...」或「等待其他人加入」
                waiting_for_others = False
                try:
                    waiting_loc = page.locator('text="Waiting for others to join", :has-text("Waiting for others to join"), :has-text("等待其他人加入"), :has-text("等候其他人加入")').first
                    if waiting_loc.is_visible():
                        waiting_for_others = True
                except Exception:
                    pass

                solo_detected = waiting_for_others or any(k in page_content for k in [
                    "waiting for others to join",
                    "等待其他人加入",
                    "等候其他人加入",
                    "正在等待其他人加入",
                    "正在等候其他人加入",
                    "you're the only one here",
                    "只剩您一人",
                    "只有您一人",
                    "你是會議中唯一的人",
                    "everyone left",
                    "其他人已離開"
                ])

                # 方式 B: 即時偵測會議室在線人數 (Participant / Roster Count)
                try:
                    roster_btn = page.locator('button[data-tid="roster-button"], button[aria-label*="participant" i], button[aria-label*="參與者" i], button[aria-label*="people" i]').first
                    if roster_btn.is_visible():
                        btn_text = roster_btn.inner_text() or ""
                        aria_text = roster_btn.get_attribute("aria-label") or ""
                        nums = re.findall(r'\d+', f"{btn_text} {aria_text}")
                        if nums:
                            count = int(nums[0])
                            if int(elapsed) % 15 < 3:
                                print(f"[TeamsBot] 👥 會議室即時在線人數: {count} 人")
                            if count <= 1:
                                solo_detected = True
                except Exception:
                    pass

                if solo_detected:
                    solo_participant_counter += 1
                    print(f"[TeamsBot] 偵測到會議室內已無其他與會者 (Waiting for others to join / 只剩一人，連續判定: {solo_participant_counter}/2)...")
                    if solo_participant_counter >= 2: # 連續兩次偵測到 (~6秒)，代表會議其他人皆已離席
                        print("[TeamsBot] 偵測到會議室內僅剩助理一人，所有人皆已離席，自動退出並進行轉譯！")
                        break
                else:
                    solo_participant_counter = 0

                page.wait_for_timeout(3000)

            # 6. 優雅離開會議
            print("[TeamsBot] 正在離開 Teams 會議...")
            try:
                for sel in ['button[data-tid="hangup-button"]', 'button[data-tid="call-hangup"]', 'button[aria-label*="Leave" i]', 'button[aria-label*="離開" i]']:
                    btn = page.locator(sel).first
                    if btn.is_visible():
                        btn.click()
                        break
            except Exception:
                pass
            page.wait_for_timeout(2000)

        except Exception as e:
            print(f"[TeamsBot] 會議階段發生例外: {e}")
            has_audio = os.path.exists(self.audio_path) and os.path.getsize(self.audio_path) > 20480
            if not joined_meeting and not has_audio:
                now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                database.update_bot_task(self.task_id, status="failed", error_message=str(e), ended_at=now_str)
                self._cleanup(context, playwright_obj)
                return

        finally:
            self._cleanup(context, playwright_obj)

        # 7. 檢查錄音檔案並發起 AI 轉譯任務
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if os.path.exists(self.audio_path) and os.path.getsize(self.audio_path) > 20480: # 大於 20KB
            print(f"[TeamsBot] 錄音完成！檔案大小: {os.path.getsize(self.audio_path)} bytes")
            database.update_bot_task(
                self.task_id,
                status="transcribing",
                meeting_id=self.folder_name,
                ended_at=now_str
            )

            # 建立轉錄任務
            ai_task_id = f"task_{int(time.time() * 1000)}"
            if self.tasks_dict is not None:
                self.tasks_dict[ai_task_id] = {
                    "status": "processing",
                    "userId": self.user_id,
                    "meetingTitle": self.title,
                    "progress": 25,
                    "message": "Teams 自動撥入錄音完畢，正在進行雲端 AI 轉譯與摘要生成...",
                    "folderName": self.folder_name,
                    "meetingId": self.folder_name,
                    "created_at": now_str
                }

            # 啟動轉錄線程
            trans_thread = threading.Thread(
                target=self._run_transcription,
                args=(ai_task_id,),
                daemon=True
            )
            trans_thread.start()
        else:
            err = "會議結束，但錄音檔無有效聲音內容 (檔案過小或未錄製到聲音)"
            print(f"[TeamsBot] {err}")
            database.update_bot_task(self.task_id, status="failed", error_message=err, ended_at=now_str)

    def _run_transcription(self, ai_task_id):
        """執行後續 AI 轉錄，並於完成後更新 Bot 任務狀態為 completed"""
        try:
            print(f"[TeamsBot] 開始執行 AI 轉譯: {self.audio_path}")
            ai_transcriber.transcribe_audio_task(
                task_id=ai_task_id,
                audio_path=self.audio_path,
                user_id=self.user_id,
                user_title=self.title,
                tasks_dict=self.tasks_dict,
                profile_id=self.profile_id
            )
            database.update_bot_task(self.task_id, status="completed", meeting_id=self.folder_name)
            print(f"[TeamsBot] ===== 任務 {self.task_id} 全流程處理完成！=====")

            # 自動寄送 Email 會議紀錄
            if self.notify_email:
                try:
                    folder_dir = os.path.dirname(self.audio_path)
                    summary_content = ""
                    transcript_content = ""
                    for f in os.listdir(folder_dir):
                        if f.endswith("_會議摘要.txt") or f.endswith("summary.txt"):
                            with open(os.path.join(folder_dir, f), "r", encoding="utf-8") as sf:
                                summary_content = sf.read()
                        elif f.endswith("_逐字稿.txt") or f.endswith("transcript.txt"):
                            with open(os.path.join(folder_dir, f), "r", encoding="utf-8") as tf:
                                transcript_content = tf.read()

                    import email_service
                    email_service.send_meeting_summary_email(
                        to_email=self.notify_email,
                        meeting_title=self.title,
                        summary_text=summary_content,
                        transcript_text=transcript_content,
                        meeting_id=self.folder_name
                    )
                except Exception as mail_err:
                    print(f"[TeamsBot] 發送會議紀錄 Email 發生例外: {mail_err}")
        except Exception as e:
            print(f"[TeamsBot] 轉譯過程發生錯誤: {e}")
            database.update_bot_task(self.task_id, status="failed", error_message=f"AI 轉錄失敗: {e}")

    def _cleanup(self, context, playwright_obj):
        """關閉瀏覽器與 context、停止 FFmpeg 並卸載虛擬聲道"""
        try:
            if context:
                context.close()
        except Exception:
            pass
        try:
            if playwright_obj:
                playwright_obj.stop()
        except Exception:
            pass

        # 結束 FFmpeg 錄音並 flush
        if self.ffmpeg_proc:
            try:
                self.ffmpeg_proc.communicate(input=b'q', timeout=5)
            except Exception:
                try:
                    self.ffmpeg_proc.terminate()
                    self.ffmpeg_proc.wait(timeout=3)
                except Exception:
                    self.ffmpeg_proc.kill()
            self.ffmpeg_proc = None

        # 卸載 Null-Sink
        if self.sink_mod_id:
            unload_virtual_sink(self.sink_mod_id)
            self.sink_mod_id = None


class TeamsBotManager:
    """全域 Teams Bot 排程器與任務管理器"""
    def __init__(self, tasks_dict=None):
        self.tasks_dict = tasks_dict if tasks_dict is not None else {}
        self.active_sessions = {} # task_id -> {"session": ..., "stop_event": ...}
        self.lock = threading.Lock()
        self.running = False
        self.scheduler_thread = None

    def start_scheduler(self):
        """啟動背景定時排程輪詢 (每 5 秒檢查是否有到期預約)"""
        if self.running:
            return
        self.running = True
        self.scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self.scheduler_thread.start()
        print("[TeamsBotManager] 背景排程器已啟動，開始持續監聽預約任務...")

    def _scheduler_loop(self):
        while self.running:
            try:
                due_tasks = database.get_due_bot_tasks()
                for task in due_tasks:
                    task_id = task["id"]
                    with self.lock:
                        if task_id not in self.active_sessions:
                            print(f"[TeamsBotManager] 發現到期預約任務: {task_id} ({task['title']})，即刻調度撥入！")
                            self._spawn_bot_task(task)
            except Exception as e:
                print(f"[TeamsBotManager] Scheduler loop error: {e}")
            time.sleep(5)

    def trigger_task_immediately(self, task_id):
        """立即執行任務 (不用等排程時間)"""
        task = database.get_bot_task(task_id)
        if not task:
            return False, "任務不存在"
        with self.lock:
            if task_id in self.active_sessions:
                return False, "任務已在執行中"
            self._spawn_bot_task(task)
            return True, "已成功啟動立即撥入"

    def _spawn_bot_task(self, task):
        stop_event = threading.Event()
        session = TeamsBotSession(task, tasks_dict=self.tasks_dict, stop_event=stop_event)
        self.active_sessions[task["id"]] = {
            "session": session,
            "stop_event": stop_event
        }

        def worker():
            try:
                session.run()
            finally:
                with self.lock:
                    if task["id"] in self.active_sessions:
                        del self.active_sessions[task["id"]]

        t = threading.Thread(target=worker, daemon=True)
        t.start()

    def stop_task(self, task_id, user_id, is_admin=False):
        """提前結束進行中的會議錄音，並啟動轉譯"""
        task = database.get_bot_task(task_id)
        if not task:
            return False, "找不到指定任務"
        if not is_admin and task["user_id"] != user_id:
            return False, "無權限操作此任務"

        with self.lock:
            if task_id in self.active_sessions:
                print(f"[TeamsBotManager] 使用者請求手動結束錄音: {task_id}")
                self.active_sessions[task_id]["stop_event"].set()
                return True, "已送出提前結束指令，正在離開會議並進行 AI 轉錄"

        # 若不在執行階段，但狀態為 scheduled / in_lobby 等，直接標註取消
        database.cancel_bot_task(task_id, user_id, is_admin=is_admin)
        return True, "已取消排程任務"

# 全域單例
bot_manager = None

def get_bot_manager(tasks_dict=None):
    global bot_manager
    if bot_manager is None:
        bot_manager = TeamsBotManager(tasks_dict=tasks_dict)
        bot_manager.start_scheduler()
    return bot_manager
