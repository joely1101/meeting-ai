import os
import sys
import json
import time
import datetime
import base64
import re
import shutil
import subprocess
import requests
from pathlib import Path
import database

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SERVER_DIR, "..", "data")
CONFIG_PATH = os.path.join(SERVER_DIR, "config.json")

# 30 分鐘切片閥值 (1800 秒)
CHUNK_DURATION_SEC = 1800

# 預設候選模型優先順序（最新穩定模型優先）
CANDIDATE_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gemini-flash-latest",
    "gemini-flash-lite-latest"
]

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
    user_key = database.get_user_api_key(user_id)
    if user_key and (not exclude_keys or user_key not in exclude_keys):
        return user_key, "使用者自訂 API Key"

    pool_key_obj = database.get_next_balanced_api_key(exclude_keys=exclude_keys)
    if pool_key_obj:
        return pool_key_obj["api_key"], f"系統 Key Pool (ID: {pool_key_obj['id']}, 次數: {pool_key_obj.get('usage_count', 0)})"

    cfg = load_config()
    cfg_key = cfg.get("gemini_api_key", "").strip()
    if cfg_key and (not exclude_keys or cfg_key not in exclude_keys):
        return cfg_key, "伺服器預設設定檔"

    return None, None

def get_ffmpeg_path():
    """尋找可用 ffmpeg 執行檔路徑 (支援原生 PATH、imageio-ffmpeg 及系統預設)"""
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return "ffmpeg"

def get_audio_duration(audio_path):
    """透過 ffmpeg 取得音訊總秒數"""
    ff = get_ffmpeg_path()
    cmd = [ff, "-i", audio_path]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="ignore")
        m = re.search(r'Duration:\s*(\d+):(\d+):(\d+\.?\d*)', p.stderr)
        if m:
            h, m_, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return int(h * 3600 + m_ * 60 + s)
    except Exception as e:
        print(f"[AI Transcriber] 讀取音訊長度異常: {e}", flush=True)
    return 0

def format_timestamp(total_sec):
    h = total_sec // 3600
    m = (total_sec % 3600) // 60
    s = total_sec % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def parse_relative_time(t_str):
    parts = [int(x) for x in t_str.strip().split(':')]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    elif len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0]

def call_gemini_api(api_key, payload, candidate_models=None, timeout=300):
    """嘗試候選模型呼叫 Gemini API，自帶錯誤容錯重試"""
    if candidate_models is None:
        candidate_models = CANDIDATE_MODELS

    last_err = ""
    for model_name in candidate_models:
        model_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        try:
            print(f"[AI Transcriber] 調度模型 {model_name}...", flush=True)
            resp = requests.post(model_url, json=payload, headers={"Content-Type": "application/json"}, timeout=timeout)
            if resp.status_code == 200:
                return resp.json(), model_name
            elif resp.status_code == 503 or resp.status_code == 429:
                last_err = f"模型 {model_name} 忙碌 ({resp.status_code})"
                print(f"[AI Transcriber] {last_err}，嘗試切換模型...", flush=True)
            else:
                last_err = f"模型 {model_name} 錯誤 ({resp.status_code}): {resp.text[:150]}"
                print(f"[AI Transcriber] {last_err}", flush=True)
        except Exception as e:
            last_err = f"模型 {model_name} 連線異常: {e}"
            print(f"[AI Transcriber] {last_err}", flush=True)

    raise RuntimeError(f"Gemini API 請求失敗：\n{last_err}")

