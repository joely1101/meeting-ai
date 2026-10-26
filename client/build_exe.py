import os
import sys
import subprocess
import datetime
import shutil

def build():
    build_time = datetime.datetime.now().strftime("%Y/%m/%d-%H:%M")
    print(f"==================================================")
    print(f"開始編譯 會議錄音 v2.0 EXE")
    print(f"構建時間戳: {build_time}")
    print(f"==================================================")

    base_dir = os.path.dirname(os.path.abspath(__file__))
    web_dir = os.path.abspath(os.path.join(base_dir, "..", "web"))

    # 1. 以 PyInstaller 打包
    print(f"[1/2] 正在使用 PyInstaller 進行單一檔打包...")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--windowed",
        "--name", "會議錄音",
        "--onefile",
        "recorder_gui.py"
    ]
    subprocess.run(cmd, cwd=base_dir, check=True)

    # 2. 複製到發布目錄
    dist_exe = os.path.join(base_dir, "dist", "會議錄音.exe")
    target_exe = os.path.join(base_dir, "會議錄音.exe")
    web_exe = os.path.join(web_dir, "會議錄音.exe")

    for exe_name in ["會議錄音.exe", "會議錄音器.exe", "Teams錄音器.exe"]:
        try:
            subprocess.run(["taskkill", "/f", "/im", exe_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    if os.path.exists(dist_exe):
        shutil.copy2(dist_exe, target_exe)
        shutil.copy2(dist_exe, web_exe)
        # 同步保留一份舊名以防有外部舊連結
        legacy_web_exe = os.path.join(web_dir, "會議錄音器.exe")
        try:
            shutil.copy2(dist_exe, legacy_web_exe)
        except Exception:
            pass
        print(f"[2/2] 已將 EXE 複製至:")
        print(f"      用戶端目錄: {target_exe}")
        print(f"      下載目錄: {web_exe}")

    print(f"==================================================")
    print(f"[OK] 編譯完成！")
    print(f"==================================================")

if __name__ == "__main__":
    build()
