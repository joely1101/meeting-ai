# 智匯會議錄音與 AI 雲端轉譯系統 (Meeting AI Standalone)

專為企業線上（Teams / Zoom / Google Meet / Webex）與實體會議打造的次世代開源會議錄音與 AI 雲端轉譯系統。
本專案已完全獨立解耦，**完全容器化 (Docker)**，具備 **Google (Gmail) 快速認證**、**無任何預設私人 Key**、**動態 URL 注入**與 **Key Pool 資源池動態負載平衡**。

---

## 🌟 核心特色與架構

### 1. Docker 極簡一鍵部署
- 只要將 Docker 部署在伺服器，啟動時帶入 `PUBLIC_URL`，系統立即就緒。
- 後端伺服器會將 `PUBLIC_URL` 自動同步給 Web 前端與 Windows 客戶端下載包，使用者下載即用，無須手動設定伺服器網址。
- 會議音檔、逐字稿與 SQLite 資料庫持久化掛載於 `/app/data`。

### 2. Google OAuth 2.0 (Gmail) 快速認證
- 移除傳統繁瑣的 Email 驗證碼與信箱網域註冊流程。
- 使用者只要點擊「使用 Google 帳號登入」，授權後自動建立專屬雲端會議庫並登入。
- Windows 桌面錄音器同樣支援一鍵 Google 登入（自動開啟系統瀏覽器授權完成綁定）。

### 3. 金鑰資源池與動態負載平衡 (Key Pool)
- **零私人 Key 殘留**：系統預設無任何私人 Key，乾淨安全。
- 系統管理員啟動後，前往後台【Key Pool 資源池管理】新增一組或多組 Google Gemini API Key。
- 轉錄任務支援「Least-Used」動態負載平衡：自動挑選使用次數最少的金鑰輪替發起請求，避免單一 Key 配額耗盡或請求受限。
- 同時支援使用者在「個人偏好設定」填入自己的專屬 Key。

### 4. Windows 輕量級錄音客戶端 (`client/` & `web/會議錄音器.exe`)
- **WASAPI Loopback 雙向混音**：完美錄製耳機揚聲器（對方聲音）與麥克風（自己聲音），無單向或破音問題。
- **純錄音架構**：電腦不卡頓，錄音結束後一鍵上傳至雲端伺服器由 Gemini 雲端模型自動識別說話者、產出逐字稿與重點摘要。
- **自適應 URL**：支援同目錄 `client_config.json` 或二進位 Overlay 動態注入 URL，下載解壓即可連線。

---

## 🚀 快速啟動指南

### 方式 A: 使用 Docker Compose 一鍵部署 (推薦)

1. **進入專案目錄**：
   ```bash
   cd record_standalone
   ```

2. **編輯 `docker-compose.yml` 中的環境變數**：
   ```yaml
   services:
     meeting-ai:
       image: joely1101/meeting-ai:latest
       ports:
         - "8080:8080"
       environment:
         - PORT=8080
         # 您的伺服器對外正式網址 (重要：此 URL 會自動注入網頁與客戶端)
         - PUBLIC_URL=https://meeting.yourdomain.com
         # Google OAuth Client ID 與 Secret
         - GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
         - GOOGLE_CLIENT_SECRET=your-client-secret
         # 管理員 Google 信箱 (只要以此信箱 Google 登入即自動升級為 admin)
         - ADMIN_EMAIL=admin@gmail.com
       volumes:
         - ./data:/app/data
   ```

3. **啟動容器**：
   ```bash
   docker compose up -d
   ```

---

### 方式 B: 使用 Docker CLI 部署

```bash
# 1. 建置映像檔
docker build -t meeting-ai:latest .

# 2. 啟動容器
docker run -d \
  --name meeting-ai \
  --restart unless-stopped \
  -p 8080:8080 \
  -e PUBLIC_URL=https://meeting.yourdomain.com \
  -e GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com \
  -e GOOGLE_CLIENT_SECRET=your-client-secret \
  -e ADMIN_EMAIL=admin@gmail.com \
  -v $(pwd)/data:/app/data \
  meeting-ai:latest
```

---

### 方式 C: 本機 Python 直接啟動

```bash
# 1. 安裝必要依賴
pip install -r requirements.txt

# 2. 啟動伺服器 (預設 Port 8080)
python server/server.py 8080
```

---

## 🔑 首次啟用與 Admin 設定

1. **進入網站**：
   瀏覽器開啟您的伺服器網址（例如 `https://meeting.yourdomain.com` 或 `http://localhost:8080`）。
2. **管理員登入**：
   - 若有設定 `ADMIN_EMAIL`，點擊「使用 Google 帳號登入」並選擇該信箱，系統自動賦予 Admin 角色。
   - 或點擊登入框下方的「管理員本地密碼登入」，預設帳號為 `admin@local`，密碼預設為 `admin888`（亦可透過 `python server/set_admin_password.py [新密碼]` 隨時重設）。
3. **新增 Gemini API Key**：
   - 進入【系統管理】面板 -> 【Key Pool 金鑰資源池】分頁。
   - 點擊「新增 API Key」，貼上您的 Google Gemini API Key，即可立即啟用全系統 AI 轉錄服務！

---

## 🎙️ 客戶端使用方式

1. 登入網頁後，點擊首頁或儀表板上的「下載 Windows 錄音器套件 (ZIP)」或「單一執行檔 (.EXE)」。
2. 解壓縮執行 `會議錄音器.exe`。
3. 點擊頂部「Google 登入」按鈕，系統將自動開啟瀏覽器進行 Google 授權，完成後錄音器即自動綁定登入。
4. 選擇揚聲器與麥克風裝置，點擊「⏺ 開始會議錄音」，會議結束後自動上傳並產出重點摘要與逐字稿！

---

## 📁 目錄結構

```text
record_standalone/
├── Dockerfile                   # Docker 映像檔建置定義
├── docker-compose.yml           # Docker Compose 一鍵部署範本
├── entrypoint.sh                # 容器開機入口腳本
├── requirements.txt             # 後端 Python 依賴清單
├── README.md                    # 本文件
├── server/                      # 後端核心伺服器
│   ├── server.py                # HTTP 206 串流、Google Auth、API 端點
│   ├── database.py              # SQLite 資料庫模組 (meetings, users, system_keys)
│   ├── google_auth.py           # Google OAuth 2.0 授權與票據交換
│   ├── ai_transcriber.py        # Gemini 3.8 Flash AI 轉錄模組
│   ├── config.json              # 伺服器本機設定檔 (零私人 key)
│   ├── set_admin_password.py    # 管理員密碼重設 CLI
│   ├── set_admin_password.bat   # Windows 密碼重設腳本
│   └── set_admin_password.sh    # Linux/Mac 密碼重設腳本
├── web/                         # 前端現代化單頁 Web 應用
│   ├── index.html               # 儀表板、逐字稿時間軸、Key Pool 管理介面
│   └── 會議錄音器.exe            # 預編譯的 Windows 錄音客戶端
├── client/                      # Windows 錄音客戶端原始碼
│   ├── recorder_gui.py          # 錄音器 GUI (Tkinter + Google 一鍵授權)
│   ├── audio_engine.py          # WASAPI Loopback 雙向混音引擎
│   ├── build_exe.py             # PyInstaller 打包腳本
│   └── client_config.json       # 客戶端設定範本
└── data/                        # 持久化資料夾 (掛載 Docker Volume)
    ├── meetings/                # 會議音訊、逐字稿與摘要
    └── ToBeDelete/              # 安全刪除備份目錄
```