def transcribe_audio_task(task_id, audio_path, user_id, user_title="", tasks_dict=None, profile_id=None, custom_prompt=""):
    if tasks_dict is None:
        tasks_dict = {}

    try:
        tasks_dict[task_id]["status"] = "processing"
        tasks_dict[task_id]["progress"] = 10
        tasks_dict[task_id]["message"] = "音訊已接收，正在為此任務調度 Gemini API Key..."

        api_key, key_source = get_effective_api_key(user_id)
        if not api_key:
            raise RuntimeError("系統目前無可用的 Google Gemini API Key，請通知管理員新增 Key 或於個人設定填入自訂 Key。")

        print(f"[AI Transcriber] Task {task_id} 使用金鑰來源: {key_source}", flush=True)
        tasks_dict[task_id]["progress"] = 15
        tasks_dict[task_id]["message"] = f"已調度金鑰 ({key_source})，正在偵測音訊規格與時長..."

        # 載入並套用會議分類 Profile 與提示詞
        profile = None
        if profile_id:
            try:
                profile = database.get_profile_by_id(int(profile_id), user_id)
            except Exception:
                profile = None
        if not profile:
            profile = database.get_default_profile_for_user(user_id)

        profile_name = profile["name"] if profile else ""
        profile_id_val = profile["id"] if profile else None

        profile_instructions = []
        if profile:
            if profile.get("name"):
                profile_instructions.append(f"【會議分類情境】：{profile['name']}")
            if profile.get("location_context"):
                profile_instructions.append(f"【開會背景/環境】：{profile['location_context']}")
            if profile.get("speakers"):
                profile_instructions.append(f"【已知與會人員名單（核心依據）】：{profile['speakers']}（本場會議主要由上述名單中的人員發言。請務必依據語調、聲音特徵及上下文稱呼，優先將說話者精確標註為對應姓名）")
            if profile.get("keywords"):
                profile_instructions.append(f"【專有名詞與熱詞庫】：{profile['keywords']}（音訊中若有發音相近、同音異字或模糊詞彙，請務必以此詞庫進行專業校正）")
            if profile.get("prompt_hint"):
                profile_instructions.append(f"【特殊轉譯與摘要指示】：{profile['prompt_hint']}")
        if custom_prompt and custom_prompt.strip():
            profile_instructions.append(f"【本次會議補充關鍵字/說明】：{custom_prompt.strip()}")

        profile_prompt_block = "\n".join(profile_instructions)
        if profile_name:
            print(f"[AI Transcriber] 套用會議 Profile: {profile_name} (ID: {profile_id_val})", flush=True)

        file_size = os.path.getsize(audio_path)
        audio_dur_sec = get_audio_duration(audio_path)
        print(f"[AI Transcriber] 音訊檔案大小: {file_size // 1024} KB, 偵測時長: {audio_dur_sec} 秒 ({format_timestamp(audio_dur_sec)})", flush=True)

        # 判斷是否需要進行 30 分鐘分段切片轉錄
        if audio_dur_sec > CHUNK_DURATION_SEC:
            print(f"[AI Transcriber] 音訊超過 30 分鐘 ({audio_dur_sec}s > {CHUNK_DURATION_SEC}s)，啟動 30 分鐘自動切片流水線！", flush=True)
            summary_text, transcript_text, max_sec = transcribe_chunked_30m(
                task_id, audio_path, audio_dur_sec, api_key, key_source, tasks_dict, profile_prompt_block=profile_prompt_block
            )
        else:
            print(f"[AI Transcriber] 音訊未滿 30 分鐘，採用快速單次轉錄模式", flush=True)
            summary_text, transcript_text, max_sec = transcribe_single_pass(
                task_id, audio_path, file_size, api_key, key_source, tasks_dict, profile_prompt_block=profile_prompt_block
            )

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
        time_pattern = re.compile(r'^\[(\d+(?::\d{2}){1,2})\]\s*(?:\[([^\]]+)\]|\(([^)]+)\)|([^：:\n\[\(]{1,30})[：:])?\s*(.*)$')
        speakers = set()
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
                sec = parse_relative_time(t_str)
                if sec > max_sec:
                    max_sec = sec
                dialogue_count += 1

        if not speakers and dialogue_count > 0:
            speakers.add('說話者')

        dur_str = format_timestamp(max_sec)

        # 決定會議標題
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
            "dialogue_count": dialogue_count,
            "profile_id": profile_id_val,
            "profile_name": profile_name
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
        print(f"[AI Transcriber] Task {task_id} 執行失敗: {e}", flush=True)

