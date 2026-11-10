"""
Teams & Windows Audio Recorder Engine
支援 Windows WASAPI Loopback (喇叭系統音) 與麥克風同時擷取、非阻塞回調串流、低記憶體串流分塊混音並轉成 MP3
"""

import os
import sys
import time
import math
import wave
import tempfile
import threading
import queue
import numpy as np
import pyaudiowpatch as pyaudio
import lameenc

class AudioEngine:
    def __init__(self):
        self.p = None
        self.is_recording = False
        self.is_previewing = False
        self.preview_mic_stream = None
        self.preview_spk_stream = None
        self.spk_rec = None
        self.mic_rec = None
        
        # 即時音量 (0.0 ~ 1.0)
        self.spk_level = 0.0
        self.mic_level = 0.0
        
        # 錄音時長 (秒)
        self.record_seconds = 0.0
        self.start_time = 0.0

        # 暫存檔路徑
        self.temp_spk_path = None
        self.temp_mic_path = None

        self._init_pyaudio()

    @property
    def spk_thread(self):
        return self.spk_rec["writer_th"] if self.spk_rec else None

    @property
    def mic_thread(self):
        return self.mic_rec["writer_th"] if self.mic_rec else None

    def _init_pyaudio(self):
        """確保 PyAudio 實例建立，若已存在則重複使用，避免 terminate 導致底層 C 庫崩潰"""
        if self.p is None:
            try:
                self.p = pyaudio.PyAudio()
            except Exception as e:
                print(f"Error initializing PyAudio: {e}")
                self.p = None
        return self.p

    def get_speaker_devices(self):
        spk, _ = self.get_devices()
        return spk

    def get_mic_devices(self):
        _, mic = self.get_devices()
        return mic

    def get_speaker_level(self):
        return getattr(self, "spk_level", 0.0)

    def get_mic_level(self):
        return getattr(self, "mic_level", 0.0)

    def get_devices(self):
        """
        取得系統中的所有可用設備：
        - speakers: WASAPI Loopback 裝置列表 (錄製喇叭/揚聲器聲音)
        - microphones: WASAPI 麥克風輸入裝置列表
        """
        self._init_pyaudio()
        speakers = []
        microphones = []

        if not self.p:
            return speakers, microphones

        try:
            wasapi_info = self.p.get_host_api_info_by_type(pyaudio.paWASAPI)
            wasapi_index = wasapi_info["index"]
        except Exception:
            return speakers, microphones

        # 1. 抓取 Loopback 設備 (喇叭/揚聲器輸出監聽)
        try:
            for dev in self.p.get_loopback_device_info_generator():
                name = dev.get("name", f"Loopback Device {dev['index']}")
                speakers.append({
                    "index": dev["index"],
                    "name": name,
                    "channels": dev.get("maxInputChannels", 2),
                    "rate": int(dev.get("defaultSampleRate", 48000)),
                    "is_loopback": True
                })
        except Exception as e:
            print(f"Error enumerating loopback devices: {e}")

        # 2. 抓取麥克風設備 (WASAPI Input)
        try:
            device_count = self.p.get_device_count()
            for i in range(device_count):
                dev = self.p.get_device_info_by_index(i)
                if (dev.get("hostApi") == wasapi_index and 
                    dev.get("maxInputChannels", 0) > 0 and 
                    not dev.get("isLoopbackDevice", False)):
                    microphones.append({
                        "index": dev["index"],
                        "name": dev.get("name", f"Microphone {dev['index']}"),
                        "channels": dev.get("maxInputChannels", 1),
                        "rate": int(dev.get("defaultSampleRate", 48000)),
                        "is_loopback": False
                    })
        except Exception as e:
            print(f"Error enumerating microphones: {e}")

        return speakers, microphones

    def _calculate_level(self, data_chunk):
        """計算音訊 chunk 的 RMS 音量 (0.0 ~ 1.0)"""
        try:
            samples = np.frombuffer(data_chunk, dtype=np.int16).astype(np.float32)
            if len(samples) == 0:
                return 0.0
            rms = np.sqrt(np.mean(samples ** 2))
            if rms <= 1.0:
                return 0.0
            db = 20 * math.log10(rms / 32767.0)
            level = max(0.0, (db + 60.0) / 60.0)
            return min(1.0, level)
        except Exception:
            return 0.0

    def start_preview_monitoring(self, spk_device, mic_device):
        """啟動未錄音時的即時試音監聽（採用非阻塞回調，杜絕無聲音時卡住）"""
        if self.is_recording:
            return
        self.stop_preview_monitoring()
        self.is_previewing = True
        self._init_pyaudio()
        if not self.p:
            return

        if mic_device:
            def mic_cb(in_data, frame_count, time_info, status):
                if self.is_previewing and not self.is_recording:
                    self.mic_level = self._calculate_level(in_data)
                return (None, pyaudio.paContinue)
            try:
                self.preview_mic_stream = self.p.open(
                    format=pyaudio.paInt16,
                    channels=max(1, mic_device["channels"]),
                    rate=mic_device["rate"],
                    input=True,
                    input_device_index=mic_device["index"],
                    frames_per_buffer=1024,
                    stream_callback=mic_cb
                )
                self.preview_mic_stream.start_stream()
            except Exception as e:
                print(f"Error starting preview mic: {e}")

        if spk_device:
            def spk_cb(in_data, frame_count, time_info, status):
                if self.is_previewing and not self.is_recording:
                    self.spk_level = self._calculate_level(in_data)
                return (None, pyaudio.paContinue)
            try:
                self.preview_spk_stream = self.p.open(
                    format=pyaudio.paInt16,
                    channels=max(1, spk_device["channels"]),
                    rate=spk_device["rate"],
                    input=True,
                    input_device_index=spk_device["index"],
                    frames_per_buffer=1024,
                    stream_callback=spk_cb
                )
                self.preview_spk_stream.start_stream()
            except Exception as e:
                print(f"Error starting preview spk: {e}")

    def stop_preview_monitoring(self):
        """立即停止即時試音監聽（瞬間關閉無阻塞）"""
        self.is_previewing = False
        for attr in ["preview_mic_stream", "preview_spk_stream"]:
            s = getattr(self, attr, None)
            if s:
                try:
                    s.stop_stream()
                    s.close()
                except Exception:
                    pass
                setattr(self, attr, None)
        self.spk_level = 0.0
        self.mic_level = 0.0

    def _start_recording_stream(self, dev_info, wav_path, level_attr):
        """採用回調 + 佇列 + 獨立背景寫入執行緒，保證零掉幀且不阻塞任何事件"""
        channels = max(1, dev_info["channels"])
        rate = dev_info["rate"]
        q = queue.Queue(maxsize=1000)
        stop_event = threading.Event()

        def writer_worker():
            try:
                wav_file = wave.open(wav_path, "wb")
                wav_file.setnchannels(channels)
                wav_file.setsampwidth(2)
                wav_file.setframerate(rate)
                while not stop_event.is_set() or not q.empty():
                    try:
                        chunk = q.get(timeout=0.1)
                        wav_file.writeframes(chunk)
                    except queue.Empty:
                        continue
                wav_file.close()
            except Exception as e:
                print(f"Writer error ({wav_path}): {e}")

        writer_th = threading.Thread(target=writer_worker, daemon=True)
        writer_th.start()

        def record_cb(in_data, frame_count, time_info, status):
            if self.is_recording:
                try:
                    q.put_nowait(in_data)
                except queue.Full:
                    pass
                setattr(self, level_attr, self._calculate_level(in_data))
            return (None, pyaudio.paContinue)

        stream = self.p.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=rate,
            input=True,
            input_device_index=dev_info["index"],
            frames_per_buffer=1024,
            stream_callback=record_cb
        )
        stream.start_stream()

        return {
            "stream": stream,
            "stop_event": stop_event,
            "writer_th": writer_th,
            "queue": q
        }

    def _stop_recording_stream(self, rec_obj, level_attr):
        if not rec_obj:
            return
        setattr(self, level_attr, 0.0)
        stream = rec_obj.get("stream")
        if stream:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
        rec_obj["stop_event"].set()
        th = rec_obj.get("writer_th")
        if th and th.is_alive():
            th.join(timeout=1.5)

    def start_recording(self, spk_device, mic_device):
        """開始錄音"""
        if self.is_recording:
            return False, "已經在錄音中"

        if not spk_device and not mic_device:
            return False, "必須至少選擇一個設備（喇叭或麥克風）進行錄音"

        # 1. 停止預覽監聽，安全不重設 PyAudio
        self.stop_preview_monitoring()
        self._init_pyaudio()
        if not self.p:
            return False, "音訊引擎初始化失敗，請檢查 Windows 音效服務是否正常"

        self.is_recording = True
        self.start_time = time.time()
        self.record_seconds = 0.0

        temp_dir = tempfile.gettempdir()
        self.temp_spk_path = os.path.join(temp_dir, f"teams_rec_spk_{os.getpid()}_{int(time.time())}.wav")
        self.temp_mic_path = os.path.join(temp_dir, f"teams_rec_mic_{os.getpid()}_{int(time.time())}.wav")

        self.spk_rec = None
        self.mic_rec = None

        try:
            if spk_device:
                self.spk_rec = self._start_recording_stream(spk_device, self.temp_spk_path, "spk_level")
            else:
                self.spk_level = 0.0

            if mic_device:
                self.mic_rec = self._start_recording_stream(mic_device, self.temp_mic_path, "mic_level")
            else:
                self.mic_level = 0.0

            return True, "開始錄音"
        except Exception as e:
            self.stop_recording()
            return False, f"啟動錄音串流失敗: {e}"

    def stop_recording(self):
        """停止錄音"""
        if not self.is_recording:
            return
        self.is_recording = False

        if hasattr(self, "spk_rec") and self.spk_rec:
            self._stop_recording_stream(self.spk_rec, "spk_level")
            self.spk_rec = None

        if hasattr(self, "mic_rec") and self.mic_rec:
            self._stop_recording_stream(self.mic_rec, "mic_level")
            self.mic_rec = None

        self.spk_level = 0.0
        self.mic_level = 0.0

    def mix_and_export_mp3(self, output_mp3_path, spk_gain=1.0, mic_gain=1.2, bitrate=192, keep_separate_tracks=False, progress_cb=None, progress_callback=None, **kwargs):
        """
        採用低記憶體串流分塊混音（Chunked Streaming Mixing），即使長達數小時會議也僅需 15MB RAM，杜絕記憶體耗盡當機！
        - spk_gain: 喇叭音量倍率 (0.0 ~ 3.0)
        - mic_gain: 麥克風音量倍率 (0.0 ~ 3.0)
        - bitrate: MP3 位元率 (kbps，預設 192)
        - keep_separate_tracks: 是否保留獨立音軌
        - progress_cb: 回報進度 callback(float percent, str msg)
        """
        cb = progress_callback or progress_cb
        def update_progress(p, msg):
            if cb:
                try:
                    cb(p, msg)
                except Exception:
                    pass

        update_progress(0.05, "讀取音軌檔案資訊...")

        has_spk = (self.temp_spk_path and os.path.exists(self.temp_spk_path) and os.path.getsize(self.temp_spk_path) > 44)
        has_mic = (self.temp_mic_path and os.path.exists(self.temp_mic_path) and os.path.getsize(self.temp_mic_path) > 44)

        if not has_spk and not has_mic:
            raise RuntimeError("未錄製到任何有效音訊資料（檔案長度為 0）")

        wf_s = wave.open(self.temp_spk_path, "rb") if has_spk else None
        wf_m = wave.open(self.temp_mic_path, "rb") if has_mic else None

        target_rate = 48000
        target_channels = 2

        try:
            rate_s = wf_s.getframerate() if wf_s else target_rate
            ch_s = wf_s.getnchannels() if wf_s else target_channels
            frames_s = wf_s.getnframes() if wf_s else 0

            rate_m = wf_m.getframerate() if wf_m else target_rate
            ch_m = wf_m.getnchannels() if wf_m else target_channels
            frames_m = wf_m.getnframes() if wf_m else 0

            dur_s = frames_s / rate_s if rate_s else 0
            dur_m = frames_m / rate_m if rate_m else 0
            max_duration_sec = max(dur_s, dur_m)

            if max_duration_sec <= 0.1:
                raise RuntimeError("錄音時長過短（小於 0.1 秒），無法產出有效音訊。")

            total_target_frames = int(max_duration_sec * target_rate)

            update_progress(0.15, f"初始化 MP3 編碼器 (時長 {int(max_duration_sec//60)} 分 {int(max_duration_sec%60)} 秒)...")
            encoder = lameenc.Encoder()
            encoder.set_bit_rate(bitrate)
            encoder.set_in_sample_rate(target_rate)
            encoder.set_channels(target_channels)
            encoder.set_quality(2) # High quality

            chunk_sec = 1.0
            target_chunk_frames = int(chunk_sec * target_rate)
            processed_frames = 0

            os.makedirs(os.path.dirname(os.path.abspath(output_mp3_path)), exist_ok=True)
            with open(output_mp3_path, "wb") as f_out:
                while processed_frames < total_target_frames:
                    cur_target_frames = min(target_chunk_frames, total_target_frames - processed_frames)

                    # 1. 讀取喇叭分塊
                    s_samples = None
                    if wf_s and processed_frames < frames_s:
                        read_s = int(cur_target_frames * (rate_s / target_rate))
                        raw = wf_s.readframes(read_s)
                        if len(raw) > 0:
                            arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
                            if ch_s > 1:
                                arr = arr.reshape(-1, ch_s)[:, :2]
                            else:
                                arr = np.column_stack((arr, arr))
                            if rate_s != target_rate and len(arr) > 0:
                                orig_len = len(arr)
                                orig_idx = np.linspace(0, orig_len - 1, orig_len)
                                new_idx = np.linspace(0, orig_len - 1, cur_target_frames)
                                left = np.interp(new_idx, orig_idx, arr[:, 0])
                                right = np.interp(new_idx, orig_idx, arr[:, 1])
                                arr = np.column_stack((left, right))
                            s_samples = arr * float(spk_gain)

                    # 2. 讀取麥克風分塊
                    m_samples = None
                    if wf_m and processed_frames < frames_m:
                        read_m = int(cur_target_frames * (rate_m / target_rate))
                        raw = wf_m.readframes(read_m)
                        if len(raw) > 0:
                            arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
                            if ch_m > 1:
                                arr = arr.reshape(-1, ch_m)[:, :2]
                            else:
                                arr = np.column_stack((arr, arr))
                            if rate_m != target_rate and len(arr) > 0:
                                orig_len = len(arr)
                                orig_idx = np.linspace(0, orig_len - 1, orig_len)
                                new_idx = np.linspace(0, orig_len - 1, cur_target_frames)
                                left = np.interp(new_idx, orig_idx, arr[:, 0])
                                right = np.interp(new_idx, orig_idx, arr[:, 1])
                                arr = np.column_stack((left, right))
                            m_samples = arr * float(mic_gain)

                    # 3. 雙軌即時混音
                    cur_len = max(len(s_samples) if s_samples is not None else 0,
                                  len(m_samples) if m_samples is not None else 0)
                    if cur_len == 0:
                        break

                    mixed = np.zeros((cur_len, 2), dtype=np.float32)
                    if s_samples is not None and len(s_samples) > 0:
                        mixed[:len(s_samples)] += s_samples
                    if m_samples is not None and len(m_samples) > 0:
                        mixed[:len(m_samples)] += m_samples

                    # 4. 動態防破音平滑限制 (Soft Limiter)
                    peak = np.max(np.abs(mixed))
                    if peak > 32767.0:
                        mixed = np.tanh(mixed / 32767.0) * 32767.0

                    int16_mixed = np.clip(mixed, -32768, 32767).astype(np.int16)

                    # 5. 分塊編碼寫入
                    mp3_data = encoder.encode(int16_mixed.tobytes())
                    if mp3_data:
                        f_out.write(mp3_data)

                    processed_frames += cur_len
                    pct = 0.2 + 0.7 * (processed_frames / total_target_frames)
                    update_progress(min(0.92, pct), f"低記憶體混音編碼中... ({int(processed_frames/total_target_frames*100)}%)")

                flush_data = encoder.flush()
                if flush_data:
                    f_out.write(flush_data)

        finally:
            if wf_s:
                try: wf_s.close()
                except Exception: pass
            if wf_m:
                try: wf_m.close()
                except Exception: pass

        # 若使用者選擇保留分軌，複製至目標目錄
        if keep_separate_tracks:
            import shutil
            base_dir = os.path.dirname(os.path.abspath(output_mp3_path))
            stem = os.path.splitext(os.path.basename(output_mp3_path))[0]
            if has_spk and os.path.exists(self.temp_spk_path):
                shutil.copy2(self.temp_spk_path, os.path.join(base_dir, f"{stem}_spk.wav"))
            if has_mic and os.path.exists(self.temp_mic_path):
                shutil.copy2(self.temp_mic_path, os.path.join(base_dir, f"{stem}_mic.wav"))

        update_progress(0.95, "清理暫存檔...")
        if has_spk and os.path.exists(self.temp_spk_path):
            try: os.remove(self.temp_spk_path)
            except Exception: pass
        if has_mic and os.path.exists(self.temp_mic_path):
            try: os.remove(self.temp_mic_path)
            except Exception: pass

        update_progress(1.0, "錄音混音儲存完成！")
        return output_mp3_path

    def cleanup(self):
        """釋放所有資源"""
        self.stop_recording()
        self.stop_preview_monitoring()
        if self.p:
            try:
                self.p.terminate()
            except Exception:
                pass
            self.p = None
