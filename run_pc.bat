@echo off
title TeraStream Pro - Local Server
cd /d "%~dp0"
echo ==========================================
echo   Starting TeraStream Pro Localhost Server
echo ==========================================
echo.
timeout /t 2 /nobreak >nul
start http://localhost:8080
python app.py
pause
