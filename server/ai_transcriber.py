import os
import sys
import json
import time
import datetime
import base64
import re
import requests
from pathlib import Path
import database

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SERVER_DIR, "..", "data")
CONFIG_PATH = os.path.join(SERVER_DIR, "config.json")

def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def get_effective_api_key(user_id, exclude_keys=None):
    """
    決定當前任務使用的 Google Gemini API Key：
    1. 優先檢查使用者是否有自訂專屬 API Key (User Settings)
    2. 若無自訂 Key，則自系統 Key Pool 動態調度最少使用 (Least-Used) 的 Key
    3. 若 Key Pool 無可用 Key，則回退至 config.json
    """
    # 1. 使用者自訂 Key
    user_key = database.get_user_api_key(user_id)
    if user_key and (not exclude_keys or user_key not in exclude_keys):
        return user_key, "使用者自訂 API Key"

    # 2. 系統 Key Pool 動態負載平衡
    pool_key_obj = database.get_next_balanced_api_key(exclude_keys=exclude_keys)
    if pool_key_obj:
        return pool_key_obj["api_key"], f"系統 Key Pool (ID: {pool_key_obj['id']}, 次數: {pool_key_obj.get('usage_count', 0)})"

    # 3. 回退至 config.json
    cfg = load_config()
    cfg_key = cfg.get("gemini_api_key", "").strip()
    if cfg_key and (not exclude_keys or cfg_key not in exclude_keys):
        return cfg_key, "伺服器預設設定檔"

    return None, None

