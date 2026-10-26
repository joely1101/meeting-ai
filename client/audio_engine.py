"""
Teams & Windows Audio Recorder Engine
支援 Windows WASAPI Loopback (喇叭系統音) 與麥克風同時擷取、混音並轉成 MP3
"""

import os
import sys
import time
import math
import wave
import tempfile
import threading
import numpy as np
import pyaudiowpatch as pyaudio
import lameenc

class AudioEngine:
    def __init__(self):
        self.p = None
        self.is_recording = False
        self.spk_thread = None
        self.mic_thread = None
        
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

    def _init_pyaudio(self):
        try:
            if self.p is not None:
                try:
                    self.p.terminate()
                except Exception:
                    pass
            self.p = pyaudio.PyAudio()
        except Exception as e:
            print(f"Error initializing PyAudio: {e}")
            self.p = None

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
            # 如果沒有 WASAPI，回傳空列表
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
            # 轉換為對數刻度並標準化為 0~1
            if rms <= 1.0:
                return 0.0
            db = 20 * math.log10(rms / 32767.0)
            # -60 dB 到 0 dB 映射至 0.0 ~ 1.0
            level = max(0.0, (db + 60.0) / 60.0)
            return min(1.0, level)
        except Exception:
            return 0.0

    def _record_stream(self, dev_info, wav_path, level_attr):
        """獨立串流錄音執行緒，直接串流寫入暫存 WAV 檔"""
        channels = max(1, dev_info["channels"])
        rate = dev_info["rate"]
        dev_index = dev_info["index"]
        chunk_size = 1024

        wav_file = wave.open(wav_path, "wb")
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2) # 16-bit PCM
        wav_file.setframerate(rate)

        stream = None
        try:
            stream = self.p.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=rate,
                input=True,
                input_device_index=dev_index,
                frames_per_buffer=chunk_size
            )

            while self.is_recording:
                try:
                    data = stream.read(chunk_size, exception_on_overflow=False)
                    wav_file.writeframes(data)
                    # 更新音量表
                    lvl = self._calculate_level(data)
                    setattr(self, level_attr, lvl)
                except Exception as e:
                    # 避免單次讀取錯誤導致錄音中斷
                    time.sleep(0.01)

        except Exception as e:
            print(f"Stream error for {dev_info['name']}: {e}")
        finally:
            setattr(self, level_attr, 0.0)
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            wav_file.close()

    def start_recording(self, spk_device, mic_device):
        """
        開始錄音
        spk_device: 選擇的喇叭設備 dict (或 None)
        mic_device: 選擇的麥克風設備 dict (或 None)
        """
        if self.is_recording:
            return False, "已經在錄音中"

        if not spk_device and not mic_device:
            return False, "必須至少選擇一個設備（喇叭或麥克風）進行錄音"

        self._init_pyaudio()
        if not self.p:
            return False, "音訊引擎初始化失敗，請檢查 Windows 音效服務是否正常"

        self.is_recording = True
        self.start_time = time.time()
        self.record_seconds = 0.0

        temp_dir = tempfile.gettempdir()
        self.temp_spk_path = os.path.join(temp_dir, f"teams_rec_spk_{os.getpid()}_{int(time.time())}.wav")
        self.temp_mic_path = os.path.join(temp_dir, f"teams_rec_mic_{os.getpid()}_{int(time.time())}.wav")

        if spk_device:
            self.spk_thread = threading.Thread(
                target=self._record_stream,
                args=(spk_device, self.temp_spk_path, "spk_level"),
                daemon=True
            )
            self.spk_thread.start()
        else:
            self.spk_thread = None
            self.spk_level = 0.0

        if mic_device:
            self.mic_thread = threading.Thread(
                target=self._record_stream,
                args=(mic_device, self.temp_mic_path, "mic_level"),
                daemon=True
            )
            self.mic_thread.start()
        else:
            self.mic_thread = None
            self.mic_level = 0.0

        return True, "開始錄音"

    def stop_recording(self):
        """停止錄音"""
        if not self.is_recording:
            return
        self.is_recording = False

        if self.spk_thread and self.spk_thread.is_alive():
            self.spk_thread.join(timeout=2.0)
        if self.mic_thread and self.mic_thread.is_alive():
            self.mic_thread.join(timeout=2.0)

        self.spk_level = 0.0
        self.mic_level = 0.0

    def mix_and_export_mp3(self, output_mp3_path, spk_gain=1.0, mic_gain=1.2, bitrate=192, keep_separate_tracks=False, progress_cb=None):
        """
        混音並轉成 MP3 檔案
        - spk_gain: 喇叭音量倍率 (預設 1.0)
        - mic_gain: 麥克風音量倍率 (預設 1.2，適度加強會議中自己的聲音)
        - bitrate: MP3 位元率 (kbps，預設 192)
        - keep_separate_tracks: 是否額外保留揚聲器與麥克風獨立音軌 (供高精準說話者識別轉錄)
        - progress_cb: 進度回報回呼函式 callback(float percent, str status)
        """
        def update_progress(p, msg):
            if progress_cb:
                progress_cb(p, msg)

        update_progress(0.05, "讀取音軌中...")

        has_spk = self.spk_thread is not None and os.path.exists(self.temp_spk_path)
        has_mic = self.mic_thread is not None and os.path.exists(self.temp_mic_path)

        if not has_spk and not has_mic:
            raise RuntimeError("沒有可供混音的音訊資料")

        target_rate = 48000
        target_channels = 2

        def read_and_resample(wav_path, gain):
            with wave.open(wav_path, "rb") as wf:
                orig_rate = wf.getframerate()
                orig_channels = wf.getnchannels()
                frames = wf.getnframes()
                if frames == 0:
                    return np.zeros((0, 2), dtype=np.float32)
                raw_data = wf.readframes(frames)
                samples = np.frombuffer(raw_data, dtype=np.int16).astype(np.float32)

                # 重構成 (N, channels)
                if orig_channels > 1:
                    samples = samples.reshape(-1, orig_channels)
                    if orig_channels > 2:
                        samples = samples[:, :2] # 取前雙聲道
                else:
                    # Mono 轉 Stereo
                    samples = np.column_stack((samples, samples))

                # 重採樣至 target_rate
                if orig_rate != target_rate and len(samples) > 0:
                    orig_len = len(samples)
                    new_len = int(orig_len * target_rate / orig_rate)
                    orig_indices = np.linspace(0, orig_len - 1, orig_len)
                    new_indices = np.linspace(0, orig_len - 1, new_len)
                    left = np.interp(new_indices, orig_indices, samples[:, 0])
                    right = np.interp(new_indices, orig_indices, samples[:, 1])
                    samples = np.column_stack((left, right))

                return samples * gain

        update_progress(0.20, "處理喇叭音訊...")
        spk_data = read_and_resample(self.temp_spk_path, spk_gain) if has_spk else None

        update_progress(0.40, "處理麥克風音訊...")
        mic_data = read_and_resample(self.temp_mic_path, mic_gain) if has_mic else None

        update_progress(0.60, "混音中...")
        if spk_data is not None and mic_data is not None:
            max_len = max(len(spk_data), len(mic_data))
            mixed = np.zeros((max_len, 2), dtype=np.float32)
            if len(spk_data) > 0:
                mixed[:len(spk_data)] += spk_data
            if len(mic_data) > 0:
                mixed[:len(mic_data)] += mic_data
        elif spk_data is not None:
            mixed = spk_data
        else:
            mixed = mic_data

        if len(mixed) == 0:
            raise RuntimeError("錄音內容長度為 0")

        # 防止破音 (Soft Limiter / tanh 壓縮或平滑保護)
        peak = np.max(np.abs(mixed))
        if peak > 32767.0:
            update_progress(0.70, "防破音動態平衡調整...")
            # 軟限制保護音質不失真
            mixed = np.tanh(mixed / 32767.0) * 32767.0

        int16_mixed = np.clip(mixed, -32768, 32767).astype(np.int16)

        update_progress(0.80, f"編碼為 MP3 ({bitrate} kbps)...")
        encoder = lameenc.Encoder()
        encoder.set_bit_rate(bitrate)
        encoder.set_in_sample_rate(target_rate)
        encoder.set_channels(target_channels)
        encoder.set_quality(2) # 2: High quality

        os.makedirs(os.path.dirname(os.path.abspath(output_mp3_path)), exist_ok=True)
        with open(output_mp3_path, "wb") as f_out:
            # 分塊傳送給 lameenc 防止巨型陣列爆記憶體
            chunk_samples = 48000 # 每次處理 1 秒
            for i in range(0, len(int16_mixed), chunk_samples):
                chunk = int16_mixed[i:i + chunk_samples]
                mp3_data = encoder.encode(chunk.tobytes())
                if mp3_data:
                    f_out.write(mp3_data)
            flush_data = encoder.flush()
            if flush_data:
                f_out.write(flush_data)

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
        # 清理暫存 WAV
        if has_spk and os.path.exists(self.temp_spk_path):
            try:
                os.remove(self.temp_spk_path)
            except Exception:
                pass
        if has_mic and os.path.exists(self.temp_mic_path):
            try:
                os.remove(self.temp_mic_path)
            except Exception:
                pass

        update_progress(1.0, "錄音儲存完成！")
        return output_mp3_path

    def cleanup(self):
        """釋放所有資源"""
        self.stop_recording()
        if self.p:
            try:
                self.p.terminate()
            except Exception:
                pass
