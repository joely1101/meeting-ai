@echo off
chcp 65001 >nul
title 重設管理員密碼

echo ==================================================
echo   AI 會議系統 - 管理員密碼重設工具
echo ==================================================
echo.

set /p NEW_PWD="請輸入管理員新密碼 (預設 admin888): "
if "%NEW_PWD%"=="" set NEW_PWD=admin888

python set_admin_password.py %NEW_PWD%
echo.
pause