def transcribe_audio_task(task_id, audio_path, user_id, user_title="", tasks_dict=None):
    if tasks_dict is None:
        tasks_dict = {}

    try:
        tasks_dict[task_id]["status"] = "processing"
        tasks_dict[task_id]["progress"] = 20
        tasks_dict[task_id]["message"] = "音訊已接收，正在為此任務調度 Gemini API Key..."

        api_key, key_source = get_effective_api_key(user_id)
        if not api_key:
            raise RuntimeError("系統目前無可用的 Google Gemini API Key，請通知管理員新增 Key 或於個人設定填入自訂 Key。")

        print(f"[AI Transcriber] Task {task_id} 使用金鑰來源: {key_source}")
        tasks_dict[task_id]["progress"] = 28
        tasks_dict[task_id]["message"] = f"已調度金鑰 ({key_source})，正在準備送往 Google Gemini AI 雲端模型..."

        file_size = os.path.getsize(audio_path)
        ext = os.path.splitext(audio_path)[1].lower()
        mime_types = {
            ".mp3": "audio/mp3",
            ".wav": "audio/wav",
            ".m4a": "audio/m4a",
            ".aac": "audio/aac",
            ".ogg": "audio/ogg",
            ".flac": "audio/flac"
        }
        mime_type = mime_types.get(ext, "audio/mp3")

        # 小檔案走 inline base64，大檔案走 Google File API
        if file_size <= 15 * 1024 * 1024:
            tasks_dict[task_id]["progress"] = 35
            tasks_dict[task_id]["message"] = f"檔案約 {file_size // 1024} KB，正在打包資料..."
            with open(audio_path, "rb") as f:
                audio_b64 = base64.b64encode(f.read()).decode("utf-8")
            audio_part = {
                "inlineData": {
                    "mimeType": mime_type,
                    "data": audio_b64
                }
            }
        else:
            tasks_dict[task_id]["progress"] = 35
            tasks_dict[task_id]["message"] = f"檔案較大 ({file_size // (1024*1024)} MB)，正在上傳至 Google 檔案伺服器..."
            upload_init_url = f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={api_key}"
            headers = {
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(file_size),
                "X-Goog-Upload-Header-Content-Type": mime_type,
                "Content-Type": "application/json"
            }
            meta = {"file": {"displayName": os.path.basename(audio_path)}}
            res_init = requests.post(upload_init_url, headers=headers, json=meta)
            if res_init.status_code != 200:
                raise RuntimeError(f"初始化 Google 檔案上傳失敗 ({res_init.status_code}): {res_init.text}")

            upload_url = res_init.headers.get("X-Goog-Upload-URL")
            if not upload_url:
                raise RuntimeError("無法取得 Google 上傳網址。")

            with open(audio_path, "rb") as f:
                upload_headers = {
                    "Content-Length": str(file_size),
                    "X-Goog-Upload-Offset": "0",
                    "X-Goog-Upload-Command": "upload, finalize"
                }
                res_upload = requests.post(upload_url, headers=upload_headers, data=f)
            if res_upload.status_code != 200:
                raise RuntimeError(f"音訊上傳失敗 ({res_upload.status_code}): {res_upload.text}")

            file_info = res_upload.json().get("file", {})
            file_uri = file_info.get("uri")
            file_name = file_info.get("name")
            file_state = file_info.get("state")

            if file_state == "PROCESSING" and file_name:
                tasks_dict[task_id]["message"] = "雲端伺服器正在準備音訊檔案..."
                for _ in range(30):
                    time.sleep(2)
                    check_url = f"https://generativelanguage.googleapis.com/v1beta/{file_name}?key={api_key}"
                    check_res = requests.get(check_url)
                    if check_res.status_code == 200:
                        info = check_res.json()
                        if info.get("state") == "ACTIVE":
                            file_uri = info.get("uri", file_uri)
                            break
                        elif info.get("state") == "FAILED":
                            raise RuntimeError("Google 檔案處理失敗")

            audio_part = {
                "fileData": {
                    "mimeType": mime_type,
                    "fileUri": file_uri
                }
            }

        tasks_dict[task_id]["progress"] = 60
        tasks_dict[task_id]["message"] = "雲端 AI 正在進行多角色發言分離、逐字稿與會議摘要整理（請耐心等候）..."

        summary_instruction = """
請務必使用以下特定標籤包裹【會議摘要】：
===SUMMARY_START===
### 📊 會議摘要 (繁體中文)
- 📌 核心重點與討論主題 (條列式整理)
- ✅ 待辦事項與決策指派 (Action Items)
===SUMMARY_END===
**重要：無論會議是英文、中文或中英雙語，【會議摘要】請一律使用清晰的「繁體中文」進行結構化總結。**
"""

        prompt = f"""這是一段商務會議錄音（語言可能是英文、繁體中文或中英混雜）。
請將這段音訊轉錄為精確的逐字稿，要求如下：
1. 請嚴格使用以下標籤包裹逐字稿內容：
===TRANSCRIPT_START===
逐字稿內容
===TRANSCRIPT_END===
2. 語言處理原則：
   - 【逐字稿】：必須忠實保留說話者的「原文」（英文發言就記錄英文原文，中文發言就記錄繁體中文，中英夾雜則維持原樣，切勿將逐字稿翻譯）。
   - 【會議摘要】：無論錄音使用何種語言，一律使用「繁體中文」進行重點總結。
3. 逐字稿必須標註時間軸 [MM:SS] 或 [HH:MM:SS]。
4. 必須進行人物聲音識別 (Speaker Diarization)，明確區分不同的說話者（例如：「說話者 A」、「說話者 B」，或若對話中有提及姓名可直接標註例如「[Shelly]」、「[Joel]」）。
5. 保持語句通順，自動校正語音辨識的同音錯字與常見 IT/商業術語。
{summary_instruction}
"""

        candidate_models = [
            "gemini-3.6-flash",
            "gemini-3.5-flash",
            "gemini-3.8-flash",
            "gemini-flash-latest",
            "gemini-flash-lite-latest",
            "gemini-3.7-flash"
        ]

        payload = {
            "contents": [{
                "parts": [
                    audio_part,
                    {"text": prompt}
                ]
            }],
            "generationConfig": {
                "temperature": 0.15
            }
        }

        resp = None
        last_err = ""
        for model_name in candidate_models:
            model_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
            try:
                print(f"[AI Transcriber] 正在嘗試調度模型 {model_name}...", flush=True)
                resp = requests.post(model_url, json=payload, headers={"Content-Type": "application/json"}, timeout=300)
                if resp.status_code == 200:
                    print(f"[AI Transcriber] 模型 {model_name} 轉錄請求成功！", flush=True)
                    break
                else:
                    last_err = f"模型 {model_name} 錯誤 ({resp.status_code}): {resp.text}"
                    print(f"[AI Transcriber] {last_err}", flush=True)
            except Exception as e:
                last_err = f"模型 {model_name} 連線異常: {e}"
                print(f"[AI Transcriber] {last_err}", flush=True)

        if not resp or resp.status_code != 200:
            raise RuntimeError(f"Gemini API 請求失敗：\n{last_err}")

        result_json = resp.json()
        candidates = result_json.get("candidates", [])
        if not candidates:
            raise RuntimeError(f"未能取得轉錄內容：\n{result_json}")

        raw_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
        summary_text = ""
        transcript_text = raw_text

        if "===SUMMARY_START===" in raw_text and "===SUMMARY_END===" in raw_text:
            summary_text = raw_text.split("===SUMMARY_START===")[1].split("===SUMMARY_END===")[0].strip()

        if "===TRANSCRIPT_START===" in raw_text and "===TRANSCRIPT_END===" in raw_text:
            transcript_text = raw_text.split("===TRANSCRIPT_START===")[1].split("===TRANSCRIPT_END===")[0].strip()
        elif "===SUMMARY_END===" in raw_text:
            transcript_text = raw_text.split("===SUMMARY_END===")[1].strip()

        # 儲存摘要與逐字稿至同資料夾
        folder_path = os.path.dirname(audio_path)
        stem = Path(audio_path).stem
        summary_file = os.path.join(folder_path, f"{stem}_會議摘要.txt")
        transcript_file = os.path.join(folder_path, f"{stem}_逐字稿.txt")

        with open(summary_file, "w", encoding="utf-8") as sf:
            sf.write(summary_text)

        with open(transcript_file, "w", encoding="utf-8") as tf:
            tf.write(transcript_text)

        # 解析逐字稿以統計發言人與最大時長
        time_pattern = re.compile(r'^\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(?:\[([^\]]+)\]|([^：:\n]+)[：:])\s*(.*)$')
        speakers = set()
        max_sec = 0
        dialogue_count = 0

        for line in transcript_text.splitlines():
            m = time_pattern.match(line.strip())
            if m:
                t_str, s1, s2, txt = m.groups()
                spk = (s1 or s2).strip()
                speakers.add(spk)
                parts = t_str.split(':')
                if len(parts) == 3:
                    sec = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                else:
                    sec = int(parts[0]) * 60 + int(parts[1])
                if sec > max_sec:
                    max_sec = sec
                dialogue_count += 1

        dur_m = max_sec // 60
        dur_s = max_sec % 60
        dur_str = f"{dur_m:02d}:{dur_s:02d}"

        # 決定標題
        final_title = user_title.strip() if user_title else ""
        if not final_title and summary_text:
            for sl in summary_text.splitlines():
                clean_sl = sl.strip().lstrip('#-*• \t')
                if clean_sl and len(clean_sl) > 4 and "摘要" not in clean_sl:
                    final_title = clean_sl
                    break
        if not final_title:
            final_title = stem

        # 寫入資料庫
        folder_name = os.path.basename(folder_path)
        meeting_id = folder_name
        today_str = datetime.datetime.now().strftime("%Y/%m/%d")
        date_m = re.search(r'202\d[01]\d[0-3]\d', folder_name)
        if date_m:
            raw_d = date_m.group(0)
            today_str = f"{raw_d[:4]}/{raw_d[4:6]}/{raw_d[6:]}"

        meeting_record = {
            "id": meeting_id,
            "user_id": user_id,
            "title": final_title,
            "date": today_str,
            "duration_sec": max_sec,
            "duration_str": dur_str,
            "audio_file": os.path.basename(audio_path),
            "summary_file": os.path.basename(summary_file),
            "transcript_file": os.path.basename(transcript_file),
            "speakers": sorted(list(speakers)),
            "dialogue_count": dialogue_count
        }

        database.save_meeting_record(meeting_record)

        tasks_dict[task_id]["progress"] = 100
        tasks_dict[task_id]["status"] = "done"
        tasks_dict[task_id]["meetingId"] = meeting_id
        tasks_dict[task_id]["message"] = "會議轉錄與摘要生成完成！"

    except Exception as e:
        tasks_dict[task_id]["status"] = "error"
        tasks_dict[task_id]["error"] = str(e)
        tasks_dict[task_id]["message"] = f"轉錄失敗: {e}"
