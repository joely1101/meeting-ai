@echo off
chcp 65001 >nul

:: 自動檢查並以管理員身分重新啟動 (自動彈出 UAC 確認)
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo 正在請求系統管理員權限...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

title 放行防火牆 8088 端口
cd /d "%~dp0"
set LOGFILE=%~dp0firewall_result.txt

echo ================================================== > "%LOGFILE%"
echo 執行時間: %date% %time% >> "%LOGFILE%"
echo 正在放行 Windows 防火牆 TCP 8088... >> "%LOGFILE%"
echo ================================================== >> "%LOGFILE%"

echo 執行 netsh 指令... >> "%LOGFILE%"
netsh advfirewall firewall delete rule name="WSL Webserver 8088" >> "%LOGFILE%" 2>&1
netsh advfirewall firewall add rule name="WSL Webserver 8088" dir=in action=allow protocol=TCP localport=8088 >> "%LOGFILE%" 2>&1

echo.
echo 執行結果已儲存至: %LOGFILE%
type "%LOGFILE%"
echo.
pause