def transcribe_chunked_30m(task_id, audio_path, total_sec, api_key, key_source, tasks_dict, profile_prompt_block=""): 
    """
    30 分鐘自動切片轉錄流水線：
    1. 將超長音訊依 30 分鐘（1800 秒）切成小於 15MB 之切片
    2. 循序調度 Gemini 模型轉錄各片段，自動將時間軸重對齊為整場絕對時間戳
    3. 全局第二階段純文字整合，提煉完整結構化會議摘要
    """
    ff = get_ffmpeg_path()
    folder_path = os.path.dirname(audio_path)
    stem = Path(audio_path).stem
    temp_dir = os.path.join(folder_path, f"_chunks_{task_id}")
    os.makedirs(temp_dir, exist_ok=True)

    num_chunks = (total_sec + CHUNK_DURATION_SEC - 1) // CHUNK_DURATION_SEC
    print(f"[AI Transcriber] 總時長 {total_sec} 秒，預計分割為 {num_chunks} 個 30 分鐘切片", flush=True)

    all_adjusted_lines = []
    prev_speakers = set()
    line_pattern = re.compile(r'^\[(\d+(?::\d{2}){1,2})\]\s*(.*)$')

    try:
        for idx in range(num_chunks):
            start_sec = idx * CHUNK_DURATION_SEC
            dur_sec = min(CHUNK_DURATION_SEC, total_sec - start_sec)
            start_str = format_timestamp(start_sec)
            end_str = format_timestamp(start_sec + dur_sec)

            # 更新進度條
            pct = 20 + int((idx / num_chunks) * 60)
            tasks_dict[task_id]["progress"] = pct
            tasks_dict[task_id]["message"] = f"正在轉錄第 {idx+1}/{num_chunks} 段音訊（{start_str} ~ {end_str}）..."

            clip_path = os.path.join(temp_dir, f"chunk_{idx:02d}.mp3")
            cut_cmd = [
                ff, "-y",
                "-ss", str(start_sec),
                "-t", str(dur_sec),
                "-i", audio_path,
                "-ac", "1",
                "-b:a", "64k",
                clip_path
            ]
            subprocess.run(cut_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            if not os.path.exists(clip_path) or os.path.getsize(clip_path) == 0:
                print(f"[AI Transcriber] 切片失敗: chunk_{idx:02d}.mp3", flush=True)
                continue

            with open(clip_path, "rb") as cf:
                clip_b64 = base64.b64encode(cf.read()).decode("utf-8")

            # 跨切片角色提示
            speaker_hint = ""
            if prev_speakers:
                speaker_hint = f"前一段討論主要出現的說話者有：{', '.join(sorted(list(prev_speakers)))}。請盡量延續人物聲音與名稱識別。\n"

            chunk_prompt = f"""這是一段商務會議錄音切片（第 {idx+1}/{num_chunks} 段，時間約 {start_str} ~ {end_str}）。
請將這段音訊轉錄為精確的逐字稿，要求如下：
1. 格式：[MM:SS] [說話者]: 對話內容
2. 時間軸 [MM:SS] 請標註相對於本切片開頭的時間。
3. 必須嚴格進行人物聲音識別 (Speaker Diarization)，精準分離不同的說話者（例如：[說話者 A]、[說話者 B]，若對話中有提及姓名或有與會者名單如 [Iris]、[Adam]、[Steven] 請直接標註真實姓名）。嚴禁將所有人全部標記為相同的 [說話者]。
{speaker_hint}
【會議設定檔與熱詞參考】：
{profile_prompt_block}

4. 忠實保留說話者原文，切勿翻譯，切勿重複產生內容。
5. 只輸出逐字稿內容，不要包含開頭或結尾贅詞。
"""

            payload = {
                "contents": [{
                    "parts": [
                        {"inlineData": {"mimeType": "audio/mp3", "data": clip_b64}},
                        {"text": chunk_prompt}
                    ]
                }],
                "generationConfig": {
                    "temperature": 0.15
                }
            }

            res_json, used_model = call_gemini_api(api_key, payload)
            raw_chunk_text = res_json.get("candidates", [])[0].get("content", {}).get("parts", [{}])[0].get("text", "")

            # 轉換時間戳為整場絕對時間戳
            chunk_speakers = set()
            for line in raw_chunk_text.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                m = line_pattern.match(line)
                if m:
                    rel_sec = parse_relative_time(m.group(1))
                    abs_sec = start_sec + rel_sec
                    abs_time_str = format_timestamp(abs_sec)
                    rest = m.group(2)
                    # 擷取說話者名
                    spk_m = re.search(r'\[([^\]]+)\]', rest)
                    if spk_m:
                        chunk_speakers.add(spk_m.group(1).strip())
                    all_adjusted_lines.append(f"[{abs_time_str}] {rest}")
                else:
                    all_adjusted_lines.append(line)

            if chunk_speakers:
                prev_speakers = chunk_speakers

            # 清理切片暫存
            try:
                os.remove(clip_path)
            except Exception:
                pass

        full_transcript = "\n".join(all_adjusted_lines)

        # 第二階段：純文字全局提煉會議摘要
        tasks_dict[task_id]["progress"] = 85
        tasks_dict[task_id]["message"] = "各切片轉錄完成，正在提煉全局結構化會議摘要與待辦清單..."

        summary_prompt = f"""請根據以下完整的會議逐字稿內容，生成一份清晰、專業且高度結構化的會議總結。

格式要求如下：
### 📊 會議摘要 (繁體中文)
- 📌 核心重點與討論主題 (條列式整理)
- ✅ 待辦事項與決策指派 (Action Items)

逐字稿內容如下：
{full_transcript[:150000]}
"""
        sum_payload = {
            "contents": [{"parts": [{"text": summary_prompt}]}],
            "generationConfig": {"temperature": 0.2}
        }
        sum_res, _ = call_gemini_api(api_key, sum_payload)
        summary_text = sum_res.get("candidates", [])[0].get("content", {}).get("parts", [{}])[0].get("text", "")

        return summary_text, full_transcript, total_sec

    finally:
        # 刪除切片暫存目錄
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass

def transcribe_single_pass(task_id, audio_path, file_size, api_key, key_source, tasks_dict, profile_prompt_block=""): 
    """30 分鐘以內短音訊，走快速單次全檔轉錄"""
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
    tasks_dict[task_id]["message"] = "雲端 AI 正在進行多角色發言分離、逐字稿與會議摘要整理..."

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
4. 必須嚴格進行人物聲音識別 (Speaker Diarization)，精確分離每位不同的發言人：
   - 務必依據聲音音調、音色特徵、語調高低、口音以及對話中的相互稱呼（例如發言前提到對方名字或問答互動）仔細區分不同說話者。
   - 若有提供【已知與會人員名單】，請優先將各段發言比對歸屬至名單中的人物姓名（例如：「[Joel]」、「[Paul]」、「[Ivor]」等）。
   - 若無法確認對應姓名，也必須依據聲線特徵明確標記不同代號（例如：「[說話者 A]」、「[說話者 B]」、「[說話者 C]」）。
   - 【重要防呆規範】：本錄音為多人會議，**嚴禁將整場會議所有發言全部標為同一個 [說話者]**，必須確實標出對話輪替與不同角色！
   每句格式範例：
   [00:15] [說話者 A]: 大家好，今天會議開始。
   [00:20] [說話者 B]: 收到，請繼續。
5. 保持語句通順，自動校正語音辨識的同音錯字與常見 IT/商業術語。
{summary_instruction}
"""

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

    result_json, used_model = call_gemini_api(api_key, payload)
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

    return summary_text, transcript_text, 0
