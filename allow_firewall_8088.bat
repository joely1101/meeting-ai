@echo off
chcp 65001 >nul
title Allow Port 8088
echo ==================================================
echo   Adding Windows Firewall Rule for Port 8088...
echo ==================================================
echo.

netsh advfirewall firewall delete rule name="WSL Webserver 8088" >nul 2>&1
netsh advfirewall firewall add rule name="WSL Webserver 8088" dir=in action=allow protocol=TCP localport=8088

echo.
echo ==================================================
echo   Check Result Above. If OK, Port 8088 is Opened!
echo ==================================================
echo.
pause
